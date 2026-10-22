using System.Globalization;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using Microsoft.Data.Sqlite;

namespace Maid;

/// <summary>1 往復の会話。キャラから話しかけたとき（Proactive）は、相手の発言が無い。</summary>
public sealed record Turn(DateTime When, string User, string Assistant, bool Proactive = false)
{
    public IEnumerable<string> Lines()
    {
        if (!Proactive && User.Length > 0)
            yield return $"相手: {User}";
        if (Assistant.Length > 0)
            yield return $"キャラ: {Assistant}";
    }
}

/// <summary>NOW.md の 1 行。When が無いのは、手で書いた日時の無い行（ずっと残す）。</summary>
public sealed record NowItem(string Kind, string Text, DateTime? When);

/// <summary>
/// 記憶。全部 data\ に保存し、外には出さない。覚えるのは「相手のこと」と「二人の間のこと」だけ（キャラの性格は設定で固定）。
/// <list type="bullet">
/// <item>PROFILE.md: 相手の長く変わらない性格・傾向・好み・生活（抽象的に）</item>
/// <item>NOW.md: 今のこと（具体的に・日時付き・期限が来たら消す）。[状況] 1 日 / [約束] 7 日 / [近況] 30 日</item>
/// <item>memory.db: 1 回の会話ごとの要約と、意味のベクトル</item>
/// <item>log\日付.jsonl: 1 往復ごとの会話ログ</item>
/// </list>
/// 30 分話さなかったら（と、止めるとき）、まだまとめていない会話から覚えておくことを書き出して振り分ける。
/// 「覚えて」「忘れて」と言われたら、その場で反映する。
/// </summary>
public sealed class Memory : IDisposable
{
    public static readonly TimeSpan DefaultIdle = TimeSpan.FromMinutes(30);
    const int MaxNoteLines = 25;
    static readonly Dictionary<string, TimeSpan> NowKinds = new()
    {
        ["状況"] = TimeSpan.FromDays(1), ["約束"] = TimeSpan.FromDays(7), ["近況"] = TimeSpan.FromDays(30),
    };
    static readonly HashSet<string> Nothing = ["なし", "無し", "特になし", "特に無し", "ない", "ありません", "none", "None", "-"];
    static readonly Regex NowLine = new(@"^- \[(状況|約束|近況)\] (.+?)（(\d{4}-\d{2}-\d{2} \d{2}:\d{2})）$");
    const int RelatedEpisodes = 3;
    const double MinSimilarity = 0.5;
    const int HistoryKeep = 30;
    static readonly Regex Spaces = new(@"\s+");
    static readonly Regex Remember = new("覚えて|おぼえて|忘れないで|わすれないで|忘れて|わすれて");
    static readonly JsonSerializerOptions Json = new() { Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping };

