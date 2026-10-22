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

    /// <summary>最後の相手の発言のすぐ前に足す（messages を書き換える）。</summary>
    public static void InsertBeforeLastUser(JsonArray messages, JsonObject message)
    {
        var k = -1;
        for (var i = messages.Count - 1; i >= 0 && k < 0; i--)
            if (Role(messages[i]) == "user")
                k = i;
        messages.Insert(k < 0 ? messages.Count : k, message);
    }

    /// <summary>最初の system メッセージの末尾に一言足す（無ければ先頭に足す。messages を書き換える）。</summary>
    public static void AddNote(JsonArray messages, string note)
    {
        var sys = messages.FirstOrDefault(m => Role(m) == "system" && m!["content"] is JsonValue);
        if (sys is null)
            messages.Insert(0, Make("system", note));
        else
            sys["content"] = TextOf(sys).TrimEnd() + "\n\n" + note;
    }
}
