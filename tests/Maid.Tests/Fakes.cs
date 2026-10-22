using System.Runtime.CompilerServices;
using System.Text.Json.Nodes;
using Maid;

namespace Maid.Tests;

/// <summary>頭脳の偽物。返事は決めた文字列を少しずつ返し、受け取った会話を覚えておく。</summary>
sealed class FakeLlm : Llm
{
    public FakeLlm() : base("http://127.0.0.1:1") { }
    public List<JsonArray> Streamed { get; } = [];
    public List<JsonArray> Completed { get; } = [];
    public Func<JsonArray, IEnumerable<string>> Reply { get; set; } = _ => ["うん、", "いいよ！"];
    public string Answer { get; set; } = "東京 明日 天気";

    public override async IAsyncEnumerable<string> StreamAsync(JsonArray messages, [EnumeratorCancellation] CancellationToken ct = default,
                                                               int maxTokens = 300, double temperature = 0.7)
    {
        Streamed.Add((JsonArray)messages.DeepClone());
        foreach (var piece in Reply(messages))
        {
            ct.ThrowIfCancellationRequested();
            await Task.Yield();
            yield return piece;
        }
    }

    public override Task<string> CompleteAsync(JsonArray messages, int maxTokens, double temperature, CancellationToken ct = default)
    {
        Completed.Add((JsonArray)messages.DeepClone());
        return Task.FromResult(Answer);
    }
}

sealed class FakeSearch : WebSearch
{
    public List<string> Queries { get; } = [];

    public override Task<SearchContext> SearchAsync(string query, int pages = 3, CancellationToken ct = default)
    {
        Queries.Add(query);
        return Task.FromResult(new SearchContext(query, [new SearchResult("天気", "https://tenki.example/a", "晴れ", "明日は晴れ")]));
    }

    public override Task<SearchContext> ReadPagesAsync(IEnumerable<string> urls, CancellationToken ct = default) =>
        Task.FromResult(new SearchContext(SearchContext.PastedPages, urls.Select(u => new SearchResult(u, u, "", "ページの本文")).ToList()));
}

sealed class TempDir : IDisposable
{
    public string Path { get; } = Directory.CreateTempSubdirectory("maid-test-").FullName;
    public void Dispose() => Directory.Delete(Path, true);
}