    const string EpisodePrompt =
        "次は、相手（ユーザー）とキャラの会話の記録です。あとで思い出せるように、" +
        "何を話したか・相手の様子や出来事・約束したことを、3〜5 文の日本語で要約してください。" +
        "相手は「相手」、キャラは「キャラ」と書く。要約だけを出力する。";
    // 小さいモデルは、会話を丸ごと渡してメモを書かせると誰の発言かを取り違えたり、その場の思いつきを好みとして書いたりするので、
    // 「種類付きで書き出させる」と「振り分ける」を分ける
    const string FactsPrompt =
        "あなたは記憶係です。次は「相手」（ユーザー。人間）と「キャラ」（AI のキャラクター）の会話です。" +
        "次に話すときに知っておくとよいことを、1 行ずつ、次のどれかで始めて書き出してください。\n" +
        "プロフィール: 相手の長く変わらない性格・傾向・好み・生活。具体的な出来事は一般化して抽象的に書く" +
        "（例:「ハンバーグは作るのが大変」→「手間のかかることは面倒くさがる」）\n" +
        "状況: 今日〜明日くらいの、相手の具体的な状況（ごはん・体調・今していること・これからすること・家にあるもの）\n" +
        "約束: 相手とキャラの約束や、やりかけのこと（例:「今度オムライスを作る」）\n" +
        "近況: 数週間続く、相手の具体的なこと（例:「引っ越しの準備中」）\n" +
        "済み: 果たされた約束・終わったこと・治ったこと（前に覚えたことを消すため）\n" +
        "忘れる: 相手が「忘れて」と言ったこと\n" +
        "注意:\n" +
        "- キャラ自身の好み・気持ち・希望は書かない（キャラが「ハンバーグがいい」と言っても書かない）。\n" +
        "- 相手が質問しただけのことは書かない。その会話の中だけのやりとりや雑談は書かない。\n" +
        "- 何も無ければ「なし」とだけ出力する。";
    const string ProfilePrompt =
        "あなたは記憶係です。【今のプロフィール】は、相手（ユーザー。人間）の長く変わらない性格・傾向・好み・生活の箇条書きです。" +
        "【足すこと】と【忘れること】を反映した全文を出力してください。ルール:\n" +
        "- 抽象度を高く保つ。具体的な出来事ではなく、傾向として書く（「面倒くさがりなところがある」「辛いものが苦手」など）。\n" +
        "- 似たものはまとめる。矛盾したら新しい方を残す。【忘れること】は消す。\n" +
        "- 1 行に 1 つ、「- 」で始める。{0} 行まで。書いてないことや推測は足さない。全文だけを出力する。";
    const string TidyPrompt =
        "あなたは記憶係です。【今のプロフィール】は相手（ユーザー）の性格・傾向・好み・生活です。整理してください。" +
        "同じ内容はまとめ、矛盾するものは新しい方を残し、具体的すぎるものは傾向として抽象的に書き直す。" +
        "1 行に 1 つ、「- 」で始める。{0} 行まで。整理した全文だけを出力する。";
    const string ContextHead = "【あなたが覚えていること。会話に関係があるときだけ自然に使い、全部を言わない】";

    readonly string dir;
    readonly Func<JsonArray, int, Task<string>> llm;
    readonly Func<IReadOnlyList<string>, Task<float[][]?>>? embed;
    readonly TimeSpan idle;
    readonly object gate = new();                     // ファイルや DB を読み書きする短い間だけ
    readonly SemaphoreSlim work = new(1, 1);          // 記憶のまとめ（頭脳を使うので長い）。会話はこれを待たない
    readonly SqliteConnection db;
    List<(long Id, string Started, string Summary, float[]? Vector)>? episodes;
    readonly Dictionary<long, HashSet<string>> episodeGrams = [];  // 前の会話の要約の 2 文字ずつ（毎回作り直さない）
    long busySince;

    /// <param name="llm">(メッセージ, 最大トークン) → 返事の文字列。</param>
    /// <param name="embed">文の一覧 → 意味のベクトルの一覧（使えなければ null）。</param>
    public Memory(string folder, Func<JsonArray, int, Task<string>> llm, Func<IReadOnlyList<string>, Task<float[][]?>>? embed = null,
                  TimeSpan? idle = null)
    {
        dir = folder;
        this.llm = llm;
        this.embed = embed;
        this.idle = idle ?? DefaultIdle;
        Directory.CreateDirectory(Path.Combine(dir, "log"));
        Directory.CreateDirectory(Path.Combine(dir, "history"));
        // 接続は 1 本だけ。使い回し（プール）をすると、閉じても Windows でファイルがつかまれたままになる
        db = new SqliteConnection($"Data Source={Path.Combine(dir, "memory.db")};Pooling=False");
        db.Open();
        Exec("CREATE TABLE IF NOT EXISTS episodes (id INTEGER PRIMARY KEY, started TEXT, ended TEXT, summary TEXT, embedding TEXT)");
    }

    string ProfileMd => Path.Combine(dir, "PROFILE.md");
    string NowMd => Path.Combine(dir, "NOW.md");
    string StatePath => Path.Combine(dir, "state.json");

    void Exec(string sql, params (string, object?)[] args)
    {
        using var cmd = db.CreateCommand();
        cmd.CommandText = sql;
        foreach (var (k, v) in args)
            cmd.Parameters.AddWithValue(k, v ?? DBNull.Value);
        cmd.ExecuteNonQuery();
    }

