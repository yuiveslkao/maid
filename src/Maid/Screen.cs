using System.Drawing;
using System.Drawing.Imaging;
using System.Text.RegularExpressions;

namespace Maid;

/// <summary>画面を 1 枚撮る・貼られた画像を小さくする（頭脳に見せる画像）。</summary>
static class ScreenShot
{
    const int MaxSide = 1280;  // 長い辺をここまで縮める（頭脳はこれ以上大きくてもよく見えず、遅くなるだけ）
    // 声で「画面見て」「これわかる？」のように言われたら、画面を撮って一緒に見せる
    static readonly Regex LookRe = new("(画面|がめん|これ|この|それ|ここ|今の|いまの).{0,12}(見て|みて|見える|みえる|わかる|分かる|読んで|よんで)");

    public static bool WantsLook(string text) => LookRe.IsMatch(text);

    /// <summary>メインの画面（モニター）を撮る。</summary>
    public static string Capture()
    {
        var bounds = System.Windows.Forms.Screen.PrimaryScreen!.Bounds;
        using var bmp = new Bitmap(bounds.Width, bounds.Height);
        using (var g = Graphics.FromImage(bmp))
            g.CopyFromScreen(bounds.Location, Point.Empty, bounds.Size);
        return ToDataUrl(bmp);
    }

    /// <summary>貼られた・落とされた画像を、頭脳に見せる大きさの JPEG にする。</summary>
    public static string Shrink(string dataUrl)
    {
        var m = Regex.Match(dataUrl, @"^data:image/[a-z+.-]+;base64,(.*)$", RegexOptions.Singleline);
        if (!m.Success)
            throw new ArgumentException("画像ではありません");
        using var ms = new MemoryStream(Convert.FromBase64String(m.Groups[1].Value));
        using var img = Image.FromStream(ms);
        return ToDataUrl(img);
    }

    static string ToDataUrl(Image img)
    {
        var scale = Math.Min(1.0, (double)MaxSide / Math.Max(img.Width, img.Height));
        var (w, h) = ((int)Math.Round(img.Width * scale), (int)Math.Round(img.Height * scale));
        using var small = new Bitmap(w, h);
        using (var g = Graphics.FromImage(small))
        {
            g.InterpolationMode = System.Drawing.Drawing2D.InterpolationMode.HighQualityBicubic;
            g.Clear(Color.White);  // 透明な部分は白にする
            g.DrawImage(img, 0, 0, w, h);
        }
        var jpeg = ImageCodecInfo.GetImageEncoders().First(c => c.FormatID == ImageFormat.Jpeg.Guid);
        using var enc = new EncoderParameters(1);
        enc.Param[0] = new EncoderParameter(System.Drawing.Imaging.Encoder.Quality, 85L);
        using var outMs = new MemoryStream();
        small.Save(outMs, jpeg, enc);
        return "data:image/jpeg;base64," + Convert.ToBase64String(outMs.ToArray());
    }
}
