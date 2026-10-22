using System.Diagnostics;
using System.IO.Compression;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace Maid;

/// <summary>
/// 初回のセットアップ。足りないものだけを maid フォルダの runtime\ にダウンロードする（インストーラーは使わない）。
/// 前の版（Ollama）が持っているモデルがあれば、ダウンロードせずに引き継ぐ。
/// </summary>
public sealed class Setup
{
    public sealed record Need(string Key, string Title, string Size);

    /// <summary>使う llama.cpp の版（動くのを確かめた版）。</summary>
    public const string LlamaTag = "b11819";
    const string ReazonName = "sherpa-onnx-zipformer-ja-reazonspeech-2024-08-01";
    static readonly HttpClient Http = MakeHttp();
    readonly MaidPaths paths;
    readonly Settings settings;
    readonly Action<string, string, double?> progress;

    /// <param name="progress">(項目, 説明, 進み具合 0〜1 か null) を画面に出す。</param>
    public Setup(MaidPaths paths, Settings settings, Action<string, string, double?> progress)
    {
        this.paths = paths;
        this.settings = settings;
        this.progress = progress;
    }

    static HttpClient MakeHttp()
    {
        var h = new HttpClient { Timeout = Timeout.InfiniteTimeSpan };
        h.DefaultRequestHeaders.UserAgent.ParseAdd("maid/0.1 (+https://github.com/yuiveslkao/maid)");
        return h;
    }

    /// <summary>今の設定で動かすのに足りないもの。</summary>
    public static List<Need> Missing(MaidPaths p, Settings s)
    {
        var out_ = new List<Need>();
        if (!File.Exists(p.LlamaServer))
            out_.Add(new("llama", "頭脳を動かす llama.cpp（CUDA 版）", "約 700MB"));
        if (!File.Exists(p.Model(s.ModelFile)) || !File.Exists(p.Model(s.MmprojFile)))
            out_.Add(new("model", $"頭脳のモデル（{s.ModelFile}）", "約 5GB"));
        if (s.Memory && s.Embed && !File.Exists(p.Model(s.EmbedFile)))
            out_.Add(new("embed", "記憶を探すモデル（bge-m3）", "約 0.6〜1.2GB"));
        if (s.Mic && !Recognizer.AsrFiles.All(f => File.Exists(Path.Combine(p.Asr, f))))
            out_.Add(new("asr", "声を文字にするモデル（ReazonSpeech）", "約 700MB"));
        if (s.Mic && !File.Exists(p.SileroVad))
            out_.Add(new("vad", "話し終わりを見つけるモデル（Silero VAD）", "約 2MB"));
        if (s.VoiceReply && Parts.FindAivis(p) is null)
            out_.Add(new("voice", "声（AivisSpeech Engine）", "約 1〜2GB"));
        return out_;
    }

    public async Task RunAsync(CancellationToken ct)
    {
        Directory.CreateDirectory(paths.Models);
        Directory.CreateDirectory(paths.Downloads);
        foreach (var need in Missing(paths, settings))
        {
            progress(need.Key, "準備しています…", null);
            switch (need.Key)
            {
                case "llama": await InstallLlamaAsync(ct); break;
                case "model": await InstallModelAsync(ct); break;
                case "embed": await InstallEmbedAsync(ct); break;
                case "asr": await InstallAsrAsync(ct); break;
                case "vad": await DownloadAsync("vad", "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx", paths.SileroVad, ct); break;
                case "voice": await InstallVoiceAsync(ct); break;
            }
            progress(need.Key, "完了", 1);
        }
        TryDelete(paths.Downloads);
    }

