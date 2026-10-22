using System.Text.Json.Nodes;

namespace Maid;

/// <summary>
/// 会話のウィンドウの中身。部品（頭脳・記憶を探す頭脳・声）を起動し、画面からの呼び出しに答える。
/// 返事は、声で返事するときは文ごとに声と一緒に画面へ渡し（画面は声を鳴らしはじめたときに文を出す）、
/// そうでなければ少しずつ文字で渡す。声で割り込まれたら、前の返事の続きは画面にも声にも出さない。
/// </summary>
sealed class Conversation : IDisposable
{
    readonly MaidPaths paths;
    readonly Settings settings;
    readonly Character character;
    readonly Icons icons;
    readonly Action<string, object?[]> js;
    readonly Func<Task<string>> capture;
    readonly Parts parts;
    readonly CancellationTokenSource stop = new();
    readonly TaskCompletionSource<bool> ready = new(TaskCreationOptions.RunContinuationsAsynchronously);
    readonly ChatSession session;
    readonly Speaker? speaker;
    readonly Listener? listener;
    Memory? memory;
    Brain? brain;
    bool voice;
    int replyId;
    string status = "頭脳を準備しています…";

    public Conversation(MaidPaths paths, Settings settings, Action<string, object?[]> js, Func<Task<string>> capture)
    {
        this.paths = paths;
        this.settings = settings;
        this.js = js;
        this.capture = capture;
        character = Character.Load(paths.CharacterFile);
        icons = new Icons(paths.Icons);
        parts = new Parts(paths);
        session = new ChatSession(ReplyAsync, character, paths.ChatHistory);
        voice = settings.VoiceReply;
        if (settings.VoiceReply)
        {
            var aivis = new Aivis(Parts.Url(Parts.AivisPort), settings.Voice, settings.Speed);
            speaker = new Speaker(async (text, ct) =>
            {
                try
                {
                    return await aivis.SynthesizeAsync(text, ct);
                }
                catch (Exception e) when (e is HttpRequestException or TaskCanceledException or KeyNotFoundException)
                {
                    Log.Warn($"声を作れませんでした: {e.Message}");
                    return null;
                }
            }, (b64, part) => js("say", [b64, part]));
            Voice = aivis;
        }
        if (settings.Mic && Recognizer.AsrFiles.All(f => File.Exists(Path.Combine(paths.Asr, f))) && File.Exists(paths.SileroVad))
        {
            listener = new Listener(() => new Recognizer(paths.Asr, paths.SileroVad, settings.SilenceMs, hotwords: [character.Name, character.Human]),
                                    Win32.DiscordUsingMic, enabled: true);
            listener.OnText += Heard;
            listener.OnState += st => js("micState", [st]);
        }
    }

    Aivis? Voice { get; }

    void Status(string text)
    {
        status = text;
        js("status", [text]);
    }

    /// <summary>部品を起動して、話せるようにする。</summary>
    public async Task StartAsync()
    {
        try
        {
            var ct = stop.Token;
            listener?.RunAsync(ct);
            var brainProc = parts.StartBrain(settings);
            System.Diagnostics.Process? voiceProc = settings.VoiceReply ? parts.StartVoice() : null;
            Embedder? embedder = null;
            if (settings.Memory && settings.Embed && File.Exists(paths.Model(settings.EmbedFile)))
            {
                parts.StartEmbed(settings);
                embedder = new Embedder(Parts.Url(Parts.EmbedPort));
            }
            var sw = System.Diagnostics.Stopwatch.StartNew();
            var up = await Parts.WaitAsync(brainProc, Parts.Url(Parts.BrainPort) + "/health", TimeSpan.FromMinutes(10), ct);
            if (!up && brainProc.HasExited && sw.Elapsed < TimeSpan.FromSeconds(30))
            {
                // すぐ止まったときは、新しいオプションを知らない llama.cpp かもしれないので、付けずにもう一度
                Log.Warn("頭脳がすぐ止まったので、メモリを節約するオプションを外して起動し直します（runtime\\logs\\brain.log）");
                brainProc = parts.StartBrain(settings, basic: true);
                up = await Parts.WaitAsync(brainProc, Parts.Url(Parts.BrainPort) + "/health", TimeSpan.FromMinutes(10), ct);
            }
            if (!up)
            {
                Status("頭脳を起動できませんでした（runtime\\logs\\brain.log を見てください）");
                ready.TrySetResult(false);
                return;
            }
            Log.Info($"頭脳の準備ができました（{sw.Elapsed.TotalSeconds:0.0} 秒）");
            var llm = new Llm(Parts.Url(Parts.BrainPort));
            if (settings.Memory)
            {
                memory = new Memory(paths.Data, (m, max) => llm.CompleteAsync(m, max, 0.3, CancellationToken.None),
                                    embedder is null ? null : texts => embedder.EmbedAsync(texts));
                _ = Task.Run(() => MemoryWorkerAsync(memory, ct), ct);
            }
            brain = new Brain(llm, new WebSearch(), memory, settings.Search, settings.SearchFiller);
            _ = Proactive.LoopAsync(session, settings.ProactiveMinutes, () => ReplyAndShowAsync("", true, []), Win32.SinceInput,
                                    Win32.DiscordUsingMic, ct);
            if (voiceProc is not null && Voice is not null)
            {
                Status("声を準備しています…");
                await Voice.WarmUpAsync(ct);
            }
            Status("");
            ready.TrySetResult(true);
        }
        catch (Exception e)
        {
            Log.Error("起動に失敗しました", e);
            Status($"起動に失敗しました: {e.Message}");
            ready.TrySetResult(false);
        }
    }

