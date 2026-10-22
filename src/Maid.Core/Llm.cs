using System.Net.Http.Json;
using System.Runtime.CompilerServices;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace Maid;

/// <summary>頭脳（llama-server の OpenAI 互換 API）。考えるモードは llama-server の起動時に切っている（--reasoning-budget 0）。</summary>
public class Llm
{
    readonly HttpClient http;

    public Llm(string baseUrl, HttpMessageHandler? handler = null)
    {
        http = new HttpClient(handler ?? new HttpClientHandler()) { BaseAddress = new Uri(baseUrl.TrimEnd('/') + "/"), Timeout = Timeout.InfiniteTimeSpan };
    }

    static JsonObject Request(JsonArray messages, bool stream, int maxTokens, double temperature) => new()
    {
        ["model"] = "maid",
        ["messages"] = messages.DeepClone(),
        ["stream"] = stream,
        ["temperature"] = temperature,
        ["top_p"] = 0.8,
        ["max_tokens"] = maxTokens,
        ["presence_penalty"] = 0.5,
        ["chat_template_kwargs"] = new JsonObject { ["enable_thinking"] = false },  // 考えてから答えるモードを切る（起動時にも切っている）
    };

    /// <summary>返事を少しずつ受け取る。キャンセルすると接続を切り、頭脳の生成も止まる。</summary>
    public virtual async IAsyncEnumerable<string> StreamAsync(JsonArray messages, [EnumeratorCancellation] CancellationToken ct = default,
                                                              int maxTokens = 300, double temperature = 0.7)
    {
        using var req = new HttpRequestMessage(HttpMethod.Post, "v1/chat/completions")
        {
            Content = JsonContent.Create(Request(messages, true, maxTokens, temperature)),
        };
        using var res = await http.SendAsync(req, HttpCompletionOption.ResponseHeadersRead, ct);
        if (!res.IsSuccessStatusCode)
            throw new HttpRequestException($"頭脳がエラーを返しました ({(int)res.StatusCode}): {Trim(await res.Content.ReadAsStringAsync(ct))}");
        using var reader = new StreamReader(await res.Content.ReadAsStreamAsync(ct), Encoding.UTF8);
        while (await reader.ReadLineAsync(ct) is { } line)
        {
            line = line.Trim();
            if (!line.StartsWith("data:") || line == "data: [DONE]")
                continue;
            string piece;
            try
            {
                piece = ContentOf(JsonNode.Parse(line[5..]));
            }
            catch (JsonException)
            {
                continue;
            }
            if (piece.Length > 0)
                yield return piece;
        }
    }

    /// <summary>ふつうの（ストリーミングしない）呼び出し。記憶の整理や検索語づくりに使う。</summary>
    public virtual async Task<string> CompleteAsync(JsonArray messages, int maxTokens, double temperature, CancellationToken ct = default)
    {
        using var res = await http.PostAsJsonAsync("v1/chat/completions", Request(messages, false, maxTokens, temperature), ct);
        var body = await res.Content.ReadAsStringAsync(ct);
        if (!res.IsSuccessStatusCode)
            throw new HttpRequestException($"頭脳がエラーを返しました ({(int)res.StatusCode}): {Trim(body)}");
        return ContentOf(JsonNode.Parse(body));
    }

    public static string ContentOf(JsonNode? ev)
    {
        var sb = new StringBuilder();
        foreach (var c in ev?["choices"]?.AsArray() ?? [])
        {
            var part = c?["delta"] ?? c?["message"];
            if (part?["content"] is JsonValue v && v.TryGetValue(out string? s))
                sb.Append(s);
        }
        return sb.ToString();
    }

    static string Trim(string s) => s.Length > 300 ? s[..300] : s;
}

/// <summary>意味のベクトルを作る（2 本目の llama-server。--embedding で bge-m3 を CPU で動かす）。</summary>
public class Embedder
{
    readonly HttpClient http;

    public Embedder(string baseUrl, HttpMessageHandler? handler = null)
    {
        http = new HttpClient(handler ?? new HttpClientHandler()) { BaseAddress = new Uri(baseUrl.TrimEnd('/') + "/"), Timeout = TimeSpan.FromMinutes(2) };
    }

    public virtual async Task<float[][]?> EmbedAsync(IReadOnlyList<string> texts, CancellationToken ct = default)
    {
        using var res = await http.PostAsJsonAsync("v1/embeddings", new JsonObject { ["input"] = new JsonArray(texts.Select(t => (JsonNode)t).ToArray()) }, ct);
        if (!res.IsSuccessStatusCode)
            return null;
        var data = JsonNode.Parse(await res.Content.ReadAsStringAsync(ct))?["data"]?.AsArray();
        return data?.OrderBy(d => d?["index"]?.GetValue<int>() ?? 0)
                   .Select(d => d!["embedding"]!.AsArray().Select(x => x!.GetValue<float>()).ToArray())
                   .ToArray();
    }
}