    // ---------------------------------------------------------------- llama.cpp
    async Task InstallLlamaAsync(CancellationToken ct)
    {
        // 本体と CUDA の DLL は別の zip。CUDA 12 版を選ぶ（古めのドライバーでも動く）。無ければ CUDA 13 版
        // 版は LlamaTag に決めてある（新しい版でオプションが変わって動かなくなることがあるため）。
        // 自分でダウンロードした zip を runtime\downloads に置いてあれば、それを使う（新しい版を試したいとき）
        static string? Local(string dir, string pattern) =>
            Directory.EnumerateFiles(dir, "*.zip").Where(f => Regex.IsMatch(Path.GetFileName(f), pattern)).OrderDescending().FirstOrDefault();
        var localMain = Local(paths.Downloads, @"^llama-.+-bin-win-cuda-\d+\.[\d.]+-x64\.zip$");
        var localCudart = Local(paths.Downloads, @"^cudart-llama-.*bin-win-cuda-\d+\.[\d.]+-x64\.zip$");
        if (localMain is not null && localCudart is not null)
        {
            Log.Info($"llama.cpp: 置いてあった {Path.GetFileName(localMain)} と {Path.GetFileName(localCudart)} を使います");
            ExtractLlama(localMain, localCudart, Regex.Match(Path.GetFileName(localMain), @"^llama-(.+?)-bin-").Groups[1].Value);
            return;
        }
        var assets = await GitHubReleaseAsync("ggml-org/llama.cpp", LlamaTag, ct);
        (string Name, string Url) main = default, cudart = default;
        foreach (var major in new[] { "12", "13" })
        {
            main = assets.FirstOrDefault(a => Regex.IsMatch(a.Name, $@"^llama-.+-bin-win-cuda-{major}\.[\d.]+-x64\.zip$"));
            cudart = assets.FirstOrDefault(a => Regex.IsMatch(a.Name, $@"^cudart-llama-.*bin-win-cuda-{major}\.[\d.]+-x64\.zip$"));
            if (main.Url is not null && cudart.Url is not null)
                break;
        }
        if (main.Url is null || cudart.Url is null)
        {
            Log.Warn($"llama.cpp {LlamaTag} のファイル: {string.Join(", ", assets.Select(a => a.Name))}");
            throw new InvalidOperationException($"llama.cpp {LlamaTag} に Windows の CUDA 版が見つかりません（runtime\\logs\\maid.log にファイルの一覧があります）");
        }
        Log.Info($"llama.cpp: {main.Name} と {cudart.Name} を使います");
        var zips = new List<string>();
        foreach (var (name, url) in new[] { main, cudart })
        {
            zips.Add(Path.Combine(paths.Downloads, name));
            await DownloadAsync("llama", url, zips[^1], ct);
        }
        ExtractLlama(zips[0], zips[1], LlamaTag);
    }

    void ExtractLlama(string mainZip, string cudartZip, string tag)
    {
        progress("llama", "展開しています…", null);
        var tmp = paths.Llama + ".tmp";
        TryDelete(tmp);
        ZipFile.ExtractToDirectory(mainZip, tmp, overwriteFiles: true);
        var bin = Path.GetDirectoryName(Directory.EnumerateFiles(tmp, "llama-server.exe", SearchOption.AllDirectories).First())!;
        ZipFile.ExtractToDirectory(cudartZip, bin, overwriteFiles: true);  // CUDA の DLL は llama-server.exe の隣に
        TryDelete(paths.Llama);
        Directory.Move(bin, paths.Llama);
        File.WriteAllText(LlamaVersionFile(paths), tag);
        TryDelete(tmp);
        File.Delete(mainZip);
        File.Delete(cudartZip);
    }

    // ---------------------------------------------------------------- モデル
    async Task InstallModelAsync(CancellationToken ct)
    {
        var quant = Regex.Match(settings.ModelFile, @"(I?Q\d\w*|F16|BF16)", RegexOptions.IgnoreCase).Value;
        if (FromOllama(settings.ModelRepo, quant, settings.ModelFile, settings.MmprojFile))
            return;
        var files = await HuggingFaceFilesAsync(settings.ModelRepo, ct);
        var model = files.FirstOrDefault(f => f.Equals(settings.ModelFile, StringComparison.OrdinalIgnoreCase))
                    ?? files.FirstOrDefault(f => f.EndsWith(".gguf") && !f.StartsWith("mmproj") && f.Contains(quant, StringComparison.OrdinalIgnoreCase))
                    ?? throw new InvalidOperationException($"{settings.ModelRepo} に {settings.ModelFile} が見つかりません");
        var mmproj = files.FirstOrDefault(f => f.Equals(settings.MmprojFile, StringComparison.OrdinalIgnoreCase))
                     ?? files.FirstOrDefault(f => f.StartsWith("mmproj") && f.Contains("F16"))
                     ?? files.FirstOrDefault(f => f.StartsWith("mmproj"));
        if (!File.Exists(paths.Model(settings.ModelFile)))
            await DownloadAsync("model", HuggingFaceUrl(settings.ModelRepo, model), paths.Model(settings.ModelFile), ct);
        if (mmproj is not null && !File.Exists(paths.Model(settings.MmprojFile)))
            await DownloadAsync("model", HuggingFaceUrl(settings.ModelRepo, mmproj), paths.Model(settings.MmprojFile), ct);
    }

