using System.Text.RegularExpressions;
using System.Threading.Channels;
using SherpaOnnx;

namespace Maid;

/// <summary>
/// 音（16kHz の float）を区切って文字にする。話しはじめと話し終わりは Silero VAD、文字にするのは ReazonSpeech（どちらも CPU）。
/// ReazonSpeech は「？」を出さないので、語尾が上がっていたら（か「ですか」などで終わっていたら）付ける。
/// </summary>
public sealed class Recognizer : IDisposable
{
    public const int Rate = 16000;
    const int Window = 512;                 // Silero VAD が一度に見る長さ（16kHz で 32ms）
    const double MinSeconds = 0.3;          // これより短い音は物音として捨てる
    const double PreSeconds = 0.25;         // 話しはじめと判定される少し前の音も足す（言いはじめの子音を落とさないように）
    const int KeepSeconds = 25;             // 〃 のために残しておく音の長さ（1 回の発言は 20 秒で区切る）
    static readonly Regex NotWords = new(@"[\s、。！？!?…ー〜]+");
    static readonly Regex AsksInWords = new(@"(ですか|ますか|でしょうか|かな|かしら)$");
    public static readonly string[] AsrFiles =
        ["encoder-epoch-99-avg-1.int8.onnx", "decoder-epoch-99-avg-1.int8.onnx", "joiner-epoch-99-avg-1.int8.onnx", "tokens.txt"];

    readonly OfflineRecognizer asr;
    readonly VoiceActivityDetector vad;
    float[] buf = [];                     // VAD にまだ渡していない端数（Window より短い）
    readonly List<float> recent = [];     // 話しはじめの少し前の音を足すために残しておく音
    int recentFrom;  // recent の先頭が、Reset から何サンプル目か

    /// <param name="hotwords">聞き取りやすくする言葉（キャラの名前など）。</param>
    public Recognizer(string asrDir, string vadModel, int silenceMs = 900, int threads = 2, IEnumerable<string>? hotwords = null)
    {
        var config = new OfflineRecognizerConfig();
        config.FeatConfig.SampleRate = Rate;
        config.FeatConfig.FeatureDim = 80;
        config.ModelConfig.Transducer.Encoder = Path.Combine(asrDir, AsrFiles[0]);
        config.ModelConfig.Transducer.Decoder = Path.Combine(asrDir, AsrFiles[1]);
        config.ModelConfig.Transducer.Joiner = Path.Combine(asrDir, AsrFiles[2]);
        config.ModelConfig.Tokens = Path.Combine(asrDir, AsrFiles[3]);
        config.ModelConfig.NumThreads = threads;
        config.ModelConfig.Provider = "cpu";
        // 候補をいくつか残して探す（greedy より少し正確。時間は 2 割ほど増えるが、1 回の発言で数十 ms）
        config.DecodingMethod = "modified_beam_search";
        config.MaxActivePaths = 4;
        var words = (hotwords ?? []).Where(w => !string.IsNullOrWhiteSpace(w)).SelectMany(w => new[] { w.Trim(), Kana.ToHiragana(w.Trim()), Kana.ToKatakana(w.Trim()) })
                                    .Distinct().ToList();
        if (words.Count > 0)
        {
            var file = Path.Combine(asrDir, "hotwords.txt");
            File.WriteAllLines(file, words);
            config.ModelConfig.ModelingUnit = "cjkchar";
            config.HotwordsFile = file;
            config.HotwordsScore = 2.0f;
        }
        asr = new OfflineRecognizer(config);

        var v = new VadModelConfig();
        v.SileroVad.Model = vadModel;
        v.SileroVad.Threshold = 0.5f;
        v.SileroVad.MinSilenceDuration = silenceMs / 1000f;
        v.SileroVad.MinSpeechDuration = 0.25f;
        v.SileroVad.WindowSize = Window;
        v.SileroVad.MaxSpeechDuration = 20;
        v.SampleRate = Rate;
        v.NumThreads = 1;
        vad = new VoiceActivityDetector(v, 30);
    }

    public bool Speaking => vad.IsSpeechDetected();

    public void Reset()
    {
        vad.Reset();
        buf = [];
        recent.Clear();
        recentFrom = 0;
    }

    /// <summary>音を渡す。話し終わった文があれば、その文字を返す。</summary>
    public List<string> Feed(float[] samples)
    {
        recent.AddRange(samples);
        if (recent.Count > (KeepSeconds + 1) * Rate)  // 1 秒分たまってからまとめて捨てる（毎回詰め直さない）
        {
            var drop = recent.Count - KeepSeconds * Rate;
            recent.RemoveRange(0, drop);
            recentFrom += drop;
        }
        var all = buf.Length == 0 ? samples : [.. buf, .. samples];
        var at = 0;
        for (; at + Window <= all.Length; at += Window)
            vad.AcceptWaveform(all[at..(at + Window)]);
        buf = all[at..];
        var out_ = new List<string>();
        while (!vad.IsEmpty())
        {
            var seg = vad.Front();
            var speech = seg.Samples;
            var start = seg.Start;
            vad.Pop();
            if (speech.Length < MinSeconds * Rate)
                continue;
            int a = Math.Max(start - (int)(PreSeconds * Rate), recentFrom) - recentFrom, b = start - recentFrom;
            if (0 <= a && a < b && b <= recent.Count)
                speech = [.. recent.GetRange(a, b - a), .. speech];
            var text = Decode(speech);
            if (NotWords.Replace(text, "").Length == 0)
                continue;
            var rise = Intonation.Rise(speech);
            Log.Info($"語尾の高さ: {rise:+0.0;-0.0} 半音（{text}）");
            if (rise >= Intonation.RiseSemitones || AsksInWords.IsMatch(text))
                text += "？";
            out_.Add(text);
        }
        return out_;
    }