    static string Iso(DateTime t) => t.ToString("yyyy-MM-dd'T'HH:mm:ss", CultureInfo.InvariantCulture);

    /// <summary>記憶のまとめ中か（その間は頭脳がそちらに使われるので、返事が遅くなる）。</summary>
    public bool Busy => Interlocked.Read(ref busySince) != 0;

    // ---------------------------------------------------------------- 記録
    public void Record(string user, string assistant, bool proactive = false, DateTime? when = null)
    {
        var t = when ?? DateTime.Now;
        var row = new JsonObject { ["t"] = Iso(t), ["user"] = user, ["assistant"] = assistant, ["proactive"] = proactive };
        lock (gate)
            File.AppendAllText(Path.Combine(dir, "log", $"{t:yyyy-MM-dd}.jsonl"), row.ToJsonString(Json) + "\n");
        if (!proactive && Remember.IsMatch(user))
            _ = Task.Run(async () =>  // 「覚えて」「忘れて」はその場で（裏で）反映する
            {
                try
                {
                    await UpdateNotesAsync([new Turn(t, user, assistant)]);
                }
                catch (Exception e)
                {
                    Log.Error("「覚えて」「忘れて」を反映できませんでした", e);
                }
            });
    }

    JsonObject State()
    {
        try
        {
            return JsonNode.Parse(File.ReadAllText(StatePath)) as JsonObject ?? [];
        }
        catch (Exception e) when (e is IOException or JsonException)
        {
            return [];
        }
    }

    void SaveState(string key, string value)
    {
        var s = State();
        s[key] = value;
        File.WriteAllText(StatePath, s.ToJsonString(Json));
    }

    /// <summary>まだまとめていない会話。</summary>
    public List<Turn> Pending()
    {
        var since = State()["summarized_until"]?.GetValue<string>() ?? "";
        var turns = new List<Turn>();
        foreach (var path in Directory.GetFiles(Path.Combine(dir, "log"), "*.jsonl").Order(StringComparer.Ordinal))
        {
            if (since.Length >= 10 && string.CompareOrdinal(Path.GetFileNameWithoutExtension(path), since[..10]) < 0)
                continue;
            foreach (var line in File.ReadAllLines(path))
            {
                JsonNode? row;
                try
                {
                    row = JsonNode.Parse(line);
                }
                catch (JsonException)
                {
                    continue;
                }
                var t = row?["t"]?.GetValue<string>() ?? "";
                if (t.Length == 0 || string.CompareOrdinal(t, since) <= 0)
                    continue;
                turns.Add(new Turn(DateTime.Parse(t, CultureInfo.InvariantCulture), row!["user"]?.GetValue<string>() ?? "",
                                   row["assistant"]?.GetValue<string>() ?? "", row["proactive"]?.GetValue<bool>() ?? false));
            }
        }
        return turns;
    }

    // ---------------------------------------------------------------- まとめ
    /// <summary>最後の会話から 30 分たっていたら、まとめる。</summary>
    public async Task<bool> MaybeConsolidateAsync(DateTime? now = null)
    {
        var turns = Pending();
        if (turns.Count == 0 || (now ?? DateTime.Now) - turns[^1].When < idle)
            return false;
        return await ConsolidateAsync(turns);
    }