    async Task InstallEmbedAsync(CancellationToken ct)
    {
        if (FromOllama("bge-m3", "", settings.EmbedFile, null))
            return;
        var files = await HuggingFaceFilesAsync(settings.EmbedRepo, ct);
        var file = files.FirstOrDefault(f => f.Equals(settings.EmbedFile, StringComparison.OrdinalIgnoreCase))
                   ?? files.FirstOrDefault(f => f.EndsWith(".gguf") && f.Contains("Q8_0"))
                   ?? files.FirstOrDefault(f => f.EndsWith(".gguf"))
                   ?? throw new InvalidOperationException($"{settings.EmbedRepo} に GGUF が見つかりません");
        await DownloadAsync("embed", HuggingFaceUrl(settings.EmbedRepo, file), paths.Model(settings.EmbedFile), ct);
    }

    /// <summary>前の版の Ollama が持っているモデル（中身は GGUF そのもの）を、ダウンロードせずに引き継ぐ。</summary>
    bool FromOllama(string name, string tag, string modelFile, string? projectorFile)
    {
        var root = Path.Combine(paths.Runtime, "ollama-models");
        var manifests = Path.Combine(root, "manifests");
        if (!Directory.Exists(manifests))
            return false;
        foreach (var manifest in Directory.EnumerateFiles(manifests, "*", SearchOption.AllDirectories))
        {
            var rel = Path.GetRelativePath(manifests, manifest).Replace('\\', '/');
            if (!rel.Contains(name, StringComparison.OrdinalIgnoreCase) || (tag.Length > 0 && !rel.EndsWith("/" + tag, StringComparison.OrdinalIgnoreCase)))
                continue;
            JsonArray layers;
            try
            {
                layers = JsonNode.Parse(File.ReadAllText(manifest))?["layers"]?.AsArray() ?? [];
            }
            catch (Exception e) when (e is IOException or System.Text.Json.JsonException)
            {
                continue;
            }
            string? Blob(string kind) => layers.FirstOrDefault(l => l?["mediaType"]?.GetValue<string>() == $"application/vnd.ollama.image.{kind}")?["digest"]
                                              ?.GetValue<string>() is { } d ? Path.Combine(root, "blobs", d.Replace(':', '-')) : null;
            var model = Blob("model");
            if (model is null || !File.Exists(model))
                continue;
            progress(projectorFile is null ? "embed" : "model", "前の版（Ollama）のモデルを引き継いでいます…", null);
            File.Move(model, paths.Model(modelFile), overwrite: true);
            if (projectorFile is not null && Blob("projector") is { } proj && File.Exists(proj))
                File.Move(proj, paths.Model(projectorFile), overwrite: true);
            Log.Info($"Ollama のモデルを引き継ぎました: {rel}");
            return projectorFile is null || File.Exists(paths.Model(projectorFile));
        }
        return false;
    }

    static async Task<List<string>> HuggingFaceFilesAsync(string repo, CancellationToken ct)
    {
        var info = JsonNode.Parse(await Http.GetStringAsync($"https://huggingface.co/api/models/{repo}", ct));
        return info?["siblings"]?.AsArray().Select(s => s?["rfilename"]?.GetValue<string>() ?? "").Where(s => s.Length > 0).ToList() ?? [];
    }

    static string HuggingFaceUrl(string repo, string file) => $"https://huggingface.co/{repo}/resolve/main/{file}";

    // ---------------------------------------------------------------- 耳
    async Task InstallAsrAsync(CancellationToken ct)
    {
        var old = Path.Combine(paths.Runtime, "Open-LLM-VTuber", "models", ReazonName);  // 前の前の版の置き場所
        if (File.Exists(Path.Combine(old, "tokens.txt")) && !Directory.Exists(paths.Asr))
        {
            Directory.Move(old, paths.Asr);
            return;
        }
        var archive = Path.Combine(paths.Downloads, ReazonName + ".tar.bz2");
        await DownloadAsync("asr", $"https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/{ReazonName}.tar.bz2", archive, ct);
        progress("asr", "展開しています…", null);
        // 必要なファイルだけ取り出す（tar.exe は Windows に標準で入っている）
        await RunAsync("tar.exe", ["-xjf", archive, "-C", paths.Models, .. Recognizer.AsrFiles.Select(f => $"*{ReazonName}/{f}")], ct);
        if (!Recognizer.AsrFiles.All(f => File.Exists(Path.Combine(paths.Asr, f))))
            throw new InvalidOperationException("音声認識のモデルを展開できませんでした");
        File.Delete(archive);
    }

