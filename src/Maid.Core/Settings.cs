using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace Maid;

/// <summary>maid.settings.json。書いていない項目は既定値（config\maid.settings.default.json を exe に埋め込んだもの）。</summary>
public sealed class Settings
{
    static readonly JsonSerializerOptions Pretty = new() { WriteIndented = true, Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping };
    readonly string path;
    readonly JsonObject defaults;
    readonly object gate = new();
    (JsonObject Json, DateTime At)? cache;  // 読むたびにファイルを開かない（手で書き換えたら、更新日時が変わるので読み直す）

    public Settings(string path, string? defaultsJson = null)
    {
        this.path = path;
        defaults = (JsonNode.Parse(defaultsJson ?? Resources.Text("maid.settings.default.json")) as JsonObject) ?? new JsonObject();
        if (!File.Exists(path))
            File.WriteAllText(path, defaults.ToJsonString(Pretty) + "\n");
    }

    JsonObject ReadUser()
    {
        try
        {
            return JsonNode.Parse(File.ReadAllText(path)) as JsonObject ?? new JsonObject();
        }
        catch (Exception e) when (e is IOException or JsonException)
        {
            return new JsonObject();
        }
    }

    /// <summary>ファイルに書いてある値（gate の中で呼ぶ）。</summary>
    JsonObject User()
    {
        var at = File.Exists(path) ? File.GetLastWriteTimeUtc(path) : DateTime.MinValue;
        if (cache is not { } c || c.At != at)
            cache = c = (ReadUser(), at);
        return c.Json;
    }

    JsonNode? Get(string key)
    {
        lock (gate)
        {
            var user = User();
            return user[key] is { } v && !(v is JsonValue jv && jv.TryGetValue(out string? s) && s == "" && key != "search_filler")
                ? v.DeepClone()
                : defaults[key]?.DeepClone();
        }
    }

    public string Str(string key) => Get(key) is { } n ? n is JsonValue v && v.TryGetValue(out string? s) ? s : n.ToString() : "";
    public bool Bool(string key) => Get(key) is JsonValue v && v.TryGetValue(out bool b) ? b : false;
    public double Num(string key) => Get(key) is JsonValue v && v.TryGetValue(out double d) ? d : 0;

    public string ModelRepo => Str("model_repo");
    public string ModelFile => Str("model_file");
    public string MmprojFile => Str("mmproj_file");
    public int Context => (int)Num("context");
    public string Voice => Str("voice");
    public double Speed => Num("speed") is > 0 and var s ? s : 1.0;
    public bool Mic => Bool("mic");
    public bool VoiceReply => Bool("voice_reply");
    public int SilenceMs => (int)Num("silence_ms");
    public string Search => Str("search");
    public string SearchFiller => Str("search_filler");
    public bool Memory => Bool("memory");
    public double ProactiveMinutes => Num("proactive_minutes");
    // 前の版の「"embed_model": ""」（記憶を探すモデルを使わない）も引き継ぐ
    public bool Embed => Bool("embed") && !LegacyNoEmbed();

    bool LegacyNoEmbed()
    {
        lock (gate)
            return User()["embed_model"] is JsonValue v && v.TryGetValue(out string? s) && s == "";
    }
    public string EmbedRepo => Str("embed_repo");
    public string EmbedFile => Str("embed_file");

    /// <summary>変えた項目だけを書き足す（ほかの項目や順番はそのまま）。</summary>
    public void Update(IDictionary<string, JsonNode?> values)
    {
        lock (gate)
        {
            var user = ReadUser();
            foreach (var (k, v) in values)
                user[k] = v?.DeepClone();
            File.WriteAllText(path, user.ToJsonString(Pretty) + "\n");
            cache = null;
        }
    }
}
