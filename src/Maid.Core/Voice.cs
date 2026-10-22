using System.Net.Http.Json;
using System.Text.Json.Nodes;
using System.Threading.Channels;
using System.Text.RegularExpressions;

namespace Maid;

/// <summary>声（AivisSpeech Engine）。声の名前（「話者名/スタイル名」）をスタイル ID に変えて、文を wav にする。</summary>
public class Aivis
{
    readonly HttpClient http;
    readonly string defaultVoice;
    readonly double speed;
    List<(int Id, string Speaker, string Style)>? voices;

    public Aivis(string baseUrl, string voice, double speed, HttpMessageHandler? handler = null)
    {
        http = new HttpClient(handler ?? new HttpClientHandler()) { BaseAddress = new Uri(baseUrl.TrimEnd('/') + "/"), Timeout = TimeSpan.FromSeconds(30) };
        defaultVoice = voice;
        this.speed = speed;
    }

    public async Task<List<(int Id, string Speaker, string Style)>> VoicesAsync(bool refresh = false, CancellationToken ct = default)
    {
        if (voices is not null && !refresh)
            return voices;
        var speakers = JsonNode.Parse(await http.GetStringAsync("speakers", ct))!.AsArray();
        voices = speakers.SelectMany(sp => sp!["styles"]!.AsArray().Select(st =>
            (st!["id"]!.GetValue<int>(), sp["name"]!.GetValue<string>(), st["name"]!.GetValue<string>()))).ToList();
        return voices;
    }

    public async Task<int> ResolveAsync(string? voice = null, CancellationToken ct = default)
    {
        voice = string.IsNullOrWhiteSpace(voice) || voice == "default" ? defaultVoice : voice.Trim();
        if (int.TryParse(voice, out var id))
            return id;
        var parts = voice.Split('/', 2);
        var (speaker, style) = (parts[0], parts.Length > 1 ? parts[1] : "");
        foreach (var refresh in new[] { false, true })  // 声のモデルを追加した直後でも見つかるように 1 回だけ取り直す
        {
            var list = await VoicesAsync(refresh, ct);
            if (list.Count == 0)
                break;
            if (voice.Length == 0)
                return list[0].Id;
            foreach (var v in list)
                if (v.Speaker == speaker && (style.Length == 0 || v.Style == style))
                    return v.Id;
            if (style.Length > 0 && !refresh)
                continue;
            foreach (var v in list)  // スタイル名が違うだけなら、その話者の最初のスタイルで話す
            {
                if (v.Speaker == speaker)
                {
                    Log.Warn($"'{speaker}' にスタイル '{style}' が無いので '{v.Style}' を使います");
                    return v.Id;
                }
            }
        }
        throw new KeyNotFoundException($"声 '{voice}' が AivisSpeech に見つかりません（maid.settings.json の voice を確かめてください）");
    }

    public virtual async Task<byte[]> SynthesizeAsync(string text, CancellationToken ct = default)
    {
        var id = await ResolveAsync(null, ct);
        using var q = await http.PostAsync($"audio_query?text={Uri.EscapeDataString(text)}&speaker={id}", null, ct);
        q.EnsureSuccessStatusCode();
        var query = JsonNode.Parse(await q.Content.ReadAsStringAsync(ct))!.AsObject();
        query["speedScale"] = speed;
        query["prePhonemeLength"] = 0.0;   // 頭の無音を削ると体感が速くなる
        query["postPhonemeLength"] = 0.05;
        using var res = await http.PostAsJsonAsync($"synthesis?speaker={id}", query, ct);
        res.EnsureSuccessStatusCode();
        return await res.Content.ReadAsByteArrayAsync(ct);
    }

    /// <summary>最初の合成は声のモデルの読み込みで遅いので、起動したら 1 回しゃべらせておく。</summary>
    public async Task WarmUpAsync(CancellationToken ct)
    {
        for (var i = 0; i < 60 && !ct.IsCancellationRequested; i++)
        {
            try
            {
                var sw = System.Diagnostics.Stopwatch.StartNew();
                await SynthesizeAsync("あ", ct);
                Log.Info($"声の準備ができました（{sw.Elapsed.TotalSeconds:0.0} 秒）");
                return;
            }
            catch (KeyNotFoundException e)
            {
                Log.Warn(e.Message);
                return;
            }
            catch (Exception e) when (e is HttpRequestException or TaskCanceledException && !ct.IsCancellationRequested)
            {
                await Task.Delay(2000, ct);  // AivisSpeech Engine の起動待ち
            }
        }
    }
}

