using YamlDotNet.RepresentationModel;

namespace Maid;

/// <summary>キャラの名前・相手の名前・性格（character.yaml）。</summary>
public sealed record Character(string Name, string Human, string Persona)
{
    public static readonly Character Fallback = new("メイド", "ご主人様", "あなたは親しみやすいアシスタントです。日本語の話し言葉で、短く答えてください。");

    /// <summary>character.yaml を読む。無ければ雛形（config\character.default.yaml）をコピーして作る。</summary>
    public static Character Load(string path)
    {
        try
        {
            if (!File.Exists(path))
                File.WriteAllText(path, Resources.Text("character.default.yaml"));
            var yaml = new YamlStream();
            using (var r = new StreamReader(path))
                yaml.Load(r);
            if (yaml.Documents.Count == 0 || yaml.Documents[0].RootNode is not YamlMappingNode map)
                return Fallback;
            string Get(string key, string fallback) =>
                map.Children.TryGetValue(new YamlScalarNode(key), out var v) && v is YamlScalarNode s && !string.IsNullOrWhiteSpace(s.Value)
                    ? s.Value!.Trim()
                    : fallback;
            return new Character(Get("name", Fallback.Name), Get("human", Fallback.Human), Get("persona", Fallback.Persona));
        }
        catch (Exception e)
        {
            Log.Warn($"キャラの設定を読めませんでした（{path}）: {e.Message}");
            return Fallback;
        }
    }
}
