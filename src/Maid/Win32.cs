using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Runtime.Loader;
using Microsoft.Win32;

namespace Maid;

/// <summary>Windows の小物。</summary>
static partial class Win32
{
    /// <summary>ネイティブの DLL（音声認識・SQLite・WebView2 の読み込み役）を maid フォルダの lib\ から読む。</summary>
    public static void UseLibFolder(string lib)
    {
        if (!Directory.Exists(lib))
            return;
        SetDllDirectory(lib);  // DLL が読む DLL（onnxruntime.dll など）も lib\ から探させる
        AssemblyLoadContext.Default.ResolvingUnmanagedDll += (_, name) =>
        {
            var path = Path.Combine(lib, name.EndsWith(".dll", StringComparison.OrdinalIgnoreCase) ? name : name + ".dll");
            return File.Exists(path) ? NativeLibrary.Load(path) : IntPtr.Zero;
        };
    }

    /// <summary>キーボードかマウスを最後に触ってからの時間。</summary>
    public static TimeSpan SinceInput()
    {
        var info = new LastInputInfo { Size = (uint)Marshal.SizeOf<LastInputInfo>() };
        if (!GetLastInputInfo(ref info))
            return TimeSpan.Zero;
        return TimeSpan.FromMilliseconds(unchecked((uint)Environment.TickCount - info.Time));
    }

    const string MicKey = @"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone\NonPackaged";

    /// <summary>Discord（PTB / Canary 含む）がいまマイクを使っていれば true。Windows がアプリごとに記録しているマイクの使用状況
    /// （タスクバーのマイク表示と同じ情報）を見る。Discord は通話中だけマイクを開くので、これで通話中かがわかる。</summary>
    public static bool DiscordUsingMic()
    {
        try
        {
            using var root = Registry.CurrentUser.OpenSubKey(MicKey);
            if (root is null)
                return false;
            foreach (var name in root.GetSubKeyNames())
            {
                // 例: C:#Users#me#AppData#Local#Discord#app-1.0.9200#Discord.exe
                if (!name.Split('#')[^1].StartsWith("discord", StringComparison.OrdinalIgnoreCase))
                    continue;
                using var k = root.OpenSubKey(name);
                if (k?.GetValue("LastUsedTimeStop") is long stop && k.GetValue("LastUsedTimeStart") is long start && stop == 0 && start != 0)
                    return true;
            }
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException or System.Security.SecurityException)
        {
        }
        return false;
    }

    /// <summary>maid が終わったら（落ちても）一緒に止まる子プロセスの入れ物。</summary>
    public sealed class Job : IDisposable
    {
        readonly IntPtr handle = CreateJobObjectW(IntPtr.Zero, null);

        public Job()
        {
            var info = new ExtendedLimitInformation { BasicLimitInformation = new BasicLimitInformation { LimitFlags = 0x2000 } };  // KILL_ON_JOB_CLOSE
            var size = Marshal.SizeOf<ExtendedLimitInformation>();
            var ptr = Marshal.AllocHGlobal(size);
            try
            {
                Marshal.StructureToPtr(info, ptr, false);
                if (!SetInformationJobObject(handle, 9, ptr, (uint)size))
                    throw new Win32Exception();
            }
            finally
            {
                Marshal.FreeHGlobal(ptr);
            }
        }

        public void Add(System.Diagnostics.Process p)
        {
            if (!AssignProcessToJobObject(handle, p.Handle))
                Log.Warn($"子プロセスを Job に入れられませんでした ({p.ProcessName}): {new Win32Exception().Message}");
        }

        public void Dispose() => CloseHandle(handle);
    }

    [StructLayout(LayoutKind.Sequential)]
    struct LastInputInfo
    {
        public uint Size;
        public uint Time;
    }

    [StructLayout(LayoutKind.Sequential)]
    struct BasicLimitInformation
    {
        public long PerProcessUserTimeLimit;
        public long PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize;
        public UIntPtr MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass;
        public uint SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    struct IoCounters
    {
        public ulong ReadOperationCount, WriteOperationCount, OtherOperationCount, ReadTransferCount, WriteTransferCount, OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    struct ExtendedLimitInformation
    {
        public BasicLimitInformation BasicLimitInformation;
        public IoCounters IoInfo;
        public UIntPtr ProcessMemoryLimit;
        public UIntPtr JobMemoryLimit;
        public UIntPtr PeakProcessMemoryUsed;
        public UIntPtr PeakJobMemoryUsed;
    }

    // LibraryImport は名前を書いたとおりに探すので、文字列の関数は末尾の W まで書く
    [LibraryImport("kernel32.dll", EntryPoint = "SetDllDirectoryW", StringMarshalling = StringMarshalling.Utf16, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static partial bool SetDllDirectory(string path);

    [LibraryImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static partial bool GetLastInputInfo(ref LastInputInfo info);

    [LibraryImport("kernel32.dll", StringMarshalling = StringMarshalling.Utf16, SetLastError = true)]
    private static partial IntPtr CreateJobObjectW(IntPtr attributes, string? name);

    [LibraryImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static partial bool SetInformationJobObject(IntPtr job, int infoClass, IntPtr info, uint length);

    [LibraryImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static partial bool AssignProcessToJobObject(IntPtr job, IntPtr process);

    [LibraryImport("kernel32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static partial bool CloseHandle(IntPtr handle);
}
