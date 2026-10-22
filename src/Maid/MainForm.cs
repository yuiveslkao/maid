using System.Reflection;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace Maid;

/// <summary>
/// maid のウィンドウ。中身は exe に埋め込んだ画面（ui\*.html）を WebView2（Windows に入っている Edge の部品）で出す。
/// セットアップ → 起動の画面 → 会話、を同じウィンドウで切り替える。画面の設定とマイクの許可は runtime\webview に保存する。
/// 画面から C# は chrome.webview.postMessage（ui\host.js）、C# から画面は window.maid.&lt;関数&gt;(...) で呼ぶ。
/// </summary>
sealed class MainForm : Form
{
    const string Host = "https://maid.local/";
    static readonly JsonSerializerOptions Json = new() { Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping };

    readonly MaidPaths paths;
    readonly Settings settings;
    readonly WebView2 web = new() { Dock = DockStyle.Fill };
    readonly Dictionary<string, Func<JsonArray, Task<JsonNode?>>> api = [];
    Conversation? conversation;
    Setup? setup;
    string afterSetup = "launcher";
    bool closing, closed, settingUp;

    public MainForm(MaidPaths paths, Settings settings)
    {
        this.paths = paths;
        this.settings = settings;
        Text = "maid";
        BackColor = System.Drawing.Color.FromArgb(0xff, 0xf7, 0xfa);
        Controls.Add(web);
        Load += async (_, _) =>
        {
            try
            {
                await InitAsync();
            }
            catch (WebView2RuntimeNotFoundException)
            {
                Log.Warn("WebView2 Runtime がありません");
                MessageBox.Show("画面の表示に使う WebView2 Runtime が見つかりません。\nWindows Update を済ませるか、Microsoft の WebView2 Runtime を入れてください。",
                                "maid", MessageBoxButtons.OK, MessageBoxIcon.Error);
                Close();
            }
            catch (Exception e)
            {
                Log.Error("画面を準備できませんでした", e);
                MessageBox.Show($"画面を準備できませんでした: {e.Message}", "maid", MessageBoxButtons.OK, MessageBoxIcon.Error);
                Close();
            }
        };
        FormClosing += OnClosing;
        Register();
    }

    async Task InitAsync()
    {
        try
        {
            CoreWebView2Environment.SetLoaderDllFolderPath(Path.Combine(paths.Root, "lib"));
        }
        catch (Exception e) when (e is InvalidOperationException or ArgumentException)
        {
        }
        var options = new CoreWebView2EnvironmentOptions("--autoplay-policy=no-user-gesture-required");  // キャラから話しかけた声も鳴らす
        var env = await CoreWebView2Environment.CreateAsync(null, paths.WebView, options);
        await web.EnsureCoreWebView2Async(env);
        var core = web.CoreWebView2;
        core.Settings.IsStatusBarEnabled = false;
        core.Settings.AreDevToolsEnabled = false;
        core.Settings.IsZoomControlEnabled = false;
        core.AddWebResourceRequestedFilter(Host + "*", CoreWebView2WebResourceContext.All);
        core.WebResourceRequested += (_, e) => e.Response = Serve(env, e.Request.Uri);
        core.PermissionRequested += (_, e) =>
        {
            if (e.PermissionKind == CoreWebView2PermissionKind.Microphone)
                e.State = CoreWebView2PermissionState.Allow;  // 自分の画面だけなので、マイクは聞かずに許可する
        };
        core.NewWindowRequested += (_, e) =>
        {
            e.Handled = true;
            OpenUrl(e.Uri);
        };
        core.NavigationStarting += (_, e) =>
        {
            if (!e.Uri.StartsWith(Host))  // 画面の外へは移らない（リンクはいつものブラウザで開く）
            {
                e.Cancel = true;
                OpenUrl(e.Uri);
            }
        };
        core.WebMessageReceived += async (_, e) => await OnMessageAsync(e.WebMessageAsJson);
        await core.AddScriptToExecuteOnDocumentCreatedAsync(Resource("ui/host.js"));
        Show(Setup.Missing(paths, settings).Count > 0 ? "setup" : "launcher");
    }

