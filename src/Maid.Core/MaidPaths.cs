namespace Maid;

/// <summary>maid フォルダの中の場所。全部この中に置き、外には書かない。</summary>
public sealed class MaidPaths
{
    public MaidPaths(string root) => Root = Path.GetFullPath(root);

    public string Root { get; }
    public string Runtime => Path.Combine(Root, "runtime");
    public string Data => Path.Combine(Root, "data");
    public string Logs => Path.Combine(Runtime, "logs");
    public string Downloads => Path.Combine(Runtime, "downloads");
    public string Models => Path.Combine(Runtime, "models");
    public string Llama => Path.Combine(Runtime, "llama");
    public string LlamaServer => Path.Combine(Llama, "llama-server.exe");
    public string Aivis => Path.Combine(Runtime, "aivisspeech-engine");
    public string WebView => Path.Combine(Runtime, "webview");
    public string Asr => Path.Combine(Models, "sherpa-onnx-zipformer-ja-reazonspeech-2024-08-01");
    public string SileroVad => Path.Combine(Models, "silero_vad.onnx");
    public string SettingsFile => Path.Combine(Root, "maid.settings.json");
    public string CharacterFile => Path.Combine(Root, "character.yaml");
    public string Icons => Path.Combine(Data, "icons");
    public string ChatHistory => Path.Combine(Data, "chat.json");

    public string Model(string file) => Path.Combine(Models, file);
}
