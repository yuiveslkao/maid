using System.Text;
using System.Text.Json.Nodes;
using Maid;
using Xunit;

namespace Maid.Tests;

public class WebSearchTests
{
    [Fact]
    public void WhenToSearch()
    {
        Assert.True(WebSearch.NeedsSearch("明日の天気調べて", "explicit"));
        Assert.True(WebSearch.NeedsSearch("今日のニュースある？", "auto"));
        Assert.False(WebSearch.NeedsSearch("今日のニュースある？", "explicit"));
        Assert.False(WebSearch.NeedsSearch("今何時？", "auto"));
        Assert.False(WebSearch.NeedsSearch("調べて", "off"));
        Assert.Equal("", WebSearch.FallbackQuery("調べて"));
        Assert.Equal("東京の天気", WebSearch.FallbackQuery("ねえ、東京の天気を調べて！"));
        Assert.Equal(["https://example.com/a"], WebSearch.FindUrls("これ https://example.com/a。見て"));
    }

    [Fact]
    public void ParsesDuckDuckGo()
    {
        var html = """
            <div class="result results_links web-result"><div class="links_main links_deep result__body">
              <h2 class="result__title"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Ftenki.jp%2Fforecast&amp;rut=x">東京の天気 - tenki.jp</a></h2>
              <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Ftenki.jp%2Fforecast">明日は <b>晴れ</b></a>
            </div></div>
            <div class="result result--ad"><div class="result__body"><h2><a class="result__a" href="https://duckduckgo.com/y.js?ad=1">広告</a></h2></div></div>
            <div class="result"><div class="result__body"><h2><a class="result__a" href="https://weather.example/b">天気2</a></h2></div></div>
            """;
        var r = WebSearch.ParseDuckDuckGo(html, 5);
        Assert.Equal(2, r.Count);
        Assert.Equal("https://tenki.jp/forecast", r[0].Url);
        Assert.Equal("明日は 晴れ", r[0].Snippet);
        Assert.Equal("tenki.jp", r[0].Site);
    }

    [Fact]
    public void ParsesMojeek()
    {
        var html = """<ul class="results-standard"><li><h2><a href="https://a.jp/x">A</a></h2><p class="s">説明</p></li></ul>""";
        var r = WebSearch.ParseMojeek(html, 5);
        Assert.Equal(("A", "https://a.jp/x", "説明"), (r[0].Title, r[0].Url, r[0].Snippet));
    }

    [Fact]
    public void DecodesShiftJisFromMeta()
    {
        var sjis = Encoding.GetEncoding("shift_jis");
        var bytes = sjis.GetBytes("<html><head><meta charset=\"Shift_JIS\"></head><body>こんにちは</body></html>");
        Assert.Contains("こんにちは", WebSearch.Decode(bytes, null));
    }

    [Fact]
    public void ExtractsArticle()
    {
        var para = string.Concat(Enumerable.Repeat("今日は晴れて気温が上がり、過ごしやすい一日になるでしょう。", 20));
        var html = $"<html><head><title>天気</title><script>var x=1;</script></head><body><nav>メニュー</nav><article><h1>明日の天気</h1><p>{para}</p><p>{para}</p></article></body></html>";
        var text = WebSearch.ArticleText("https://example.com/a", html);
        Assert.Contains("過ごしやすい", text);
        Assert.DoesNotContain("var x", text);
    }

    [Fact]
    public void ContextAndSources()
    {
        var ctx = new SearchContext("q", [new SearchResult("a", "https://time.is/ja/Japan", ""), new SearchResult("b", "https://ja.example.com/x", "", "本文")]);
        Assert.Equal("<think>出典: https://time.is/ja/Japan / https://ja.example.com/x。</think>", WebSearch.SourcesLine(ctx));
        var c = WebSearch.BuildContext(ctx);
        Assert.Contains("[1] a（time.is/ja/Japan）".Replace("/ja/Japan", ""), c);
        Assert.Contains("本文を読み取れなかった", c);
        Assert.Contains("[2] b（ja.example.com）\n本文", c);
    }
}

