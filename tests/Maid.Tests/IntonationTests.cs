using Maid;
using Xunit;

namespace Maid.Tests;

public class IntonationTests
{
    // 声の代わりに、倍音のある音を高さを変えながら鳴らす
    static float[] Tone(Func<double, double> hz, double seconds)
    {
        var s = new float[(int)(seconds * Recognizer.Rate)];
        double phase = 0;
        for (var i = 0; i < s.Length; i++)
        {
            phase += 2 * Math.PI * hz((double)i / Recognizer.Rate) / Recognizer.Rate;
            s[i] = (float)(0.3 * Math.Sin(phase) + 0.15 * Math.Sin(2 * phase) + 0.08 * Math.Sin(3 * phase));
        }
        return s;
    }

    [Fact]
    public void FindsTheRiseAtTheEnd()
    {
        Assert.True(Intonation.RisesAtEnd(Tone(t => t < 1.2 ? 150 : 150 + (t - 1.2) * 250, 1.5)));
        Assert.False(Intonation.RisesAtEnd(Tone(_ => 150, 1.5)));
        Assert.False(Intonation.RisesAtEnd(Tone(t => t < 1.2 ? 150 : 150 - (t - 1.2) * 150, 1.5)));
        Assert.False(Intonation.RisesAtEnd(Tone(t => 200 - t * 40, 2)));
        Assert.False(Intonation.RisesAtEnd(new float[8000]));
    }

    [Fact]
    public void WritesKanaBothWays()
    {
        Assert.Equal("めい", Kana.ToHiragana("メイ"));
        Assert.Equal("メイ", Kana.ToKatakana("めい"));
        Assert.Equal("ゴ主人様", Kana.ToKatakana("ご主人様"));
    }
}
