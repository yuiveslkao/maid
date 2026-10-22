using System.Windows.Forms;

namespace Maid;

static class Program
{
    [STAThread]
    static int Main()
    {
        // maid フォルダ = maid.exe のある場所。全部この中に置く
        var paths = new MaidPaths(Environment.GetEnvironmentVariable("MAID_ROOT") is { Length: > 0 } r ? r : AppContext.BaseDirectory);
        Directory.CreateDirectory(paths.Logs);
        Log.Open(Path.Combine(paths.Logs, "maid.log"));
        Win32.UseLibFolder(Path.Combine(paths.Root, "lib"));
        using var one = new Mutex(true, "maid-" + Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(System.Text.Encoding.UTF8.GetBytes(paths.Root)))[..16], out var first);
        if (!first)
        {
            MessageBox.Show("maid はもう起動しています。", "maid", MessageBoxButtons.OK, MessageBoxIcon.Information);
            return 1;
        }
        Application.SetHighDpiMode(HighDpiMode.PerMonitorV2);
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        Application.ThreadException += (_, e) => Log.Error("ウィンドウでエラー", e.Exception);
        AppDomain.CurrentDomain.UnhandledException += (_, e) => Log.Error("エラー", (Exception)e.ExceptionObject);
        try
        {
            Log.Info($"maid {typeof(Program).Assembly.GetName().Version} を起動します（{paths.Root}）");
            Application.Run(new MainForm(paths, new Settings(paths.SettingsFile)));
            return 0;
        }
        catch (Exception e)
        {
            Log.Error("起動できませんでした", e);
            MessageBox.Show($"起動できませんでした: {e.Message}\n\n詳しくは runtime\\logs\\maid.log を見てください。", "maid", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
}
