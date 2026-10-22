namespace Maid;

/// <summary>声の高さ（F0）から、語尾が上がっているか（問いかけか）を見る。音声認識は「？」を出さないため。</summary>
public static class Intonation
{
    const int Rate = Recognizer.Rate;
    const int Frame = 640, Hop = 160;         // 40ms ずつ、10ms ずらして見る
    const int MinLag = Rate / 500, MaxLag = Rate / 70;  // 70〜500Hz
    const double Threshold = 0.2;             // YIN のしきい値
    const int TailFrames = 15;                // 語尾として見る、声のあるフレームの数（150ms ほど）
    public const double RiseSemitones = 2.5;  // これだけ上がったら問いかけ

    /// <summary>声の高さ（Hz）を 10ms ごとに。声が無いところは 0。</summary>
    public static double[] Pitch(ReadOnlySpan<float> s)
    {
        var n = Math.Max(0, (s.Length - Frame - MaxLag) / Hop + 1);
        var f0 = new double[n];
        var rms = new double[n];
        for (var k = 0; k < n; k++)
        {
            double e = 0;
            foreach (var x in s.Slice(k * Hop, Frame))
                e += x * x;
            rms[k] = Math.Sqrt(e / Frame);
        }
        var loud = rms.Length > 0 ? rms.Max() : 0;
        var d = new double[MaxLag + 1];
        for (var k = 0; k < n; k++)
        {
            if (rms[k] < loud * 0.1 || rms[k] < 1e-3)
                continue;
            var w = s.Slice(k * Hop, Frame + MaxLag);
            for (var lag = 1; lag <= MaxLag; lag++)
            {
                double sum = 0;
                for (var i = 0; i < Frame; i++)
                {
                    var diff = w[i] - w[i + lag];
                    sum += diff * diff;
                }
                d[lag] = sum;
            }
            // 累積平均で割って、最初にしきい値を下回った谷を周期とする（YIN）
            double run = 0;
            var best = 0;
            for (var lag = 1; lag <= MaxLag; lag++)
            {
                run += d[lag];
                var v = run > 0 ? d[lag] * lag / run : 1;
                d[lag] = v;
            }
            for (var lag = MinLag; lag < MaxLag; lag++)
                if (d[lag] < Threshold && d[lag] <= d[lag + 1])
                {
                    best = lag;
                    break;
                }
            if (best > 0)
                f0[k] = (double)Rate / best;
        }
        return f0;
    }

    /// <summary>語尾が上がっているか。最後の 1.5 秒だけを見る。</summary>
    public static bool RisesAtEnd(float[] samples) => Rise(samples) >= RiseSemitones;

    /// <summary>語尾で声がどれだけ上がったか（半音。声が短すぎて分からなければ 0）。</summary>
    public static double Rise(float[] samples)
    {
        var from = Math.Max(0, samples.Length - (int)(1.5 * Rate));
        var voiced = Pitch(samples.AsSpan(from)).Where(f => f > 0).Select(f => 12 * Math.Log2(f / 100)).ToArray();
        if (voiced.Length < TailFrames * 2)
            return 0;
        // 5 フレームの中央値でならす（倍・半分の高さに飛ぶ誤りを消す）
        var smooth = voiced.Select((_, i) => Median(voiced[Math.Max(0, i - 2)..Math.Min(voiced.Length, i + 3)])).ToArray();
        var tail = smooth[^TailFrames..];
        var before = Median(smooth[..^TailFrames][^Math.Min(smooth.Length - TailFrames, 60)..]);
        // 語尾のいちばん高いところと、最後まで上がったままかの両方を見る（途中だけ高い「えー、」などを除く）
        return Math.Min(tail.Max() - before, (tail[^3..].Average() - before) / 0.6);
    }

    static double Median(double[] a)
    {
        var b = a.Order().ToArray();
        return b.Length % 2 == 1 ? b[b.Length / 2] : (b[b.Length / 2 - 1] + b[b.Length / 2]) / 2;
    }
}