    // ---------------------------------------------------------------- 声
    async Task InstallVoiceAsync(CancellationToken ct)
    {
        var sevenZip = Path.Combine(paths.Runtime, "tools", "7zr.exe");  // 7z を展開する公式の単体版（インストール不要）
        if (!File.Exists(sevenZip))
            await DownloadAsync("voice", "https://www.7-zip.org/a/7zr.exe", sevenZip, ct);
        var parts = (await GitHubAssetsAsync("Aivis-Project/AivisSpeech-Engine", ct))
                    .Where(a => a.Name.StartsWith("AivisSpeech-Engine-Windows-x64-") && Regex.IsMatch(a.Name, @"\.7z\.\d+$"))
                    .OrderBy(a => a.Name, StringComparer.Ordinal).ToList();
        if (parts.Count == 0)
            throw new InvalidOperationException("AivisSpeech Engine の Windows 版が最新リリースに見つかりません");
        foreach (var (name, url) in parts)
            await DownloadAsync("voice", url, Path.Combine(paths.Downloads, name), ct);
        progress("voice", "展開しています…", null);
        var tmp = paths.Aivis + ".tmp";
        TryDelete(tmp);
        await RunAsync(sevenZip, ["x", Path.Combine(paths.Downloads, parts[0].Name), $"-o{tmp}", "-y"], ct);
        TryDelete(paths.Aivis);
        Directory.Move(tmp, paths.Aivis);
        foreach (var (name, _) in parts)
            File.Delete(Path.Combine(paths.Downloads, name));
    }

    // ---------------------------------------------------------------- 共通
    /// <summary>新しい順のリリース（タグ, ファイル）。下書きは除く。</summary>
    static async Task<List<(string Name, string Url)>> GitHubReleaseAsync(string repo, string tag, CancellationToken ct) =>
        Assets(JsonNode.Parse(await Http.GetStringAsync($"https://api.github.com/repos/{repo}/releases/tags/{tag}", ct)));

    static async Task<List<(string Name, string Url)>> GitHubAssetsAsync(string repo, CancellationToken ct) =>
        Assets(JsonNode.Parse(await Http.GetStringAsync($"https://api.github.com/repos/{repo}/releases/latest", ct)));

    static List<(string Name, string Url)> Assets(JsonNode? release) =>
        release?["assets"]?.AsArray()
               .Select(a => (a?["name"]?.GetValue<string>() ?? "", a?["browser_download_url"]?.GetValue<string>() ?? ""))
               .Where(a => a.Item1.Length > 0).ToList() ?? [];

    static string LlamaVersionFile(MaidPaths p) => Path.Combine(p.Llama, "version.txt");

    /// <summary>
    /// llama.cpp に今より新しい版があれば、その名前（b12000 など）。確かめるのは 1 日 1 回（runtime\llama-update.txt に覚える）。
    /// 知らせるだけで、更新はしない。
    /// </summary>
    public static async Task<(string Installed, string Latest)?> LlamaUpdateAsync(MaidPaths p, CancellationToken ct)
    {
        if (!File.Exists(p.LlamaServer))
            return null;
        var installed = File.Exists(LlamaVersionFile(p)) ? File.ReadAllText(LlamaVersionFile(p)).Trim() : LlamaTag;
        var cache = Path.Combine(p.Runtime, "llama-update.txt");
        string latest;
        if (File.Exists(cache) && DateTime.Now - File.GetLastWriteTime(cache) < TimeSpan.FromDays(1))
            latest = File.ReadAllText(cache).Trim();
        else
        {
            try
            {
                var tags = JsonNode.Parse(await Http.GetStringAsync("https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=10", ct))?.AsArray() ?? [];
                latest = tags.Select(r => r?["tag_name"]?.GetValue<string>() ?? "").FirstOrDefault(t => Build(t) > 0) ?? "";
                File.WriteAllText(cache, latest);
            }
            catch (Exception e) when (e is HttpRequestException or TaskCanceledException or IOException or System.Text.Json.JsonException)
            {
                Log.Info($"llama.cpp の新しい版を確かめられませんでした: {e.Message}");
                return null;
            }
        }
        return Build(latest) > Build(installed) && Build(installed) > 0 ? (installed, latest) : null;
    }