    /// <summary>まだまとめていない会話を要約し、メモを書き直す。tidy なら最後にメモを整理する（止めるとき）。</summary>
    public async Task<bool> ConsolidateAsync(List<Turn>? turns = null, bool tidy = false)
    {
        await work.WaitAsync();
        try
        {
            // 待っている間にほかの呼び出し（止めるときのまとめなど）が済ませた分は除く（二重にまとめない）
            var since = State()["summarized_until"]?.GetValue<string>() ?? "";
            turns = turns?.Where(t => string.CompareOrdinal(Iso(t.When), since) > 0).ToList() ?? Pending();
            if (turns.Count == 0 && !tidy)
                return false;
            var sw = System.Diagnostics.Stopwatch.StartNew();
            Interlocked.Exchange(ref busySince, DateTime.Now.Ticks);
            Log.Info($"記憶をまとめています（{turns.Count} 往復）...");
            try
            {
                foreach (var session in Sessions(turns))  // 30 分以上あいたところで分け、古い順にまとめる（状態の変化を順に反映するため）
                {
                    if (session.Any(t => t.User.Length > 0))
                    {
                        var text = string.Join("\n", session.SelectMany(t => t.Lines()));
                        var summary = await AskAsync([Msg.Make("system", EpisodePrompt), Msg.Make("user", text)], 400);
                        if (summary.Length > 0)
                            await AddEpisodeAsync(session[0].When, session[^1].When, summary);
                        await UpdateNotesCoreAsync(session);
                    }
                    lock (gate)
                        SaveState("summarized_until", Iso(session[^1].When));
                }
                if (tidy)
                {
                    await RewriteAsync(ProfileMd, string.Format(TidyPrompt, MaxNoteLines), "");
                    WriteNow(NowItems());  // 期限切れを消す
                }
                return turns.Count > 0;
            }
            finally
            {
                Interlocked.Exchange(ref busySince, 0);
                Log.Info($"記憶をまとめました（{sw.Elapsed.TotalSeconds:0} 秒）");
            }
        }
        finally
        {
            work.Release();
        }
    }

    public List<List<Turn>> Sessions(List<Turn> turns)
    {
        var out_ = new List<List<Turn>>();
        foreach (var t in turns)
        {
            if (out_.Count > 0 && t.When - out_[^1][^1].When < idle)
                out_[^1].Add(t);
            else
                out_.Add([t]);
        }
        return out_;
    }

    /// <summary>会話から覚えておくことを書き出し、種類ごとに分ける。</summary>
    public async Task<Dictionary<string, List<string>>> FactsAsync(IEnumerable<Turn> turns)
    {
        var talk = string.Join("\n", turns.SelectMany(t => t.Lines()));
        var answer = await AskAsync([Msg.Make("system", FactsPrompt), Msg.Make("user", talk)], 600);
        var out_ = new[] { "プロフィール", "状況", "約束", "近況", "済み", "忘れる" }.ToDictionary(k => k, _ => new List<string>());
        foreach (var raw in answer.Split('\n'))
        {
            var line = raw.Trim().TrimStart('-', '・', '*', ' ').Trim().Replace("：", ":");
            var sep = line.IndexOf(':');
            if (sep < 0)
                continue;
            var kind = line[..sep].Trim();
            var text = line[(sep + 1)..].Trim().TrimEnd('。');
            if (out_.TryGetValue(kind, out var list) && text.Length > 0 && !Nothing.Contains(text))
                list.Add(text);
        }
        return out_;
    }

    public async Task UpdateNotesAsync(List<Turn> turns)
    {
        await work.WaitAsync();
        try
        {
            await UpdateNotesCoreAsync(turns);
        }
        finally
        {
            work.Release();
        }
    }

    async Task UpdateNotesCoreAsync(List<Turn> turns)
    {
        var when = turns.Count > 0 ? turns[^1].When : DateTime.Now;
        var f = await FactsAsync(turns);
        // NOW.md: 済み・忘れることを消して、新しいことを足す（頭脳に書き直させず、プログラムで行う）
        var gone = f["済み"].Concat(f["忘れる"]).ToList();
        var items = NowItems(when).Where(it => !gone.Any(g => Math.Max(Overlap(g, it.Text), Overlap(it.Text, g)) >= 0.4)).ToList();
        foreach (var kind in NowKinds.Keys)
            foreach (var text in f[kind])
                if (!items.Any(it => it.Kind == kind && Overlap(text, it.Text) >= 0.8))
                    items.Add(new NowItem(kind, text, when));
        WriteNow(items);
        // PROFILE.md: 足すこと・忘れることがあるときだけ、頭脳に書き直させる
        if (f["プロフィール"].Count > 0 || f["忘れる"].Count > 0)
        {
            static string Bullets(List<string> xs) => xs.Count > 0 ? string.Join("\n", xs.Select(x => $"- {x}")) : "（なし）";
            var change = $"【足すこと】\n{Bullets(f["プロフィール"])}\n\n【忘れること】\n{Bullets(f["忘れる"])}";
            await RewriteAsync(ProfileMd, string.Format(ProfilePrompt, MaxNoteLines), change);
        }
    }

