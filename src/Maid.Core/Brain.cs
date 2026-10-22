using System.Diagnostics;
using System.Text;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace Maid;

/// <summary>
/// 頭脳への話しかけ方。会話に今の日時・覚えていること・（要るなら）Web 検索の結果を足してから頭脳に渡し、
/// 返事を少しずつ返す。調べる前には「ちょっと調べるね。」をすぐ返し、返事の最後に出典を付ける。
/// チャットに URL が貼られたら、検索せずにそのページを直接読む。同じ言葉の繰り返し（無限ループ）は打ち切る。
/// </summary>
public sealed class Brain
{
    const int SearchPages = 3;                                      // 本文を読むページ数
    const int FollowupTurns = 2;                                    // 調べたあと、続きの質問に前回の結果を渡す回数
    static readonly TimeSpan FollowupFor = TimeSpan.FromMinutes(10); // 〃 の期限
    static readonly TimeSpan ProactiveContextFor = TimeSpan.FromMinutes(30);  // 話しかけた内容を、相手の返事のときに思い出させる期限
    const string Weekdays = "日月火水木金土";
    // 過去の返事に付けた出典（今の形と前の形）
    static readonly Regex Sources = new(@"\s*(<think>出典: .*?</think>|\[出典: [^\]]*\])", RegexOptions.Singleline);
    const string ClockNote = "現在の日時は {0}。時刻・日付・曜日を聞かれたら、この日時を使って正確に答える（調べなくてよい）。";
    const string SearchNote = "（補足）Web 検索の結果が渡されたときは、それを使って最新の情報を答えてよい。" +
                              "結果が渡されていないのに、天気・ニュース・日付などの最新情報を推測で答えない。";
    const string QueryPrompt = "あなたは検索語を作る係です。会話の流れを踏まえて、最後の発言で調べたいことを Web 検索するための" +
                               "短い日本語の検索語を 1 行だけ出力してください。「調べて」だけのときは、その前の話題を調べる。" +
                               "説明や記号は付けない。会話相手の名前や家族の情報など、個人的な情報は入れない。" +
                               "検索の必要がなければ「なし」とだけ出力する。";
    const int QueryContextMessages = 6;  // 検索語を作るときに見せる直前の発言の数
    const string ProactivePrompt = "（あなたから話しかける番です。相手は今は何も言っていません）\n話題: {0}\n" +
                                   "キャラの口調で、1〜2 文で自然に話しかける。返事を急かさない。前置きや説明は言わない。";
    const string DefaultTopic = "思いついた雑談や、相手への質問をする";

    readonly Llm llm;
    readonly WebSearch search;
    readonly Memory? memory;
    readonly string searchMode;
    readonly string filler;
    SearchContext? lastSearch;
    int lastSearchTurns;
    (string Text, DateTime When) lastProactive = ("", DateTime.MinValue);

    public Brain(Llm llm, WebSearch search, Memory? memory, string searchMode, string filler)
    {
        this.llm = llm;
        this.search = search;
        this.memory = memory;
        this.searchMode = searchMode is "auto" or "explicit" ? searchMode : "off";
        this.filler = filler;
    }

    /// <summary>
    /// 返事をする。messages はキャラ設定（system）＋これまでの会話＋相手の最後の発言（proactive のときは無し）。
    /// emit には画面に出す文字を順に渡す（「ちょっと調べるね。」・返事・出典）。戻り値はそれを全部つなげたもの。
    /// </summary>
    public async Task<string> ReplyAsync(JsonArray messages, bool proactive, Action<string> emit, CancellationToken ct = default)
    {
        var sw = Stopwatch.StartNew();
        var all = new StringBuilder();
        void Say(string s)
        {
            if (s.Length == 0)
                return;
            all.Append(s);
            emit(s);
        }

        var userText = proactive ? "" : Msg.LastUserText(messages);
        var images = proactive || messages.Count == 0 ? 0 : Msg.ImageCount(messages[^1]);
        var (msgs, suffix) = await AugmentAsync(messages, proactive, Say, ct);
        var prepared = sw.Elapsed;

        var reply = new StringBuilder();
        TimeSpan? first = null;
        await foreach (var piece in llm.StreamAsync(msgs, ct))
        {
            first ??= sw.Elapsed;
            reply.Append(piece);
            Say(piece);
            if (IsLooping(reply.ToString()))
            {
                Log.Warn($"同じ言葉の繰り返しを見つけたので返事を打ち切りました: …{Tail(reply.ToString(), 40)}");
                break;  // ここで抜けると接続が切れ、頭脳の生成も止まる
            }
        }
        Say(suffix);
        var text = reply.ToString();
        Log.Info($"返事: 準備 {prepared.TotalSeconds:0.0} 秒 / 最初の言葉まで {(first ?? sw.Elapsed).TotalSeconds:0.0} 秒 / 全部で {sw.Elapsed.TotalSeconds:0.0} 秒"
                 + $"（{text.Length} 文字）{(images > 0 ? $"（画像 {images} 枚つき）" : "")}{(memory?.Busy == true ? "（記憶のまとめ中のため遅いかも）" : "")}");
        if (proactive && text.Length > 0)
            lastProactive = (text, DateTime.Now);
        if (text.Length > 0)
            memory?.Record(userText, text, proactive);
        return all.ToString();
    }

