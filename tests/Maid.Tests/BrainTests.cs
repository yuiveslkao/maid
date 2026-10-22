using System.Text.Json.Nodes;
using Maid;
using Xunit;

namespace Maid.Tests;

public class BrainTests
{
    static JsonArray Talk(params (string Role, string Text)[] ms) =>
        new(ms.Select(m => (JsonNode)Msg.Make(m.Role, m.Text)).ToArray());

    [Fact]
    public async Task ClockNoteGoesRightBeforeTheLastWords()
    {
        var llm = new FakeLlm();
        var brain = new Brain(llm, new FakeSearch(), null, "auto", "ちょっと調べるね。");
        var said = new List<string>();
        var all = await brain.ReplyAsync(Talk(("system", "あなたはめい。"), ("user", "おはよう")), false, said.Add);
        Assert.Equal("うん、いいよ！", all);
        Assert.Equal(["うん、", "いいよ！"], said);
        var sent = llm.Streamed[0];
        Assert.StartsWith("現在の日時は", Msg.TextOf(sent[^2]));
        Assert.Equal("おはよう", Msg.TextOf(sent[^1]));
        Assert.Contains("Web 検索の結果が渡されたときは", Msg.TextOf(sent[0]));  // 検索の補足はキャラ設定の後ろ
    }

    [Fact]
    public async Task NotesGoBeforeTheLastWordsSoTheEarlierTalkStaysTheSame()
    {
        using var dir = new TempDir();
        File.WriteAllText(Path.Combine(dir.Path, "PROFILE.md"), "- 納豆が好き");
        using var memory = new Memory(dir.Path, (_, _) => Task.FromResult(""));
        var llm = new FakeLlm();
        var brain = new Brain(llm, new FakeSearch(), memory, "off", "");
        await brain.ReplyAsync(Talk(("system", "あなたはめい。"), ("user", "おはよう")), false, _ => { });
        await brain.ReplyAsync(Talk(("system", "あなたはめい。"), ("user", "おはよう"), ("assistant", "おはよう！"), ("user", "ごはん食べた？")), false, _ => { });
        var (first, second) = (llm.Streamed[0], llm.Streamed[1]);
        Assert.Equal("あなたはめい。", Msg.TextOf(second[0]));  // キャラ設定は毎回同じ
        Assert.Equal("おはよう", Msg.TextOf(second[1]));        // 前の会話の間にはメモを残さない
        Assert.Contains("納豆が好き", Msg.TextOf(second[^3]));
        Assert.StartsWith("現在の日時は", Msg.TextOf(second[^2]));
        Assert.Equal("ごはん食べた？", Msg.TextOf(second[^1]));
        Assert.Equal(4, first.Count);
    }

    [Fact]
    public async Task SearchSaysFillerFirstAndAddsSources()
    {
        var llm = new FakeLlm();
        var search = new FakeSearch();
        var brain = new Brain(llm, search, null, "auto", "ちょっと調べるね。");
        var said = new List<string>();
        await brain.ReplyAsync(Talk(("system", "p"), ("user", "明日の天気調べて")), false, said.Add);
        Assert.Equal("ちょっと調べるね。", said[0]);
        Assert.Equal(["東京 明日 天気"], search.Queries);
        Assert.Equal("<think>出典: https://tenki.example/a。</think>", said[^1]);
        Assert.Contains("【Web 検索の結果】", Msg.TextOf(llm.Streamed[0][^2]));
        // 続けての質問には前回の結果を渡す
        await brain.ReplyAsync(Talk(("system", "p"), ("user", "それって雨降る？")), false, _ => { });
        Assert.Contains("前回の結果", Msg.TextOf(llm.Streamed[1][^2]));
    }

    [Fact]
    public async Task PastedUrlIsReadInsteadOfSearching()
    {
        var llm = new FakeLlm();
        var search = new FakeSearch();
        var brain = new Brain(llm, search, null, "auto", "");
        await brain.ReplyAsync(Talk(("system", "p"), ("user", "これ読んで https://example.com/x。")), false, _ => { });
        Assert.Empty(search.Queries);
        Assert.Contains("相手が貼ったページの内容", Msg.TextOf(llm.Streamed[0][^2]));
    }

    [Fact]
    public async Task ClockQuestionIsNotSearched()
    {
        var search = new FakeSearch();
        await new Brain(new FakeLlm(), search, null, "auto", "").ReplyAsync(Talk(("system", "p"), ("user", "今何時？")), false, _ => { });
        Assert.Empty(search.Queries);
    }

    [Fact]
    public async Task LoopIsCut()
    {
        var llm = new FakeLlm { Reply = _ => Enumerable.Repeat("ぱぱ、", 100) };
        var said = new List<string>();
        await new Brain(llm, new FakeSearch(), null, "off", "").ReplyAsync(Talk(("system", "p"), ("user", "ねえ")), false, said.Add);
        Assert.True(said.Count < 20);
        Assert.True(Brain.IsLooping(string.Concat(Enumerable.Repeat("ぱぱ、", 10))));
        Assert.False(Brain.IsLooping("うん、いいよ！また明日ね。"));
    }

    [Fact]
    public async Task ProactiveSpeechIsRememberedForTheNextReply()
    {
        var llm = new FakeLlm { Reply = _ => ["ねえねえ、テストどうだった？"] };
        var brain = new Brain(llm, new FakeSearch(), null, "off", "");
        await brain.ReplyAsync(Talk(("system", "p")), true, _ => { });
        Assert.Contains("あなたから話しかける番", Msg.TextOf(llm.Streamed[0][^1]));
        llm.Reply = _ => ["よかったね！"];
        await brain.ReplyAsync(Talk(("system", "p"), ("user", "ばっちり")), false, _ => { });
        var sent = llm.Streamed[1];
        Assert.Equal("ねえねえ、テストどうだった？", Msg.TextOf(sent.First(m => Msg.Role(m) == "assistant")));
    }

    [Fact]
    public void HistoryLosesFillerAndSources()
    {
        var brain = new Brain(new FakeLlm(), new FakeSearch(), null, "auto", "ちょっと調べるね。");
        var clean = brain.CleanHistory(Talk(("assistant", "ちょっと調べるね。晴れだよ！<think>出典: https://a.jp/x。</think>")));
        Assert.Equal("晴れだよ！", Msg.TextOf(clean[0]));
    }
}