    // ---------------------------------------------------------------- NOW.md
    /// <summary>NOW.md の中身（期限切れは除く）。</summary>
    public List<NowItem> NowItems(DateTime? now = null)
    {
        var t = now ?? DateTime.Now;
        var items = new List<NowItem>();
        foreach (var raw in ReadNote(NowMd).Split('\n'))
        {
            var line = raw.Trim();
            var m = NowLine.Match(line);
            if (m.Success)
            {
                var when = DateTime.ParseExact(m.Groups[3].Value, "yyyy-MM-dd HH:mm", CultureInfo.InvariantCulture);
                if (t - when < NowKinds[m.Groups[1].Value])
                    items.Add(new NowItem(m.Groups[1].Value, m.Groups[2].Value, when));
            }
            else if (line.StartsWith("- "))
                items.Add(new NowItem("メモ", line[2..], null));
        }
        return items;
    }

    public void WriteNow(List<NowItem> items)
    {
        var lines = items.Select(it => it.When is { } w ? $"- [{it.Kind}] {it.Text}（{w:yyyy-MM-dd HH:mm}）" : $"- {it.Text}").ToList();
        var text = string.Join("\n", lines) + (lines.Count > 0 ? "\n" : "");
        lock (gate)
        {
            if (text != (File.Exists(NowMd) ? File.ReadAllText(NowMd) : ""))
            {
                File.WriteAllText(NowMd, text);
                Log.Info($"NOW.md を更新しました（{lines.Count} 行）");
            }
        }
    }

    async Task RewriteAsync(string path, string prompt, string talk)
    {
        var old = ReadNote(path);
        if (old.Length == 0 && talk.Length == 0)
            return;
        var content = $"【今のプロフィール】\n{(old.Length > 0 ? old : "（まだ無い）")}" + (talk.Length > 0 ? $"\n\n{talk}" : "");
        var answer = await AskAsync([Msg.Make("system", prompt), Msg.Make("user", content)], 1200);
        var lines = answer.Split('\n').Select(l => l.Trim()).Where(l => l.StartsWith("- ")).Take(MaxNoteLines).ToList();
        var updated = string.Join("\n", lines);
        var forget = talk.Contains("【忘れること】\n- ");
        // 頭脳がうまく書けなかったとき（空・半分以下に減った）は、忘れてと言われていない限り書き換えない
        if (updated.Length == 0 || (old.Length > 0 && !forget && lines.Count < old.Split('\n').Length / 2.0))
        {
            if (answer.Length > 0)
                Log.Warn($"{Path.GetFileName(path)} の書き直しを見送りました（結果が短すぎる）");
            return;
        }
        if (updated == old)
            return;
        lock (gate)
        {
            Backup(path);
            File.WriteAllText(path, updated + "\n");
        }
        Log.Info($"{Path.GetFileName(path)} を更新しました（{lines.Count} 行）");
    }

    void Backup(string path)
    {
        if (!File.Exists(path))
            return;
        var hist = Path.Combine(dir, "history");
        var stem = Path.GetFileNameWithoutExtension(path);
        File.Copy(path, Path.Combine(hist, $"{stem}-{DateTime.Now:yyyyMMdd-HHmmss}.md"), overwrite: true);
        foreach (var old in Directory.GetFiles(hist, $"{stem}-*.md").Order(StringComparer.Ordinal).SkipLast(HistoryKeep))
            File.Delete(old);
    }

    async Task<string> AskAsync(JsonArray messages, int maxTokens)
    {
        try
        {
            return (await llm(messages, maxTokens) ?? "").Trim();
        }
        catch (Exception e)  // 頭脳が使えないときは、次の機会にまとめる
        {
            Log.Warn($"記憶の整理に失敗しました: {e.Message}");
            return "";
        }
    }