    /// <summary>しばらく話していなかったら、それまでの会話を黙ってまとめる。</summary>
    static async Task MemoryWorkerAsync(Memory memory, CancellationToken ct)
    {
        using var timer = new PeriodicTimer(TimeSpan.FromMinutes(1));
        try
        {
            while (await timer.WaitForNextTickAsync(ct))
            {
                try
                {
                    await memory.MaybeConsolidateAsync();
                }
                catch (Exception e)
                {
                    Log.Error("記憶の整理でエラー", e);
                }
            }
        }
        catch (OperationCanceledException)
        {
        }
    }

    async Task<string> ReplyAsync(JsonArray messages, bool proactive, Action<string> emit, CancellationToken ct)
    {
        if (!await ready.Task || brain is null)
        {
            emit(ChatSession.NoBrain);
            return ChatSession.NoBrain;
        }
        return await brain.ReplyAsync(messages, proactive, emit, ct);
    }

    async Task ReplyAndShowAsync(string text, bool proactive, List<string> images)
    {
        var rid = Interlocked.Increment(ref replyId);
        var voiced = speaker is not null && voice;  // 返事の途中で切り替えても、この返事は最初の出し方のまま
        speaker?.Reset();
        if (proactive)
            js("proactive", []);
        try
        {
            await session.SendAsync(text, piece =>
            {
                if (rid != replyId)
                    return;
                if (voiced)
                    speaker!.Feed(piece);
                else
                    js("chunk", [piece]);
            }, proactive, images);
        }
        catch (Exception e)
        {
            Log.Error("返事でエラー", e);  // 画面が「返事中」のまま止まらないように、下で終わりを知らせる
        }
        if (rid != replyId)
            return;
        if (voiced)
        {
            speaker!.Feed("", final: true);
            speaker.Finish();
        }
        else
            js("done", []);
    }

    /// <summary>声で話しかけられた。返事の途中なら止めて、聞こえた文を送る。「画面見て」なら画面も見せる。</summary>
    void Heard(string text)
    {
        Interlocked.Increment(ref replyId);  // 前の返事の続きを、この時点から出さない
        session.Cancel();
        speaker?.Reset();
        _ = Task.Run(async () =>
        {
            var images = new List<string>();
            if (ScreenShot.WantsLook(text))
            {
                try
                {
                    images.Add(await capture());
                }
                catch (Exception e)
                {
                    Log.Warn($"画面を撮れませんでした: {e.Message}");
                }
            }
            js("heard", [text, images]);
            await ReplyAndShowAsync(text, false, images);
        });
    }

    // ---------------------------------------------------------------- 画面からの呼び出し
    public JsonNode Init() => new JsonObject
    {
        ["name"] = character.Name,
        ["human"] = character.Human,
        ["icons"] = new JsonObject { ["character"] = icons.DataUrl("character"), ["user"] = icons.DataUrl("user") },
        ["history"] = new JsonArray(session.History.Select(m => (JsonNode)m.DeepClone()).ToArray()),
        ["voice"] = voice,
        ["voiceAvailable"] = speaker is not null,
        ["mic"] = listener?.Enabled ?? false,
        ["micAvailable"] = listener is not null,
        ["micState"] = listener?.State ?? "",
        ["status"] = status,
    };

    public void Send(string text, List<string> images) => _ = Task.Run(() => ReplyAndShowAsync(text, false, images));

    public void Stop()
    {
        session.Cancel();
        speaker?.Reset();
    }

    public void SetVoice(bool on) => voice = on && speaker is not null;

    public string SetMic(bool on)
    {
        if (listener is null)
            return "";
        listener.Enabled = on;
        return listener.State;
    }

    public void Hear(string b64) => listener?.Hear(b64);

    public void Clear() => session.Clear();

    /// <summary>止めるとき: まだまとめていない会話を記憶にまとめてから、部品を止める。</summary>
    public async Task ShutdownAsync()
    {
        stop.Cancel();
        session.Cancel();
        if (memory is not null && ready.Task.IsCompletedSuccessfully && ready.Task.Result)
        {
            try
            {
                await memory.ConsolidateAsync(tidy: true).WaitAsync(TimeSpan.FromMinutes(10));
            }
            catch (Exception e)
            {
                Log.Warn($"記憶をまとめられませんでした（次に起動したときにまとめます）: {e.Message}");
            }
        }
        Dispose();
    }

    public void Dispose()
    {
        stop.Cancel();
        parts.Dispose();
        memory?.Dispose();
    }
}
