namespace Maid;

/// <summary>maid のログ（runtime\logs\maid.log）。起動のたびに作り直す。</summary>
public static class Log
{
    static readonly object Gate = new();
    static StreamWriter? writer;

    public static void Open(string path)
    {
        lock (Gate)
        {
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            writer?.Dispose();
            writer = new StreamWriter(path, append: false, new System.Text.UTF8Encoding(false)) { AutoFlush = true };
        }
    }

    public static void Info(string message) => Write("", message);
    public static void Warn(string message) => Write("注意 ", message);
    public static void Error(string message, Exception e) => Write("エラー ", $"{message}: {e}");

    static void Write(string level, string message)
    {
        var line = $"{DateTime.Now:HH:mm:ss} {level}{message}";
        lock (Gate)
        {
            writer?.WriteLine(line);
        }
        System.Diagnostics.Debug.WriteLine(line);
    }
}