    static string ReadNote(string path)
    {
        try
        {
            return File.ReadAllText(path).Replace("\r\n", "\n").Trim();
        }
        catch (IOException)
        {
            return "";
        }
    }

    // ---------------------------------------------------------------- 前の会話
    public async Task AddEpisodeAsync(DateTime started, DateTime ended, string summary)
    {
        var vec = await EmbedOneAsync(summary);
        lock (gate)
        {
            Exec("INSERT INTO episodes (started, ended, summary, embedding) VALUES ($s, $e, $t, $v)",
                 ("$s", Iso(started)), ("$e", Iso(ended)), ("$t", summary), ("$v", vec is null ? null : JsonSerializer.Serialize(vec)));
            episodes = null;
        }
    }

    async Task<float[]?> EmbedOneAsync(string text)
    {
        if (embed is null)
            return null;
        try
        {
            var vecs = await embed([text]);
            return vecs is { Length: > 0 } ? vecs[0] : null;
        }
        catch (Exception e)
        {
            Log.Info($"意味のベクトルを作れませんでした: {e.Message}");
            return null;
        }
    }

    public List<(long Id, string Started, string Summary, float[]? Vector)> Episodes()
    {
        lock (gate)
        {
            if (episodes is null)
            {
                episodes = [];
                using var cmd = db.CreateCommand();
                cmd.CommandText = "SELECT id, started, summary, embedding FROM episodes ORDER BY id";
                using var r = cmd.ExecuteReader();
                while (r.Read())
                {
                    var e = r.IsDBNull(3) ? null : JsonSerializer.Deserialize<float[]>(r.GetString(3));
                    episodes.Add((r.GetInt64(0), r.GetString(1), r.GetString(2), e));
                }
            }
            return episodes;
        }
    }

    /// <summary>今の発言に関係ありそうな前の会話（日時, 要約）。意味の近さ（ベクトル）と、言葉の重なりの両方で探す。</summary>
    public async Task<List<(string Started, string Summary)>> RelatedAsync(string query, int limit = RelatedEpisodes)
    {
        var eps = Episodes();
        if (string.IsNullOrWhiteSpace(query) || eps.Count == 0)
            return [];
        var qvec = eps.Any(e => e.Vector is not null) ? await EmbedOneAsync(query) : null;
        var qgrams = Grams(query);
        HashSet<string> GramsOf((long Id, string, string Summary, float[]?) e)
        {
            lock (gate)
            {
                if (!episodeGrams.TryGetValue(e.Id, out var g))
                    episodeGrams[e.Id] = g = Grams(e.Summary);
                return g;
            }
        }
        return eps.Select(e => (score: 0.6 * Overlap(qgrams, GramsOf(e)) + (qvec is not null && e.Vector is not null ? Cosine(qvec, e.Vector) : 0), e))
                  .Where(x => x.score >= MinSimilarity)
                  .OrderByDescending(x => x.score)
                  .Take(limit)
                  .Select(x => (x.e.Started, x.e.Summary))
                  .ToList();
    }

    /// <summary>頭脳に渡す「覚えていること」。</summary>
    public async Task<string> ContextAsync(string query)
    {
        var now = DateTime.Now;
        var parts = new List<string>();
        var profile = ReadNote(ProfileMd);
        if (profile.Length > 0)
            parts.Add("相手について:\n" + profile);
        var items = NowItems(now);
        if (items.Count > 0)
            parts.Add("相手の今のこと:\n" + string.Join("\n", items.Select(it => it.When is { } w ? $"- [{it.Kind}] {it.Text}（{Ago(w, now)}）" : $"- {it.Text}")));
        var rel = await RelatedAsync(query);
        if (rel.Count > 0)
            parts.Add("関係がありそうな前の会話:\n" + string.Join("\n", rel.Select(r => $"- {Ago(DateTime.Parse(r.Started, CultureInfo.InvariantCulture), now)}: {r.Summary}")));
        return parts.Count > 0 ? ContextHead + "\n" + string.Join("\n\n", parts) : "";
    }

