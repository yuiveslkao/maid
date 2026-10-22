using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace Maid;

/// <summary>
/// 会話の記録（data\chat.json。次に開いたとき続きから見える）と、頭脳への話しかけ。
/// 見せた画像は残さず「何枚見せたか」だけ残す。キャラから話しかけた内容は、相手が返事をしたときに Brain が足すので残さない。
/// </summary>
public sealed class ChatSession
{
    public const int HistoryKeep = 40;  // 頭脳に渡す・保存する発言の数
    public const string NoBrain = "（頭脳につながりませんでした。少し待ってからもう一度話しかけてください）";
    static readonly JsonSerializerOptions Json = new() { WriteIndented = true, Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping };

    readonly Func<JsonArray, bool, Action<string>, CancellationToken, Task<string>> reply;
    readonly Character character;
    readonly string? historyPath;
    readonly SemaphoreSlim one = new(1, 1);
    CancellationTokenSource cancel = new();
    long lastActivity = Environment.TickCount64;

    public ChatSession(Func<JsonArray, bool, Action<string>, CancellationToken, Task<string>> reply, Character character, string? historyPath)
    {
        this.reply = reply;
        this.character = character;
        this.historyPath = historyPath;
        History = Load();
    }

    public List<JsonObject> History { get; private set; }
    public bool Busy { get; private set; }
    public TimeSpan Quiet => TimeSpan.FromMilliseconds(Environment.TickCount64 - Interlocked.Read(ref lastActivity));
    public void Touch() => Interlocked.Exchange(ref lastActivity, Environment.TickCount64);

    List<JsonObject> Load()
    {
        try
        {
            if (historyPath is null || !File.Exists(historyPath))
                return [];
            return (JsonNode.Parse(File.ReadAllText(historyPath)) as JsonArray ?? [])
                   .OfType<JsonObject>()
                   .Where(m => Msg.Role(m) is "user" or "assistant" && m["content"] is JsonValue)
                   .TakeLast(HistoryKeep)
                   .Select(m => (JsonObject)m.DeepClone())
                   .ToList();
        }
        catch (Exception e) when (e is IOException or JsonException)
        {
            return [];
        }
    }

    void Save()
    {
        if (historyPath is null)
            return;
        Directory.CreateDirectory(Path.GetDirectoryName(historyPath)!);
        File.WriteAllText(historyPath, new JsonArray(History.TakeLast(HistoryKeep).Select(m => (JsonNode)m.DeepClone()).ToArray()).ToJsonString(Json));
    }

    /// <summary>記録した発言を、頭脳に渡す形にする（見せた画像は残していないので、見せたことだけ伝える）。</summary>
    static JsonObject ForBrain(JsonObject m)
    {
        var n = m["images"]?.GetValue<int>() ?? 0;
        var text = Msg.TextOf(m);
        return Msg.Make(Msg.Role(m), n > 0 ? $"{text}\n（このとき画像を {n} 枚見せた）".Trim() : text);
    }

    /// <summary>今の返事を止める（割り込み・「止める」）。</summary>
    public void Cancel() => cancel.Cancel();

    /// <summary>頭脳に話しかけ、返事を少しずつ onChunk に渡す。戻り値は返事の全文。images は見せる画像（data URL）。</summary>
    public async Task<string> SendAsync(string text, Action<string> onChunk, bool proactive = false, IReadOnlyList<string>? images = null)
    {
        await one.WaitAsync();
        cancel = new CancellationTokenSource();
        Busy = true;
        Touch();
        try
        {
            var messages = new JsonArray { Msg.Make("system", character.Persona) };
            foreach (var m in History.TakeLast(HistoryKeep))
                messages.Add(ForBrain(m));
            if (!proactive)
                messages.Add(images is { Count: > 0 } ? Msg.WithImages(text.Length > 0 ? text : "この画像を見て", images) : Msg.Make("user", text));
            var received = new System.Text.StringBuilder();
            string full;
            try
            {
                full = await reply(messages, proactive, piece => { received.Append(piece); onChunk(piece); }, cancel.Token);
            }
            catch (OperationCanceledException)
            {
                full = received.ToString();  // 止めたところまでを残す
            }
            catch (Exception e) when (e is HttpRequestException or IOException)
            {
                Log.Warn($"頭脳に話しかけられませんでした: {e.Message}");
                onChunk(NoBrain);
                return NoBrain;
            }
            if (!proactive)
            {
                var said = Msg.Make("user", text);
                if (images is { Count: > 0 })
                    said["images"] = images.Count;
                History.Add(said);
                History.Add(Msg.Make("assistant", full));
                Save();
            }
            return full;
        }
        finally
        {
            Busy = false;
            Touch();
            one.Release();
        }
    }

    public void Clear()
    {
        History = [];
        Save();
    }
}

public static class Proactive
{
    /// <summary>会話が minutes 分無く、PC を使っていて（idle が away 未満）、Discord で通話していないときに speak を呼ぶ。</summary>
    public static async Task LoopAsync(ChatSession session, double minutes, Func<Task> speak, Func<TimeSpan> idle, Func<bool> blocked,
                                       CancellationToken stop, TimeSpan? away = null, TimeSpan? poll = null)
    {
        if (minutes <= 0)
            return;
        using var timer = new PeriodicTimer(poll ?? TimeSpan.FromSeconds(20));
        try
        {
            while (await timer.WaitForNextTickAsync(stop))
            {
                if (!session.Busy && session.Quiet >= TimeSpan.FromMinutes(minutes) && idle() < (away ?? TimeSpan.FromMinutes(15)) && !blocked())
                {
                    session.Touch();
                    await speak();
                }
            }
        }
        catch (OperationCanceledException)
        {
        }
    }
}
