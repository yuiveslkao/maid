using System.Diagnostics;
using System.Text;

namespace Maid;

/// <summary>
/// 裏で動く部品（頭脳の llama-server・記憶を探す llama-server・声の AivisSpeech Engine）。ウィンドウは出さず、
/// 出力は runtime\logs\&lt;名前&gt;.log に書く。maid が終わったら（落ちても）一緒に止まる。
/// </summary>
public sealed class Parts : IDisposable
{
    public const int BrainPort = 11436;
    public const int EmbedPort = 11437;
    public const int AivisPort = 10101;

    readonly MaidPaths paths;
    readonly Win32.Job job = new();
    readonly List<Process> running = [];
    static readonly HttpClient Http = new() { Timeout = TimeSpan.FromSeconds(3) };

    public Parts(MaidPaths paths) => this.paths = paths;

    public static string Url(int port) => $"http://127.0.0.1:{port}";

    Process Start(string name, string exe, IEnumerable<string> args, string? dir = null)
    {
        Directory.CreateDirectory(paths.Logs);
        var log = new StreamWriter(Path.Combine(paths.Logs, $"{name}.log"), false, new UTF8Encoding(false)) { AutoFlush = true };
        var psi = new ProcessStartInfo(exe)
        {
            WorkingDirectory = dir ?? Path.GetDirectoryName(exe)!,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8,
        };
        foreach (var a in args)
            psi.ArgumentList.Add(a);
        var p = new Process { StartInfo = psi, EnableRaisingEvents = true };
        void Write(object _, DataReceivedEventArgs e)
        {
            if (e.Data is null)
                return;
            lock (log)
                log.WriteLine(e.Data);
        }
        p.OutputDataReceived += Write;
        p.ErrorDataReceived += Write;
        p.Exited += (_, _) => Log.Info($"{name} が終わりました（終了コード {SafeExitCode(p)}）");
        p.Start();
        job.Add(p);
        p.BeginOutputReadLine();
        p.BeginErrorReadLine();
        lock (running)
            running.Add(p);
        Log.Info($"{name} を起動しました: {exe} {string.Join(" ", psi.ArgumentList)}");
        return p;
    }

    static string SafeExitCode(Process p)
    {
        try
        {
            return p.ExitCode.ToString();
        }
        catch (InvalidOperationException)
        {
            return "?";
        }
    }

    /// <summary>
    /// 頭脳。モデルは全部 GPU に載せ、ファイルはメモリに写さずに読む（mmap。メモリの「専有」に数えられない）。
    /// Gemma の単語ごとの大きな表は、要る行だけをディスクから読む（--lazy-mode on）。考えるモードは切る。
    /// </summary>
    /// <param name="basic">新しいオプション（メモリの節約）を付けない。古い llama.cpp で起動できなかったとき用。</param>
    public Process StartBrain(Settings s, bool basic = false)
    {
        var args = new List<string>
        {
            "-m", paths.Model(s.ModelFile), "--host", "127.0.0.1", "--port", BrainPort.ToString(), "--alias", "maid",
            "-ngl", "999", "-c", s.Context.ToString(), "-np", "1", "-fa", "on", "-ctk", "q8_0", "-ctv", "q8_0",
            "--jinja", "--reasoning-budget", "0", "--reasoning-format", "deepseek", "--no-webui",
        };
        if (!basic)
        {
            // GPU に載らない部分（Gemma の単語ごとの大きな表など）を、専有メモリにコピーさせない:
            // CPU 用の並べ替え（repack）と、GPU とやりとりするための固定メモリ（host buffer）を使わず、ファイルのまま読む
            args.AddRange(["--cache-ram", "0", "--load-mode", "mmap", "--lazy-mode", "on", "--no-repack", "--no-host"]);
            args.AddRange(["-lv", "4"]);  // 読み込みのとき、どこに何 MB 確保したかをログに出す
        }
        if (File.Exists(paths.Model(s.MmprojFile)))
            args.AddRange(["--mmproj", paths.Model(s.MmprojFile)]);
        return Start(basic ? "brain-basic" : "brain", paths.LlamaServer, args);  // 1 回目のログ（止まった理由）を残す
    }

    /// <summary>記憶を探すための、意味のベクトルを作る頭脳（bge-m3）。GPU は頭脳に空けておくため CPU で動かす。</summary>
    public Process StartEmbed(Settings s) =>
        Start("embed", paths.LlamaServer,
        [
            "-m", paths.Model(s.EmbedFile), "--host", "127.0.0.1", "--port", EmbedPort.ToString(), "--embedding",
            "-ngl", "0", "-c", "2048", "-np", "1", "--load-mode", "mmap", "--no-webui",
        ]);

    /// <summary>声。全部の声のモデルは読み込まず、使う声だけを最初の合成で読み込む。</summary>
    public Process? StartVoice()
    {
        var exe = FindAivis();
        if (exe is null)
        {
            Log.Warn("AivisSpeech Engine が見つかりません");
            return null;
        }
        return Start("aivisspeech", exe, ["--host", "127.0.0.1", "--port", AivisPort.ToString()]);
    }

    public string? FindAivis() =>
        Directory.Exists(paths.Aivis) ? Directory.EnumerateFiles(paths.Aivis, "run.exe", SearchOption.AllDirectories).FirstOrDefault() : null;

    /// <summary>部品が返事をするようになるまで待つ。途中で部品が止まったら false。</summary>
    public static async Task<bool> WaitAsync(Process p, string url, TimeSpan timeout, CancellationToken ct)
    {
        var sw = Stopwatch.StartNew();
        while (sw.Elapsed < timeout && !ct.IsCancellationRequested)
        {
            if (p.HasExited)
                return false;
            try
            {
                using var res = await Http.GetAsync(url, ct);
                if (res.IsSuccessStatusCode)
                    return true;
            }
            catch (Exception e) when (e is HttpRequestException or TaskCanceledException)
            {
            }
            await Task.Delay(500, ct);
        }
        return false;
    }

    public void Dispose()
    {
        lock (running)
        {
            foreach (var p in running)
            {
                try
                {
                    if (!p.HasExited)
                        p.Kill(entireProcessTree: true);
                }
                catch (Exception e) when (e is InvalidOperationException or System.ComponentModel.Win32Exception)
                {
                }
            }
            running.Clear();
        }
        job.Dispose();
    }
}