    // ---------------------------------------------------------------- 話しかける話題
    /// <summary>キャラから話しかけるときの話題の種。今のこと・前の会話・相手のこと・励まし・雑談から選ぶ。</summary>
    public string Topic(Random? rng = null)
    {
        rng ??= Random.Shared;
        var now = DateTime.Now;
        var recent = Episodes().TakeLast(5).ToList();
        var items = NowItems(now);
        var profile = ReadNote(ProfileMd).Split('\n').Where(l => l.StartsWith("- ")).Select(l => l[2..]).ToList();
        var promises = items.Where(it => it.Kind == "約束").ToList();
        var states = items.Where(it => it.Kind != "約束").ToList();
        var choices = new List<(int Weight, Func<string> Make)>();
        if (promises.Count > 0)
            choices.Add((4, () => "約束ややりかけのことに触れる（楽しみにしている・どうなったか聞く）: " + promises[rng.Next(promises.Count)].Text));
        if (states.Count > 0)
            choices.Add((4, () => "相手の今の状況を気にかけたり、どうなったか聞いたりする: " + states[rng.Next(states.Count)].Text));
        if (recent.Count > 0)
            choices.Add((3, () => "この前の話の続きを聞く。前の会話: " + recent[rng.Next(recent.Count)].Summary));
        if (profile.Count > 0)
            choices.Add((2, () => "相手の性格や好みに合わせた話をする: " + profile[rng.Next(profile.Count)]));
        choices.Add((1, () => "相手が今していることを応援したり、がんばりをほめたりする"));
        choices.Add((1, () => "相手を気づかう（休憩・水分・姿勢など）。押しつけない"));
        choices.Add((1, () => $"今の時間（{now.Hour} 時ごろ）らしい話をする（ごはん・眠さ・一日の様子など）"));
        choices.Add((1, () => "思いついた雑談や、相手への質問をする（好きなもの・最近のこと・今日あったこと）"));
        var pick = rng.Next(choices.Sum(c => c.Weight));
        foreach (var (w, make) in choices)
        {
            if (pick < w)
                return make();
            pick -= w;
        }
        return choices[^1].Make();
    }

    /// <summary>「さっき」「3 時間前」「昨日」「5 日前」。</summary>
    public static string Ago(DateTime when, DateTime now)
    {
        var d = now - when;
        if (d < TimeSpan.FromHours(1))
            return "さっき";
        if (d < TimeSpan.FromDays(1))
            return $"{(int)d.TotalHours} 時間前";
        if (d < TimeSpan.FromDays(2))
            return "昨日";
        return $"{d.Days} 日前";
    }

    /// <summary>2 文字ずつの重なり（日本語でも使える、言葉の重なりの目安）。0〜1。</summary>
    public static double Overlap(string a, string b) => Overlap(Grams(a), Grams(b));

    static double Overlap(HashSet<string> a, HashSet<string> b)
    {
        if (a.Count == 0)
            return 0;
        var n = 0;
        foreach (var g in a)
            if (b.Contains(g))
                n++;
        return n / (double)a.Count;
    }

    static HashSet<string> Grams(string s)
    {
        s = Spaces.Replace(s, "");
        var g = new HashSet<string>();
        for (var i = 0; i + 1 < s.Length; i++)
            g.Add(s.Substring(i, 2));
        return g;
    }

    public static double Cosine(float[] a, float[] b)
    {
        double dot = 0, na = 0, nb = 0;
        for (var i = 0; i < Math.Min(a.Length, b.Length); i++)
        {
            dot += a[i] * b[i];
            na += a[i] * a[i];
            nb += b[i] * b[i];
        }
        return na > 0 && nb > 0 ? dot / (Math.Sqrt(na) * Math.Sqrt(nb)) : 0;
    }

    public void Dispose()
    {
        work.Wait(TimeSpan.FromSeconds(3));  // まとめの途中なら少しだけ待つ（DB を閉じたあとに書かないように）
        db.Dispose();
    }
}