    void Show(string page)
    {
        var (w, h, title) = page switch
        {
            "setup" => (640, 720, "maid のセットアップ"),
            "launcher" => (580, 720, $"{Character.Load(paths.CharacterFile).Name} を起動"),
            _ => (760, 860, Character.Load(paths.CharacterFile).Name),
        };
        Text = title;
        ClientSize = new System.Drawing.Size(LogicalToDeviceUnits(w), LogicalToDeviceUnits(h));
        web.CoreWebView2.Navigate($"{Host}{page}.html");
    }

    static string Resource(string name)
    {
        using var s = Assembly.GetExecutingAssembly().GetManifestResourceStream(name) ?? throw new FileNotFoundException(name);
        using var r = new StreamReader(s);
        return r.ReadToEnd();
    }

    static CoreWebView2WebResourceResponse Serve(CoreWebView2Environment env, string uri)
    {
        var path = new Uri(uri).AbsolutePath.TrimStart('/');
        var stream = Assembly.GetExecutingAssembly().GetManifestResourceStream("ui/" + path);
        if (stream is null)
            return env.CreateWebResourceResponse(null, 404, "Not Found", "");
        var type = Path.GetExtension(path) switch
        {
            ".html" => "text/html; charset=utf-8",
            ".js" => "text/javascript; charset=utf-8",
            ".css" => "text/css; charset=utf-8",
            ".png" => "image/png",
            _ => "application/octet-stream",
        };
        return env.CreateWebResourceResponse(stream, 200, "OK", $"Content-Type: {type}\nCache-Control: no-store");
    }

    // ---------------------------------------------------------------- 画面とのやりとり
    /// <summary>画面の関数を呼ぶ（どのスレッドからでもよい。呼んだ順に届く）。</summary>
    public void Js(string fn, object?[] args)
    {
        var script = $"window.maid && maid.{fn}({string.Join(",", args.Select(a => JsonSerializer.Serialize(a, Json)))})";
        void Run()
        {
            if (!closed && web.CoreWebView2 is not null)
                _ = web.CoreWebView2.ExecuteScriptAsync(script);
        }
        if (InvokeRequired)
            BeginInvoke(Run);
        else
            Run();
    }

    async Task OnMessageAsync(string json)
    {
        var msg = JsonNode.Parse(json);
        var id = msg?["id"];
        var method = msg?["method"]?.GetValue<string>() ?? "";
        var args = msg?["args"]?.AsArray() ?? [];
        var reply = new JsonObject { ["id"] = id?.DeepClone() };
        try
        {
            if (!api.TryGetValue(method, out var handler))
                throw new InvalidOperationException($"{method} はありません");
            reply["result"] = await handler(args);
        }
        catch (Exception e)
        {
            Log.Error($"画面からの呼び出し {method} に失敗しました", e);
            reply["error"] = e.Message;
        }
        if (id is not null && !closed)
            web.CoreWebView2.PostWebMessageAsJson(reply.ToJsonString());
    }

    static string Str(JsonArray a, int i) => a.Count > i && a[i] is JsonValue v && v.TryGetValue(out string? s) ? s : "";
    static bool Bool(JsonArray a, int i) => a.Count > i && a[i] is JsonValue v && v.TryGetValue(out bool b) && b;

    void On(string name, Func<JsonArray, JsonNode?> f) => api[name] = a => Task.FromResult(f(a));
    void OnAsync(string name, Func<JsonArray, Task<JsonNode?>> f) => api[name] = f;

