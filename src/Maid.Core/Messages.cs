using System.Text.Json.Nodes;

namespace Maid;

/// <summary>OpenAI 形式のメッセージ（{"role", "content"}。content は文字列か、文字と画像の部品の配列）。</summary>
public static class Msg
{
    public static JsonObject Make(string role, string content) => new() { ["role"] = role, ["content"] = content };

    /// <summary>文字と画像（data URL）の発言。</summary>
    public static JsonObject WithImages(string text, IEnumerable<string> images)
    {
        var parts = new JsonArray { new JsonObject { ["type"] = "text", ["text"] = text } };
        foreach (var url in images)
            parts.Add(new JsonObject { ["type"] = "image_url", ["image_url"] = new JsonObject { ["url"] = url } });
        return new JsonObject { ["role"] = "user", ["content"] = parts };
    }

    public static string Role(JsonNode? m) => m?["role"]?.GetValue<string>() ?? "";

    public static string TextOf(JsonNode? m)
    {
        var c = m?["content"];
        if (c is JsonArray parts)
            return string.Concat(parts.Select(p => p?["type"]?.GetValue<string>() == "text" ? p["text"]?.GetValue<string>() ?? "" : ""));
        return c is JsonValue v && v.TryGetValue(out string? s) ? s : "";
    }

    public static int ImageCount(JsonNode? m) =>
        m?["content"] is JsonArray parts ? parts.Count(p => p?["type"]?.GetValue<string>() == "image_url") : 0;

    public static string LastUserText(JsonArray messages) =>
        messages.LastOrDefault(m => Role(m) == "user") is { } u ? TextOf(u) : "";

    public static JsonArray Copy(IEnumerable<JsonNode?> messages) => new(messages.Select(m => m?.DeepClone()).ToArray());

    /// <summary>最後の相手の発言のすぐ前に system メッセージを足す。</summary>
    public static JsonArray InsertBeforeLastUser(JsonArray messages, string text)
    {
        var list = messages.Select(m => m?.DeepClone()).ToList();
        var k = list.FindLastIndex(m => Role(m) == "user");
        list.Insert(k < 0 ? list.Count : k, Make("system", text));
        return new JsonArray(list.ToArray());
    }

    /// <summary>最初の system メッセージの末尾に一言足す（無ければ先頭に足す）。</summary>
    public static JsonArray AddNote(JsonArray messages, string note)
    {
        var list = messages.Select(m => m?.DeepClone()).ToList();
        var k = list.FindIndex(m => Role(m) == "system" && m!["content"] is JsonValue);
        if (k < 0)
            list.Insert(0, Make("system", note));
        else
            list[k]!["content"] = TextOf(list[k]).TrimEnd() + "\n\n" + note;
        return new JsonArray(list.ToArray());
    }
}
