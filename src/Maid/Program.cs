using System.Windows.Forms;

namespace Maid;

static class Program
{
    [STAThread]
    static int Main()
    {
        // どこで落ちても理由が残るように、まず exe の隣の maid-crash.log に書けるようにしておく
        AppDomain.CurrentDomain.UnhandledException += (_, e) => Crash((Exception)e.ExceptionObject);
        try
        {
            return Run();
        }
        catch (Exception e)
        {
            Crash(e);
            return 1;
        }
    }

    static int Run()
    {
        // maid フォルダ = maid.exe のある場所。全部この中に置く
        var paths = new MaidPaths(Environment.GetEnvironmentVariable("MAID_ROOT") is { Length: > 0 } r ? r : AppContext.BaseDirectory);
        Directory.CreateDirectory(paths.Logs);
        Log.Open(Path.Combine(paths.Logs, "maid.log"));
        Log.Info($"maid {typeof(Program).Assembly.GetName().Version} を起動します（{paths.Root}）");
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
        Application.Run(new MainForm(paths, new Settings(paths.SettingsFile)));
        Log.Info("終了しました");
        return 0;
    }

    static void Crash(Exception e)
    {
        var text = $"{DateTime.Now:yyyy-MM-dd HH:mm:ss} 起動できませんでした\n{e}\n";
        try
        {
            File.AppendAllText(Path.Combine(AppContext.BaseDirectory, "maid-crash.log"), text);
            Log.Error("起動できませんでした", e);
        }
        catch (Exception)
        {
        }
        MessageBox.Show($"起動できませんでした: {e.Message}\n\n詳しくは maid フォルダの maid-crash.log を見てください。", "maid",
                        MessageBoxButtons.OK, MessageBoxIcon.Error);
    }
}