    /// <summary>日時と、覚えていることと、要るなら検索結果を差し込む。戻り値は (頭脳に渡す会話, 返事の最後に足す出典)。</summary>
    async Task<(JsonArray, string)> AugmentAsync(JsonArray messages, bool proactive, Action<string> say, CancellationToken ct)
    {
        var msgs = CleanHistory(messages);
        if (proactive)
        {
            var topic = memory?.Topic() ?? DefaultTopic;
            Log.Info($"話しかけます（話題: {Tail(topic, 60, head: true)}）");
            msgs.Add(Msg.Make("user", string.Format(ProactivePrompt, topic)));
        }
        else if (lastProactive.Text.Length > 0 && DateTime.Now - lastProactive.When < ProactiveContextFor)
        {
            // 直前にキャラから話しかけていたら、その内容を相手の返事の前に置く（画面の会話の記録には残していない）
            var list = msgs.Select(m => m?.DeepClone()).ToList();
            list.Insert(Math.Max(0, list.FindLastIndex(m => Msg.Role(m) == "user")), Msg.Make("assistant", lastProactive.Text));
            msgs = new JsonArray(list.ToArray());
            lastProactive = ("", DateTime.MinValue);
        }
        var history = Msg.Copy(msgs);
        var text = proactive ? "" : Msg.LastUserText(msgs);
        if (memory is not null)
        {
            var note = await memory.ContextAsync(text);
            if (note.Length > 0)
                msgs = Msg.AddNote(msgs, note);
        }
        if (searchMode != "off")
            msgs = Msg.AddNote(msgs, SearchNote);
        // 今の日時は最後の発言のすぐ前に置く（キャラ設定の後ろに足すより、小さいモデルでも使ってくれる）
        var now = DateTime.Now;
        msgs = Msg.InsertBeforeLastUser(msgs, string.Format(ClockNote, $"{now:yyyy年MM月dd日}（{Weekdays[(int)now.DayOfWeek]}曜日）{now.Hour}時{now.Minute}分"));

        var urls = searchMode != "off" ? WebSearch.FindUrls(text) : [];
        if (urls.Count > 0)  // 貼られたページは検索せずに直接読む
        {
            say(filler);
            var ctx = await search.ReadPagesAsync(urls, ct);
            Log.Info($"ページを読みました: {string.Join(", ", ctx.Results.Select(r => $"{r.Url} ({r.Text.Length} 文字)"))}");
            return UseResults(msgs, ctx);
        }
        if (WebSearch.NeedsSearch(text, searchMode))
        {
            say(filler);  // 調べている間に読み上げが始まる
            var sw = Stopwatch.StartNew();
            var query = await MakeQueryAsync(history, ct);
            Log.Info($"  検索語づくり {sw.Elapsed.TotalSeconds:0.0} 秒 →「{query ?? "なし"}」");
            if (query is null && WebSearch.NeedsSearch(text, "explicit"))  // はっきり頼まれたら必ず調べる
                query = FallbackQuery(history);
            if (!string.IsNullOrEmpty(query))
            {
                var ctx = await search.SearchAsync(query, SearchPages, ct);
                Log.Info($"検索「{query}」→ {(ctx.Results.Count > 0 ? string.Join(", ", ctx.Results.Select(r => r.Site)) : "見つからず")}");
                return UseResults(msgs, ctx);
            }
        }
        if (lastSearch is { } last && lastSearchTurns < FollowupTurns && now - last.When < FollowupFor)
        {
            lastSearchTurns++;
            msgs = Msg.InsertBeforeLastUser(msgs, WebSearch.BuildContext(last, followup: true));
        }
        return (msgs, "");
    }