public class SpeakerTests
{
    [Fact]
    public async Task SentencesComeWithTheirTextAndSourcesAreNotRead()
    {
        var spoken = new List<string>();
        var played = new List<(string? B64, string? Part)>();
        var done = new TaskCompletionSource();
        var sp = new Speaker((t, _) => { spoken.Add(t); return Task.FromResult<byte[]?>(Encoding.UTF8.GetBytes(t)); },
                             (b, p) => { played.Add((b, p)); if (p is null) done.TrySetResult(); });
        foreach (var piece in new[] { "ちょっと調べるね。", "晴れ", "だって！", "？", "えっと（にこ）\nそれでね。", "<thi", "nk>出典: https://x.example/。</think>" })
            sp.Feed(piece);
        sp.Feed("", final: true);
        sp.Finish();
        await done.Task.WaitAsync(TimeSpan.FromSeconds(5));
        Assert.Equal(["ちょっと調べるね。", "晴れだって！？", "えっと", "それでね。"], spoken);
        Assert.Equal([null, null], played.Skip(4).Select(p => p.B64));
        Assert.Equal("<think>出典: https://x.example/。</think>", played[4].Part);
        Assert.Equal("晴れだって！？", played[1].Part);
        Assert.Equal("うん", Speaker.Spoken("うん（にこっ）[joy] *えへへ*"));
    }

    [Fact]
    public async Task ResetDropsTheRest()
    {
        var gate = new TaskCompletionSource();
        var played = new List<string?>();
        var done = new TaskCompletionSource();
        var sp = new Speaker(async (_, _) => { await gate.Task; return [1]; }, (b, p) => { played.Add(p); if (p is null) done.TrySetResult(); });
        sp.Feed("いち。にい。さん。");
        await Task.Delay(50);
        sp.Reset();
        sp.Finish();
        gate.SetResult();
        await done.Task.WaitAsync(TimeSpan.FromSeconds(5));
        Assert.Equal([null], played);
    }
}

public class ChatSessionTests
{
    static readonly Character Mei = new("めい", "ぱぱ", "あなたはめい。");

    [Fact]
    public async Task CancelAlsoStopsTheOneWaitingInLine()
    {
        var asked = new List<string>();
        var s = new ChatSession(async (m, p, emit, ct) =>
        {
            asked.Add(Msg.LastUserText(m));
            await Task.Delay(Timeout.Infinite, ct);  // 止められるまで返事をしている
            return "";
        }, Mei, null);
        var a = s.SendAsync("A", _ => { });
        var b = s.SendAsync("B", _ => { });  // A の後ろで待つ
        await Task.Delay(100);
        s.Cancel();
        await Task.WhenAll(a, b);
        Assert.Equal(["A"], asked);  // B の返事は作らない
        Assert.Equal(["A", "", "B"], s.History.Select(Msg.TextOf));  // B が言ったことは残す
    }

    [Fact]
    public async Task ImagesAreSentButOnlyTheirCountIsKept()
    {
        using var dir = new TempDir();
        var sent = new List<JsonArray>();
        var path = Path.Combine(dir.Path, "chat.json");
        var s = new ChatSession((m, p, emit, ct) => { sent.Add(m); emit("見えたよ"); return Task.FromResult("見えたよ"); }, Mei, path);
        await s.SendAsync("これ何？", _ => { }, images: ["data:image/jpeg;base64,AAAA"]);
        Assert.Equal(1, Msg.ImageCount(sent[0][^1]));
        Assert.DoesNotContain("AAAA", File.ReadAllText(path));
        await s.SendAsync("ありがとう", _ => { });
        Assert.Equal("これ何？\n（このとき画像を 1 枚見せた）", Msg.TextOf(sent[1][1]));
        Assert.StartsWith("あなたはめい。\n\n会話の決まり", Msg.TextOf(sent[1][0]));
        Assert.Equal(4, new ChatSession((_, _, _, _) => Task.FromResult(""), Mei, path).History.Count);  // 次に開いても続きから
    }

