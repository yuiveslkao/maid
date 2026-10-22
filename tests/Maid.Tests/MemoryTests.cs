using System.Text.Json.Nodes;
using Maid;
using Xunit;

namespace Maid.Tests;

public class MemoryTests
{
    sealed class Brainy
    {
        public Func<string, string> Answer = _ => "なし";
        public List<string> Prompts { get; } = [];

        public Task<string> Ask(JsonArray messages, int maxTokens)
        {
            var system = Msg.TextOf(messages[0]);
            Prompts.Add(system);
            return Task.FromResult(Answer(system));
        }
    }

    [Fact]
    public async Task FactsGoToNowAndProfile()
    {
        using var dir = new TempDir();
        var b = new Brainy();
        b.Answer = s => s.StartsWith("あなたは記憶係です。次は") ? "- 状況: 熱がある\n約束：今度オムライスを作る\nプロフィール: 辛いものが苦手\n近況: なし"
                      : s.StartsWith("あなたは記憶係です。【今") ? "- 辛いものが苦手" : "今日は熱の話をした。";
        using var m = new Memory(dir.Path, b.Ask);
        var t0 = DateTime.Now.AddHours(-2);
        m.Record("熱があるんだ", "大丈夫？", when: t0);
        Assert.True(await m.MaybeConsolidateAsync(t0.AddMinutes(31)));
        var now = m.NowItems();
        Assert.Contains(now, it => it.Kind == "状況" && it.Text == "熱がある");
        Assert.Contains(now, it => it.Kind == "約束" && it.Text == "今度オムライスを作る");
        Assert.DoesNotContain(now, it => it.Kind == "近況");  // 「なし」は書かない
        Assert.Equal("- 辛いものが苦手", File.ReadAllText(Path.Combine(dir.Path, "PROFILE.md")).Trim());
        Assert.Single(m.Episodes());
        Assert.Empty(m.Pending());

        // 「治った」と話せば、NOW.md からも消える
        b.Answer = s => s.StartsWith("あなたは記憶係です。次は") ? "済み: 熱がある" : "熱が治った話。";
        m.Record("熱治ったよ", "よかった！", when: DateTime.Now.AddMinutes(-40));
        await m.ConsolidateAsync();
        Assert.DoesNotContain(m.NowItems(), it => it.Text == "熱がある");
        var ctx = await m.ContextAsync("オムライス");
        Assert.Contains("相手について:\n- 辛いものが苦手", ctx);
        Assert.Contains("[約束] 今度オムライスを作る", ctx);
    }

    [Fact]
    public void SessionsSplitAtLongGaps()
    {
        using var dir = new TempDir();
        using var m = new Memory(dir.Path, (_, _) => Task.FromResult(""));
        var t = new DateTime(2026, 1, 1, 12, 0, 0);
        var s = m.Sessions([new(t, "a", "b"), new(t.AddMinutes(10), "c", "d"), new(t.AddMinutes(50), "e", "f")]);
        Assert.Equal([2, 1], s.Select(x => x.Count));
    }

    [Fact]
    public void AgoAndOverlap()
    {
        var now = new DateTime(2026, 1, 3, 12, 0, 0);
        Assert.Equal("さっき", Memory.Ago(now.AddMinutes(-5), now));
        Assert.Equal("3 時間前", Memory.Ago(now.AddHours(-3), now));
        Assert.Equal("昨日", Memory.Ago(now.AddHours(-30), now));
        Assert.Equal("2 日前", Memory.Ago(now.AddDays(-2), now));
        Assert.Equal(1.0, Memory.Overlap("熱がある", "昨日から熱がある"));
        Assert.Equal(0.0, Memory.Overlap("", "x"));
    }

    [Fact]
    public void ProactiveTopicPrefersPromises()
    {
        using var dir = new TempDir();
        using var m = new Memory(dir.Path, (_, _) => Task.FromResult(""));
        m.WriteNow([new NowItem("約束", "今度オムライスを作る", DateTime.Now)]);
        var topics = Enumerable.Range(0, 200).Select(i => m.Topic(new Random(i))).ToList();
        Assert.Contains(topics, t => t.Contains("オムライス"));
    }

    [Fact]
    public async Task RelatedUsesVectorsWhenAvailable()
    {
        using var dir = new TempDir();
        Func<IReadOnlyList<string>, Task<float[][]?>> embed = texts =>
            Task.FromResult<float[][]?>(texts.Select(t => t.Contains("猫") ? new[] { 1f, 0f } : new[] { 0f, 1f }).ToArray());
        using var m = new Memory(dir.Path, (_, _) => Task.FromResult(""), embed);
        await m.AddEpisodeAsync(DateTime.Now, DateTime.Now, "相手は猫を飼いはじめた。");
        await m.AddEpisodeAsync(DateTime.Now, DateTime.Now, "相手は仕事で忙しい。");
        var rel = await m.RelatedAsync("猫ちゃん元気？");
        Assert.Single(rel);
        Assert.Contains("猫", rel[0].Summary);
    }
}