    (JsonArray, string) UseResults(JsonArray msgs, SearchContext ctx)
    {
        lastSearch = ctx;
        lastSearchTurns = 0;
        return (Msg.InsertBeforeLastUser(msgs, WebSearch.BuildContext(ctx)), WebSearch.SourcesLine(ctx));
    }

    /// <summary>頭脳に、直前の会話を踏まえた短い検索語を作らせる。作れなければ発言から作る。「なし」なら null。</summary>
    async Task<string?> MakeQueryAsync(JsonArray history, CancellationToken ct)
    {
        var line = "";
        try
        {
            var answer = await llm.CompleteAsync(QueryRequest(history), 40, 0.2, ct);
            line = answer.Trim().Split('\n')[0].Trim(' ', '「', '」', '"', '\'', '`');
        }
        catch (HttpRequestException e)
        {
            Log.Warn($"検索語を作れませんでした: {e.Message}");
        }
        if (line is "なし" or "無し" or "NONE" or "none")
            return null;
        return line.Length > 0 ? (line.Length > 60 ? line[..60] : line) : FallbackQuery(history);
    }

    /// <summary>検索語づくりに渡すメッセージ（直前の会話を 1 つの文章にまとめる）。</summary>
    public static JsonArray QueryRequest(JsonArray messages)
    {
        var talk = messages.Where(m => Msg.Role(m) is "user" or "assistant").TakeLast(QueryContextMessages).ToList();
        var lines = talk.Select(m => (Msg.Role(m) == "user" ? "相手: " : "キャラ: ") + Msg.TextOf(m).Trim()).ToList();
        if (lines.Count > 0)
            lines[^1] = "最後の発言: " + Msg.TextOf(talk[^1]).Trim();
        return [Msg.Make("system", QueryPrompt), Msg.Make("user", string.Join("\n", lines))];
    }

    /// <summary>頭脳が検索語を作れなかったときの検索語。「調べて」だけなら、その前の相手の発言から作る。</summary>
    public static string FallbackQuery(JsonArray messages)
    {
        foreach (var m in messages.Reverse())
            if (Msg.Role(m) == "user" && WebSearch.FallbackQuery(Msg.TextOf(m)) is { Length: > 0 } q)
                return q;
        return "";
    }

    /// <summary>過去の返事から出典と「ちょっと調べるね。」を取り除く（残すと頭脳が真似して自分で書き始める）。</summary>
    public JsonArray CleanHistory(JsonArray messages)
    {
        var out_ = new JsonArray();
        foreach (var m in messages)
        {
            var c = m!.DeepClone();
            if (Msg.Role(c) == "assistant" && c["content"] is JsonValue)
            {
                var s = Sources.Replace(Msg.TextOf(c), "");
                if (filler.Length > 0 && s.StartsWith(filler))
                    s = s[filler.Length..];
                c["content"] = s;
            }
            out_.Add(c);
        }
        return out_;
    }

    /// <summary>末尾が同じ並びの繰り返しになっていたら true（例: 「ぱぱ、ぱぱ、ぱぱ、ぱぱ、」）。</summary>
    public static bool IsLooping(string text)
    {
        var t = text.Length > 400 ? text[^400..] : text;
        for (var n = 1; n <= 80; n++)
        {
            var reps = n <= 4 ? 8 : n <= 20 ? 4 : 3;
            if (t.Length < n * reps)
                continue;
            var unit = t[^n..];
            var ok = true;
            for (var k = 2; k <= reps && ok; k++)
                ok = string.CompareOrdinal(t, t.Length - n * k, unit, 0, n) == 0;
            if (ok)
                return true;
        }
        return false;
    }

    static string Tail(string s, int n, bool head = false) => s.Length <= n ? s : head ? s[..n] : s[^n..];
}