    [Fact]
    public async Task ProactiveIsNotKeptAndBrainDownIsReported()
    {
        var s = new ChatSession((_, _, _, _) => Task.FromResult("ねえねえ"), Mei, null);
        await s.SendAsync("", _ => { }, proactive: true);
        Assert.Empty(s.History);
        var down = new ChatSession((_, _, _, _) => throw new HttpRequestException("down"), Mei, null);
        Assert.Equal(ChatSession.NoBrain, await down.SendAsync("やあ", _ => { }));
    }

    [Fact]
    public async Task CancelKeepsWhatWasSaid()
    {
        var s = new ChatSession(async (_, _, emit, ct) => { emit("ええと"); await Task.Delay(5000, ct); return "x"; }, Mei, null);
        var t = s.SendAsync("ねえ", _ => { });
        await Task.Delay(100);
        s.Cancel();
        Assert.Equal("ええと", await t);
        Assert.Equal("ええと", Msg.TextOf(s.History[^1]));
    }

    [Fact]
    public async Task ProactiveLoopWaitsForQuietAndNoCall()
    {
        var s = new ChatSession((_, _, _, _) => Task.FromResult(""), Mei, null);
        var calls = 0;
        using var stop = new CancellationTokenSource(300);
        await Proactive.LoopAsync(s, 0.000001, () => { calls++; return Task.CompletedTask; }, () => TimeSpan.Zero, () => true, stop.Token,
                                  poll: TimeSpan.FromMilliseconds(20));
        Assert.Equal(0, calls);  // Discord で通話中
        using var stop2 = new CancellationTokenSource(300);
        await Proactive.LoopAsync(s, 0.000001, () => { calls++; return Task.CompletedTask; }, () => TimeSpan.Zero, () => false, stop2.Token,
                                  poll: TimeSpan.FromMilliseconds(20));
        Assert.True(calls > 0);
    }
}

public class SettingsTests
{
    [Fact]
    public void DefaultsUpdateAndOldKeys()
    {
        using var dir = new TempDir();
        var path = Path.Combine(dir.Path, "maid.settings.json");
        var s = new Settings(path, """{"voice":"コハク/あまあま","mic":true,"embed":true,"search_filler":"ちょっと調べるね。"}""");
        Assert.True(File.Exists(path));
        File.WriteAllText(path, """{"voice":"まい","search_filler":"","model":"old","embed_model":""}""");
        Assert.Equal("まい", s.Voice);
        Assert.Equal("", s.SearchFiller);  // 空は「言わない」
        Assert.True(s.Mic);
        Assert.False(s.Embed);           // 前の版の "embed_model": "" を引き継ぐ
        s.Update(new Dictionary<string, JsonNode?> { ["mic"] = false });
        Assert.False(s.Mic);
        Assert.Contains("\"model\": \"old\"", File.ReadAllText(path));
    }

    [Fact]
    public void CharacterAndIcons()
    {
        using var dir = new TempDir();
        var path = Path.Combine(dir.Path, "character.yaml");
        var c = Character.Load(path);
        Assert.Equal("メイド", c.Name);
        Assert.Contains("話し方のルール", c.Persona);
        File.WriteAllText(path, "name: めい\nhuman: ぱぱ\npersona: |\n  あなたはめい。\n\n  - 短く話す: 大事\n");
        Assert.Equal(new Character("めい", "ぱぱ", "あなたはめい。\n\n- 短く話す: 大事"), Character.Load(path));

        var icons = new Icons(Path.Combine(dir.Path, "icons"));
        Assert.Null(icons.DataUrl("user"));
        var png = "data:image/png;base64," + Convert.ToBase64String([137, 80, 78, 71]);
        Assert.Equal(png, icons.Save("user", png));
        Assert.Throws<ArgumentException>(() => icons.Save("user", "data:text/plain;base64,AA=="));
        icons.Remove("user");
        Assert.Null(icons.Find("user"));
    }
}