    static int Build(string tag) => Regex.Match(tag, @"^b(\d+)$") is { Success: true } m ? int.Parse(m.Groups[1].Value) : 0;

    /// <summary>ダウンロードする。途中で切れても、次は続きから（.part に書き、終わったら名前を変える）。</summary>
    async Task DownloadAsync(string key, string url, string dest, CancellationToken ct)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(dest)!);
        var part = dest + ".part";
        var title = Path.GetFileName(dest);
        for (var attempt = 1; ; attempt++)
        {
            try
            {
                var have = File.Exists(part) ? new FileInfo(part).Length : 0;
                using var req = new HttpRequestMessage(HttpMethod.Get, url);
                if (have > 0)
                    req.Headers.Range = new System.Net.Http.Headers.RangeHeaderValue(have, null);
                using var res = await Http.SendAsync(req, HttpCompletionOption.ResponseHeadersRead, ct);
                res.EnsureSuccessStatusCode();
                if (have > 0 && res.StatusCode != System.Net.HttpStatusCode.PartialContent)
                    have = 0;  // 続きからは受け取れなかった
                var total = have + (res.Content.Headers.ContentLength ?? 0);
                await using (var src = await res.Content.ReadAsStreamAsync(ct))
                await using (var file = new FileStream(part, have > 0 ? FileMode.Append : FileMode.Create))
                {
                    var buf = new byte[1 << 20];
                    var done = have;
                    var shown = Stopwatch.StartNew();
                    int n;
                    while ((n = await src.ReadAsync(buf, ct)) > 0)
                    {
                        await file.WriteAsync(buf.AsMemory(0, n), ct);
                        done += n;
                        if (shown.ElapsedMilliseconds > 300)
                        {
                            shown.Restart();
                            progress(key, $"{title} をダウンロードしています（{done / 1e6:0} / {(total > 0 ? $"{total / 1e6:0}" : "?")} MB）",
                                     total > 0 ? (double)done / total : null);
                        }
                    }
                }
                File.Move(part, dest, overwrite: true);
                return;
            }
            catch (Exception e) when (e is HttpRequestException or IOException && attempt < 5 && !ct.IsCancellationRequested)
            {
                Log.Warn($"ダウンロードが途中で止まりました。やり直します ({attempt}/5): {e.Message}");
                progress(key, $"通信が切れました。続きからやり直します（{attempt}/5）", null);
                await Task.Delay(3000, ct);
            }
        }
    }

    static async Task RunAsync(string exe, IEnumerable<string> args, CancellationToken ct)
    {
        var psi = new ProcessStartInfo(exe) { UseShellExecute = false, CreateNoWindow = true };
        foreach (var a in args)
            psi.ArgumentList.Add(a);
        using var p = Process.Start(psi) ?? throw new InvalidOperationException($"{exe} を起動できません");
        await p.WaitForExitAsync(ct);
        if (p.ExitCode != 0)
            throw new InvalidOperationException($"{Path.GetFileName(exe)} が失敗しました（終了コード {p.ExitCode}）");
    }

    // ---------------------------------------------------------------- 前の版の片付け
    /// <summary>前の版（Python・Ollama・Open-LLM-VTuber）が runtime\ に残したもので、もう使わないもの。</summary>
    public List<(string Path, long Bytes)> Leftovers() =>
        new[] { "ollama", "ollama-models", "python", "maid-venv", "uv", "cache", "Open-LLM-VTuber", "downloads" }
            .Select(d => Path.Combine(paths.Runtime, d))
            .Where(Directory.Exists)
            .Select(d => (d, Size(d)))
            .ToList();

    public void RemoveLeftovers()
    {
        foreach (var (dir, _) in Leftovers())
            TryDelete(dir);
    }

    static long Size(string dir)
    {
        try
        {
            return Directory.EnumerateFiles(dir, "*", SearchOption.AllDirectories).Sum(f => new FileInfo(f).Length);
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            return 0;
        }
    }

    static void TryDelete(string dir)
    {
        try
        {
            if (Directory.Exists(dir))
                Directory.Delete(dir, true);
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            Log.Warn($"消せませんでした {dir}: {e.Message}");
        }
    }
}