    public string Decode(float[] samples)
    {
        var pad = new float[Rate / 2];  // 前後に無音を足さないと、言いはじめを聞き落とす
        using var stream = asr.CreateStream();
        stream.AcceptWaveform(Rate, [.. pad, .. samples, .. pad]);
        asr.Decode(stream);
        return stream.Result.Text.Trim();
    }

    public void Dispose()
    {
        vad.Dispose();
        asr.Dispose();
    }
}

/// <summary>
/// 画面から届くマイクの音を聞き、話し終わるごとに OnText を呼ぶ。マイクの音は画面（WebView2）が取り、
/// ブラウザと同じ雑音の除去・エコーの除去・音量の自動調整をかけてから 0.1 秒ごとに Hear() に渡す。
/// State: "loading"（準備中）/ "listening" / "speaking"（話している）/ "off" / "discord"（通話中で聞かない）/ "error"。
/// </summary>
public sealed class Listener
{
    readonly Func<Recognizer> make;
    readonly Func<bool> blocked;
    readonly Channel<byte[]> audio = Channel.CreateBounded<byte[]>(new BoundedChannelOptions(100) { FullMode = BoundedChannelFullMode.DropOldest });  // 10 秒分

    public Listener(Func<Recognizer> make, Func<bool> blocked, bool enabled)
    {
        this.make = make;
        this.blocked = blocked;
        Enabled = enabled;
    }

    public event Action<string>? OnText;
    public event Action<string>? OnState;
    public bool Enabled { get; set; }
    public string State { get; private set; } = "";

    /// <summary>画面から届いた音（16kHz・16 ビットの PCM を base64 にしたもの）。</summary>
    public void Hear(string b64)
    {
        if (Enabled)
            audio.Writer.TryWrite(Convert.FromBase64String(b64));
    }

    void Set(string state)
    {
        if (state == State)
            return;
        State = state;
        OnState?.Invoke(state);
    }

    public Task RunAsync(CancellationToken stop) => Task.Run(() => Run(stop), stop);

    void Run(CancellationToken stop)
    {
        Set("loading");
        Recognizer rec;
        try
        {
            var sw = System.Diagnostics.Stopwatch.StartNew();
            rec = make();
            Log.Info($"音声認識の準備ができました（{sw.Elapsed.TotalSeconds:0.0} 秒）");
        }
        catch (Exception e)
        {
            Log.Error("音声認識を準備できませんでした", e);
            Set("error");
            return;
        }
        using (rec)
        {
            var checkedAt = 0L;
            var isBlocked = false;
            while (!stop.IsCancellationRequested)
            {
                if (Environment.TickCount64 - checkedAt >= 1000)
                {
                    checkedAt = Environment.TickCount64;
                    isBlocked = blocked();
                }
                if (!Enabled || isBlocked)
                {
                    Set(Enabled ? "discord" : "off");
                    rec.Reset();
                    while (audio.Reader.TryRead(out _))  // 通話中・切っている間の音は使わない
                    {
                    }
                    stop.WaitHandle.WaitOne(300);
                    continue;
                }
                byte[] pcm;
                try
                {
                    using var wait = CancellationTokenSource.CreateLinkedTokenSource(stop);
                    wait.CancelAfter(500);
                    pcm = audio.Reader.ReadAsync(wait.Token).AsTask().GetAwaiter().GetResult();
                }
                catch (OperationCanceledException)
                {
                    Set("listening");
                    continue;
                }
                var samples = new float[pcm.Length / 2];
                for (var i = 0; i < samples.Length; i++)
                    samples[i] = BitConverter.ToInt16(pcm, i * 2) / 32768f;
                var texts = rec.Feed(samples);
                Set(rec.Speaking ? "speaking" : "listening");
                foreach (var text in texts)
                {
                    Log.Info($"聞こえた: {text}");
                    OnText?.Invoke(text);
                }
            }
        }
    }
}

/// <summary>ひらがなとカタカナの書き換え（聞き取りやすくする言葉を、どちらの書き方でも足すため）。</summary>
public static class Kana
{
    public static string ToHiragana(string s) => new([.. s.Select(c => c is >= 'ァ' and <= 'ヶ' ? (char)(c - 0x60) : c)]);
    public static string ToKatakana(string s) => new([.. s.Select(c => c is >= 'ぁ' and <= 'ゖ' ? (char)(c + 0x60) : c)]);
}
