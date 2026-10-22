using System.Text.RegularExpressions;

namespace Maid;

/// <summary>キャラと自分のアイコン（data\icons\character.png など）。</summary>
public sealed class Icons
{
    public static readonly string[] Kinds = ["character", "user"];
    static readonly Dictionary<string, string> Types = new()
    {
        ["image/png"] = "png", ["image/jpeg"] = "jpg", ["image/webp"] = "webp", ["image/gif"] = "gif",
    };
    const int MaxBytes = 8_000_000;
    readonly string dir;

    public Icons(string dir) => this.dir = dir;

    public string? Find(string kind) =>
        Types.Values.Select(ext => Path.Combine(dir, $"{kind}.{ext}")).FirstOrDefault(File.Exists);

    /// <summary>画面に渡す {"character": data URL か null, "user": …}。</summary>
    public System.Text.Json.Nodes.JsonObject ToJson() => new(Kinds.Select(k => KeyValuePair.Create(k, (System.Text.Json.Nodes.JsonNode?)DataUrl(k))));

    public string? DataUrl(string kind)
    {
        var p = Find(kind);
        if (p is null)
            return null;
        var mime = Types.First(t => t.Value == Path.GetExtension(p)[1..]).Key;
        return $"data:{mime};base64,{Convert.ToBase64String(File.ReadAllBytes(p))}";
    }

    /// <summary>画面で選んだ画像（data URL）を保存する。戻り値は表示用の data URL。</summary>
    public string? Save(string kind, string dataUrl)
    {
        if (!Kinds.Contains(kind))
            throw new ArgumentException(kind);
        var m = Regex.Match(dataUrl, @"^data:(image/[a-z]+);base64,(.*)$", RegexOptions.Singleline);
        if (!m.Success || !Types.TryGetValue(m.Groups[1].Value, out var ext))
            throw new ArgumentException("PNG / JPEG / WebP / GIF の画像を選んでください");
        var data = Convert.FromBase64String(m.Groups[2].Value);
        if (data.Length > MaxBytes)
            throw new ArgumentException("画像が大きすぎます（8MB まで）");
        Remove(kind);
        Directory.CreateDirectory(dir);
        File.WriteAllBytes(Path.Combine(dir, $"{kind}.{ext}"), data);
        return DataUrl(kind);
    }

    public void Remove(string kind)
    {
        while (Find(kind) is { } p)
            File.Delete(p);
    }
}
