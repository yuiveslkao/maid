using Maid;
using Xunit;

namespace Maid.Tests;

/// <summary>本物のモデルで聞き取る（MAID_TEST_ASR に ReazonSpeech のフォルダ、MAID_TEST_VAD に silero_vad.onnx があるときだけ）。</summary>
public class ListenTests
{
    static (string Asr, string Vad)? Models()
    {
        var asr = Environment.GetEnvironmentVariable("MAID_TEST_ASR");
        var vad = Environment.GetEnvironmentVariable("MAID_TEST_VAD");
        return asr is not null && vad is not null && Directory.Exists(asr) && File.Exists(vad) ? (asr, vad) : null;
    }

    static float[] Wav(string path)
    {
        var bytes = File.ReadAllBytes(path);
        var data = 44;
        var s = new float[(bytes.Length - data) / 2];
        for (var i = 0; i < s.Length; i++)
            s[i] = BitConverter.ToInt16(bytes, data + i * 2) / 32768f;
        return s;
    }

    [Fact]
    public async Task HearsSentencesFromTheWindow()
    {
        if (Models() is not { } m)
            return;
        var silence = new float[32000];
        var audio = new List<float>(silence);
        foreach (var i in new[] { 2, 3 })
            audio.AddRange([.. Wav(Path.Combine(m.Asr, "test_wavs", $"{i}.wav")), .. silence]);
        var heard = new List<string>();
        var listener = new Listener(() => new Recognizer(m.Asr, m.Vad), () => false, true);
        listener.OnText += heard.Add;
        using var stop = new CancellationTokenSource();
        var run = listener.RunAsync(stop.Token);
        while (listener.State != "listening")
            await Task.Delay(50);
        var pcm = audio.Select(f => (short)Math.Clamp(f * 32767, short.MinValue, short.MaxValue)).ToArray();
        for (var k = 0; k < pcm.Length; k += 1600)
        {
            var chunk = pcm.Skip(k).Take(1600).SelectMany(BitConverter.GetBytes).ToArray();
            listener.Hear(Convert.ToBase64String(chunk));
            await Task.Delay(10);
        }
        for (var t = 0; t < 100 && heard.Count < 2; t++)
            await Task.Delay(100);
        stop.Cancel();
        await run;
        Assert.Equal(["はやくおじいさんにあのおとこのはなしをきかせたかったのです", "ヤンバルクイナとの出会いは十八歳の時だった"], heard);
    }
}