/// <summary>
/// 返事を文ごとに区切って声にする。文ごとに play(声の wav を base64 にしたもの, その文) を、返事の順に 1 つずつ呼ぶ。
/// 画面はその声を鳴らしはじめたときに文を出すので、声と文字がそろう。読まない部分（出典の &lt;think&gt;…&lt;/think&gt; など）は
/// 声なし（null）で順番どおりに渡す。Finish() で入れた区切りは play(null, null)（返事の終わり）になる。
/// </summary>
public sealed class Speaker
{
    // 文の終わり（「！？」「。」」のように続く記号や閉じかっこは、その文に含める）
    static readonly Regex SentenceEnd = new(@"(?<=[。！？!?♪〜…\n])(?![。！？!?♪〜…」』）)\n])");
    static readonly Regex[] Unspoken =
    [
        new(@"<think>.*?</think>", RegexOptions.Singleline), new(@"<think>.*$", RegexOptions.Singleline), new(@"\[[^\]]*\]"),
        new(@"\([^)]*\)"), new(@"（[^）]*）"), new(@"\*[^*]*\*"),
    ];

    readonly Func<string, CancellationToken, Task<byte[]?>> synthesize;
    readonly Action<string?, string?> play;
    readonly Channel<(string? Part, int Generation)> queue = Channel.CreateUnbounded<(string?, int)>();
    string buf = "";
    int generation;

    public Speaker(Func<string, CancellationToken, Task<byte[]?>> synthesize, Action<string?, string?> play)
    {
        this.synthesize = synthesize;
        this.play = play;
        _ = Task.Run(WorkAsync);
    }

    /// <summary>読み上げる部分だけ（出典や括弧の中は読まない）。</summary>
    public static string Spoken(string text)
    {
        foreach (var re in Unspoken)
            text = re.Replace(text, "");
        return text.Trim();
    }

    /// <summary>割り込み: まだ読んでいない文は捨てる。</summary>
    public void Reset()
    {
        Interlocked.Increment(ref generation);
        buf = "";
    }

    public void Feed(string chunk, bool final = false)
    {
        buf += chunk;
        var parts = new List<string>();
        while (true)
        {
            var cut = buf.IndexOf("<think>", StringComparison.Ordinal);
            var head = cut < 0 ? buf : buf[..cut];
            var sentences = SentenceEnd.Split(head).ToList();
            if (cut < 0)
            {
                // 文の途中は、続きが来るまで待つ。ちょうど記号で終わっているときも、「！？」のように続くかもしれないので待つ
                var tail = sentences[^1];
                sentences.RemoveAt(sentences.Count - 1);
                if (tail.Length == 0 && sentences.Count > 0)
                {
                    tail = sentences[^1];
                    sentences.RemoveAt(sentences.Count - 1);
                }
                buf = tail;
                parts.AddRange(sentences);
                break;
            }
            parts.AddRange(sentences);  // <think> の前は文の途中でも区切る
            var end = buf.IndexOf("</think>", cut, StringComparison.Ordinal);
            if (end < 0)
            {
                buf = buf[cut..];  // 閉じるまで待つ
                break;
            }
            parts.Add(buf[cut..(end + "</think>".Length)]);
            buf = buf[(end + "</think>".Length)..];
        }
        if (final)
        {
            parts.Add(buf);
            buf = "";
        }
        foreach (var p in parts.Where(p => p.Length > 0))
            queue.Writer.TryWrite((p, generation));
    }

    public void Finish() => queue.Writer.TryWrite((null, generation));

    async Task WorkAsync()
    {
        await foreach (var (part, gen) in queue.Reader.ReadAllAsync())
        {
            if (gen != generation)
                continue;
            try
            {
                if (part is null)
                {
                    play(null, null);
                    continue;
                }
                var text = Spoken(part);
                var wav = text.Length > 0 ? await synthesize(text, CancellationToken.None) : null;
                if (gen == generation)
                    play(wav is null ? null : Convert.ToBase64String(wav), part);
            }
            catch (Exception e)
            {
                Log.Error("声か文を画面に渡せませんでした", e);  // 1 つ失敗しても、次の文からは続ける
            }
        }
    }
}