    void Register()
    {
        On("open_url", a => { OpenUrl(Str(a, 0)); return null; });

        // セットアップ
        On("setup_info", _ =>
        {
            setup ??= new Setup(paths, settings, (key, text, ratio) => Js("progress", [key, text, ratio]));
            return new JsonObject
            {
                ["needs"] = new JsonArray(Setup.Missing(paths, settings).Select(n => (JsonNode)new JsonObject
                    { ["key"] = n.Key, ["title"] = n.Title, ["size"] = n.Size }).ToArray()),
                ["leftovers"] = new JsonArray(setup.Leftovers().Select(l => (JsonNode)new JsonObject
                    { ["path"] = Path.GetRelativePath(paths.Root, l.Path), ["mb"] = l.Bytes / 1_000_000 }).ToArray()),
            };
        });
        OnAsync("setup_start", async _ =>
        {
            setup ??= new Setup(paths, settings, (key, text, ratio) => Js("progress", [key, text, ratio]));
            if (settingUp)
                return null;  // もう動いている
            settingUp = true;
            try
            {
                await Task.Run(() => setup.RunAsync(CancellationToken.None));
                Js("setupDone", []);
            }
            catch (Exception e)
            {
                Log.Error("セットアップに失敗しました", e);
                Js("setupError", [e.Message]);
            }
            finally
            {
                settingUp = false;
            }
            return null;
        });
        OnAsync("remove_leftovers", async _ =>
        {
            await Task.Run(() => setup?.RemoveLeftovers());
            return null;
        });
        On("setup_next", _ => { Next(afterSetup); return null; });

        // 起動の画面
        var icons = new Icons(paths.Icons);
        On("load", _ => new JsonObject
        {
            ["voice_reply"] = settings.VoiceReply, ["mic"] = settings.Mic, ["search"] = settings.Search, ["memory"] = settings.Memory,
            ["proactive_minutes"] = settings.ProactiveMinutes, ["name"] = Character.Load(paths.CharacterFile).Name,
            ["icons"] = new JsonObject { ["character"] = icons.DataUrl("character"), ["user"] = icons.DataUrl("user") },
        });
        On("save_icon", a => icons.Save(Str(a, 0), Str(a, 1)));
        On("remove_icon", a => { icons.Remove(Str(a, 0)); return null; });
        On("launch", a =>
        {
            var choice = a.Count > 0 ? a[0] as JsonObject : null;
            var keys = new[] { "voice_reply", "mic", "search", "memory", "proactive_minutes" };
            settings.Update(keys.Where(k => choice?.ContainsKey(k) == true).ToDictionary(k => k, k => choice![k]));
            Next("chat");
            return null;
        });
        On("quit", _ => { Close(); return null; });

        // 会話
        On("init", _ => conversation?.Init());
        On("send", a =>
        {
            conversation?.Send(Str(a, 0), (a.Count > 1 ? a[1] as JsonArray : null)?.Select(x => x?.GetValue<string>() ?? "").Where(x => x.Length > 0).ToList() ?? []);
            return null;
        });
        OnAsync("capture", async _ => await CaptureAsync());
        On("shrink", a => ScreenShot.Shrink(Str(a, 0)));
        On("stop", _ => { conversation?.Stop(); return null; });
        On("set_voice", a => { conversation?.SetVoice(Bool(a, 0)); return null; });
        On("set_mic", a => conversation?.SetMic(Bool(a, 0)));
        On("hear", a => { conversation?.Hear(Str(a, 0)); return null; });
        On("mic_error", a => { Log.Warn($"マイクを使えませんでした: {Str(a, 0)}"); return null; });
        On("clear", _ => { conversation?.Clear(); return null; });
    }

    /// <summary>次の画面へ。足りないものがあれば、先にセットアップ。</summary>
    void Next(string page)
    {
        if (Setup.Missing(paths, settings).Count > 0)
        {
            afterSetup = page;
            Show("setup");
            return;
        }
        if (page == "chat" && conversation is null)
        {
            conversation = new Conversation(paths, settings, Js, CaptureAsync);
            _ = Task.Run(conversation.StartAsync);
        }
        Show(page);
    }

    /// <summary>画面を撮る。自分のウィンドウが写らないように、撮る間だけ隠す。</summary>
    Task<string> CaptureAsync()
    {
        var done = new TaskCompletionSource<string>();
        BeginInvoke(async () =>
        {
            try
            {
                Hide();
                await Task.Delay(400);  // 隠れきるのを待つ
                done.SetResult(ScreenShot.Capture());
            }
            catch (Exception e)
            {
                done.SetException(e);
            }
            finally
            {
                Show();
                Activate();
            }
        });
        return done.Task;
    }

    static void OpenUrl(string url)
    {
        if (url.StartsWith("http://") || url.StartsWith("https://"))
            System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo(url) { UseShellExecute = true });
    }

    /// <summary>閉じるとき: 記憶をまとめてから部品を止める（もう一度閉じると、まとめずにすぐ終わる）。</summary>
    async void OnClosing(object? sender, FormClosingEventArgs e)
    {
        if (closed || conversation is null)
        {
            closed = true;
            return;
        }
        e.Cancel = true;
        if (closing)
        {
            conversation.Dispose();
            closed = true;
            Close();
            return;
        }
        closing = true;
        Js("closing", []);
        await conversation.ShutdownAsync();
        closed = true;
        Close();
    }
}
