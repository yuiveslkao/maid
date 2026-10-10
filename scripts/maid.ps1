# maid 管理スクリプト（Windows PowerShell 5.1 以降）
#
#   必要なものは全部 maid\runtime\ に入れる。インストーラー・PATH の変更・winget は使わない。
#   片付けるときは maid フォルダを消せばよい（例外は docs\SETUP_WINDOWS.md の「片付け方」を参照）。
#
#   maid.bat setup            必要なものを runtime\ にダウンロードする（何度実行してもよい）
#   maid.bat check            そろっているか確認する
#   maid.bat start            起動する（start.bat と同じ）
#   maid.bat bench            応答速度を測る
#   maid.bat add-voice <URL>  AivisHub の声モデルを追加する
#   maid.bat discord-check    Discord が通話中と判定されるか確認する
#   maid.bat doctor           起動中に、返事が来ない・声が出ないなどの原因を調べる
#   maid.bat stop             裏に残っている maid の部品を全部止める
#   maid.bat models           取得済みの頭脳のモデル一覧
#   maid.bat remove-model <名前>  頭脳のモデルを消す

param(
    [Parameter(Position = 0)][string]$Command = "help",
    [Parameter(Position = 1)][string]$Arg = "",
    [string]$Model = "",             # Ollama のモデル（省略時は maid.settings.json）
    [string]$Voice = "",             # AivisSpeech のスタイル ID か「話者名/スタイル名」（省略時は maid.settings.json）
    [double]$Speed = 0,
    [switch]$AivisGpu,               # AivisSpeech を GPU (DirectML) で動かす（VRAM を食う）
    [switch]$NoDiscordGate,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # PowerShell 5.1 のダウンロードが遅くなるのを防ぐ
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$Root = Split-Path -Parent $PSScriptRoot
$Rt = Join-Path $Root "runtime"
$P = @{
    Uv          = "$Rt\uv\uv.exe"
    SevenZip    = "$Rt\tools\7zr.exe"
    Ollama      = "$Rt\ollama\ollama.exe"
    OllamaModel = "$Rt\ollama-models"
    AivisDir    = "$Rt\aivisspeech-engine"
    Olv         = "$Rt\Open-LLM-VTuber"
    Downloads   = "$Rt\downloads"
}
# --- いつも使う設定（maid.settings.json。無ければ作る。コマンドの -Model などが優先） ---
$SettingsPath = Join-Path $Root "maid.settings.json"
if (-not (Test-Path $SettingsPath)) {
    $default = "{`n  `"model`": `"qwen3:8b`",`n  `"voice`": `"コハク/あまあま`",`n  `"speed`": 1.0`n}`n"
    [IO.File]::WriteAllText($SettingsPath, $default, (New-Object Text.UTF8Encoding $false))
}
$Settings = [IO.File]::ReadAllText($SettingsPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
if (-not $Model) { $Model = if ($Settings.model) { [string]$Settings.model } else { "qwen3:8b" } }
if (-not $Voice) { $Voice = if ($Settings.voice) { [string]$Settings.voice } else { "default" } }
if ($Speed -le 0) { $Speed = if ($Settings.speed) { [double]$Settings.speed } else { 1.0 } }

$OlvTag = "v1.2.1"
$OlvFrontendCommit = "06a659b114fff788cf0daaa86e484576db4975bf"   # v1.2.1 が参照しているビルド済み画面

# --- このプロセスの中だけで有効な環境変数（Windows の設定は変えない） ---
$env:UV_CACHE_DIR = "$Rt\cache\uv"
$env:UV_PYTHON_INSTALL_DIR = "$Rt\python"
$env:UV_PYTHON_BIN_DIR = "$Rt\python\bin"
$env:UV_PYTHON_INSTALL_REGISTRY = "0"       # Python をレジストリに登録しない
$env:UV_PYTHON_PREFERENCE = "only-managed"  # PC に入っている Python は使わない
$env:UV_TOOL_DIR = "$Rt\uv-tools"
$env:UV_TOOL_BIN_DIR = "$Rt\uv-tools\bin"
$env:HF_HOME = "$Rt\cache\huggingface"
$env:MODELSCOPE_CACHE = "$Rt\cache\modelscope"
$env:TORCH_HOME = "$Rt\cache\torch"
$env:OLLAMA_MODELS = $P.OllamaModel
# 本物の Ollama は 11436 で動かし、いつもの 11434 には「頭脳の中継」(bridge\llm_proxy.py) を置く。
# 中継が「考えるモード」を切るので返事が速くなる。Open-LLM-VTuber の設定は 11434 のままでよい。
$OllamaPort = 11436
$env:OLLAMA_HOST = "127.0.0.1:$OllamaPort"
$env:OLLAMA_KEEP_ALIVE = "-1"   # モデルを GPU に載せっぱなしにする（外れると読み込み直しに 1 分以上かかる）
$env:PYTHONUNBUFFERED = "1"
$env:PATH = "$Rt\uv;$Rt\uv-tools\bin;$Rt\ollama;$env:PATH"

function Say([string]$msg, [string]$color = "Gray") { Write-Host $msg -ForegroundColor $color }

function Test-Port([int]$Port) {
    try { $c = New-Object Net.Sockets.TcpClient; $c.Connect("127.0.0.1", $Port); $c.Close(); return $true } catch { return $false }
}

function Wait-Port([int]$Port, [int]$Seconds = 120, [string]$Name = "") {
    for ($i = 0; $i -lt $Seconds; $i++) {
        if (Test-Port $Port) { return $true }
        if ($i -eq 0 -and $Name) { Say "  $Name の起動を待っています..." }
        Start-Sleep -Seconds 1
    }
    return $false
}

function Get-File([string]$Url, [string]$Out) {
    if (Test-Path $Out) { return }
    New-Item -ItemType Directory -Force -Path (Split-Path $Out) | Out-Null
    Say "  ダウンロード: $Url"
    & curl.exe -L --fail --retry 3 -o "$Out.part" $Url
    if ($LASTEXITCODE -ne 0) { Remove-Item -Force -ErrorAction SilentlyContinue "$Out.part"; throw "ダウンロードに失敗しました: $Url" }
    Move-Item -Force "$Out.part" $Out
}

function Expand-Zip([string]$Zip, [string]$Dest) {
    $tmp = "$Dest.tmp"
    if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
    # .NET の zip 展開を直接使う（Expand-Archive より速く、tar.exe と違って日本語・中国語のファイル名も扱える）
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::ExtractToDirectory($Zip, $tmp)
    # zip の中身が 1 フォルダだけならその中身を Dest にする
    $items = @(Get-ChildItem $tmp)
    $src = if ($items.Count -eq 1 -and $items[0].PSIsContainer) { $items[0].FullName } else { $tmp }
    if (Test-Path $Dest) { Remove-Item -Recurse -Force $Dest }
    Move-Item $src $Dest
    if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
}

function Find-AivisExe {
    $exe = Get-ChildItem -Path $P.AivisDir -Filter run.exe -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($exe) { return $exe.FullName }
    return $null
}

# 裏で動く部品は、それぞれ最小化したウィンドウで動かす。
# 落ちたときはウィンドウが閉じずにエラーが読めるよう、失敗したら pause する。
function Start-Window([string]$Title, [string]$Exe, [string[]]$ArgList) {
    $cmdline = "`"$Exe`" " + ($ArgList -join " ")
    Start-Process -FilePath "cmd.exe" -ArgumentList "/s /c `"title maid-$Title & $cmdline || pause`"" -WindowStyle Minimized
}

function Stop-Maid {
    # 先に maid が開いたウィンドウ（cmd）を閉じる。中身を先に止めると「何かキーを押してください」で残るため
    Get-CimInstance Win32_Process -Filter "Name='cmd.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*title maid-*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    # runtime\ の中のプログラムだけを止める
    $procs = @(Get-Process | Where-Object { $_.Path -and $_.Path.StartsWith($Rt, [StringComparison]::OrdinalIgnoreCase) })
    foreach ($pr in $procs) { Stop-Process -Id $pr.Id -Force -ErrorAction SilentlyContinue }
    return $procs.Count
}

function Start-OllamaServer {
    if (Test-Port $OllamaPort) { return }
    if (-not (Test-Path $P.Ollama)) { throw "Ollama がありません。先に setup.bat を実行してください。" }
    Start-Window "ollama" $P.Ollama @("serve")
    if (-not (Wait-Port $OllamaPort 60 "Ollama")) { throw "Ollama が起動しませんでした。" }
}

function Start-Brain {
    # 前の版の maid が 11434 で Ollama を直接動かしていたら止める（中継を置くため）
    if ((Test-Port 11434) -and -not (Test-Port $OllamaPort)) {
        Get-Process -Name ollama* -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.StartsWith($Rt, [StringComparison]::OrdinalIgnoreCase) } | Stop-Process -Force
        Start-Sleep -Seconds 2
    }
    Start-OllamaServer
    if (-not (Test-Port 11434)) {
        # conf.yaml のモデル名に関係なく、maid.settings.json のモデルを使わせる
        $a = @("run", "--no-project", "--python", $PyVer, "python", "`"$Root\bridge\llm_proxy.py`"", "--port", "11434", "--ollama", "http://127.0.0.1:$OllamaPort", "--model", "`"$Model`"")
        Start-Window "brain-proxy" $P.Uv $a
        if (-not (Wait-Port 11434 120 "頭脳の中継")) { throw "頭脳の中継が起動しませんでした（maid-brain-proxy のウィンドウを確認してください）。" }
    }
}

function Start-Aivis {
    if (Test-Port 10101) { return }
    $exe = Find-AivisExe
    if (-not $exe) { throw "AivisSpeech Engine がありません。先に maid.bat setup を実行してください。" }
    $a = @("--load_all_models")
    if ($AivisGpu) { $a += "--use_gpu" }
    Start-Window "aivisspeech" $exe $a
    # 初回はモデル（約 1GB）をダウンロードするので長めに待つ
    if (-not (Wait-Port 10101 900 "AivisSpeech Engine（初回は数分かかります）")) { throw "AivisSpeech Engine が起動しませんでした。" }
}

# ブリッジ・ゲート・計測も Open-LLM-VTuber と同じ Python 3.10 を使う（Python を 2 つ入れないため）
$PyVer = "3.10"

function Invoke-UvPython([string[]]$PyArgs, [string[]]$With = @()) {
    $a = @("run", "--no-project", "--python", $PyVer)
    foreach ($w in $With) { $a += @("--with", $w) }
    & $P.Uv @($a + @("python") + $PyArgs)
}

# ------------------------------------------------------------------ setup
function Invoke-Setup {
    New-Item -ItemType Directory -Force -Path $Rt, $P.Downloads | Out-Null
    $dl = $P.Downloads

    Say "[1/6] uv（Python とライブラリの管理。Python も runtime\ に入れる）" Cyan
    if (-not (Test-Path $P.Uv)) {
        Get-File "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip" "$dl\uv.zip"
        Expand-Zip "$dl\uv.zip" "$Rt\uv"
    }

    Say "[2/6] Ollama（頭脳を動かす）" Cyan
    if (-not (Test-Path $P.Ollama)) {
        Get-File "https://github.com/ollama/ollama/releases/latest/download/ollama-windows-amd64.zip" "$dl\ollama.zip"
        Expand-Zip "$dl\ollama.zip" "$Rt\ollama"
    }

    Say "[3/6] AivisSpeech Engine（声）" Cyan
    if (-not (Find-AivisExe)) {
        Get-File "https://www.7-zip.org/a/7zr.exe" $P.SevenZip   # 7z 展開用（公式の単体版。インストール不要）
        $rel = Invoke-RestMethod "https://api.github.com/repos/Aivis-Project/AivisSpeech-Engine/releases/latest"
        $parts = @($rel.assets | Where-Object { $_.name -like "AivisSpeech-Engine-Windows-x64-*.7z.*" -and $_.name -notlike "*.txt" } | Sort-Object name)
        if ($parts.Count -eq 0) { throw "AivisSpeech Engine の Windows 版が最新リリース ($($rel.tag_name)) に見つかりません。" }
        foreach ($a in $parts) { Get-File $a.browser_download_url "$dl\$($a.name)" }
        $tmp = "$($P.AivisDir).tmp"
        if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
        & $P.SevenZip x "$dl\$($parts[0].name)" "-o$tmp" -y | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "AivisSpeech Engine の展開に失敗しました。" }
        if (Test-Path $P.AivisDir) { Remove-Item -Recurse -Force $P.AivisDir }
        Move-Item $tmp $P.AivisDir
    }

    Say "[4/6] Open-LLM-VTuber $OlvTag（基盤）" Cyan
    if (-not (Test-Path "$($P.Olv)\run_server.py")) {
        Get-File "https://github.com/Open-LLM-VTuber/Open-LLM-VTuber/archive/refs/tags/$OlvTag.zip" "$dl\olv-$OlvTag.zip"
        Expand-Zip "$dl\olv-$OlvTag.zip" $P.Olv
    }
    if (-not (Test-Path "$($P.Olv)\frontend\index.html")) {
        Get-File "https://github.com/Open-LLM-VTuber/Open-LLM-VTuber-Web/archive/$OlvFrontendCommit.zip" "$dl\olv-frontend.zip"
        Expand-Zip "$dl\olv-frontend.zip" "$($P.Olv)\frontend"
    }
    if (-not (Test-Path "$($P.Olv)\conf.yaml")) {
        Copy-Item "$Root\config\open-llm-vtuber\conf.yaml" "$($P.Olv)\conf.yaml"
        Say "  conf.yaml をコピーしました: $($P.Olv)\conf.yaml"
    } else {
        Say "  conf.yaml は既にあるので上書きしません（作り直すなら消してから setup）"
    }

    Say "[5/6] Open-LLM-VTuber のライブラリ（数 GB。初回は時間がかかります）" Cyan
    Push-Location $P.Olv
    try { & $P.Uv sync; if ($LASTEXITCODE -ne 0) { throw "uv sync に失敗しました。" } } finally { Pop-Location }
    Invoke-UvPython @("-c", "print('ブリッジ用 Python OK')")

    Say "[6/6] 頭脳のモデル ($Model)" Cyan
    Start-OllamaServer
    # 大きいので途中で通信が切れることがある。ollama pull は続きから再開できるので数回やり直す
    for ($i = 1; $i -le 5; $i++) {
        & $P.Ollama pull $Model
        if ($LASTEXITCODE -eq 0) { break }
        if ($i -eq 5) { throw "モデル $Model の取得に失敗しました。通信を確認して、もう一度 setup.bat を実行してください（続きから再開します）。" }
        Say "  取得が途中で止まりました。やり直します ($i/5)..." Yellow
        Start-Sleep -Seconds 3
    }

    Say "`nセットアップ完了。ダウンロード済みのファイルは runtime\downloads にあります（消しても動きます）。" Green
    Say "次は maid.bat check → start.bat" Green
}

# ------------------------------------------------------------------ check
function Invoke-Check {
    $script:ok = $true
    function Row([string]$name, [bool]$good, [string]$detail, [string]$fix = "") {
        $mark = if ($good) { "OK " } else { "NG " }
        $color = if ($good) { "Green" } else { "Yellow" }
        Write-Host ("  [{0}] {1,-28} {2}" -f $mark, $name, $detail) -ForegroundColor $color
        if (-not $good -and $fix) { Write-Host "        → $fix" -ForegroundColor Yellow }
        if (-not $good) { $script:ok = $false }
    }

    Say "Windows に元からあるもの" Cyan
    Row "PowerShell" ($PSVersionTable.PSVersion.Major -ge 5) "$($PSVersionTable.PSVersion)"
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    Row "curl.exe" ([bool]$curl) $(if ($curl) { $curl.Source } else { "なし" }) "Windows 10 (1803) 以降なら標準で入っています"
    $smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    if ($smi) {
        $gpu = (& nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader) -join " / "
        Row "NVIDIA ドライバー" $true $gpu
    } else {
        Row "NVIDIA ドライバー" $false "nvidia-smi が見つかりません" "GPU ドライバーを入れてください（ゲームをしているなら入っているはず）"
    }
    $vc = Test-Path "$env:WINDIR\System32\msvcp140.dll"
    Row "Visual C++ ランタイム" $vc $(if ($vc) { "あり" } else { "なし" }) "Microsoft の「Visual C++ 再頒布可能パッケージ」が必要です（多くのゲーム・Discord と一緒に入っています）"
    $drive = Get-PSDrive -Name ($Root.Substring(0, 1))
    $freeGb = [math]::Round($drive.Free / 1GB, 1)
    Row "空き容量 ($($drive.Name):)" ($freeGb -ge 20) "$freeGb GB" "合計 15〜20GB 程度使います"

    Say "maid\runtime\ の中" Cyan
    Row "uv" (Test-Path $P.Uv) $P.Uv ".\setup.bat を実行（まだ setup していなければ正常です）"
    Row "Ollama" (Test-Path $P.Ollama) $P.Ollama ".\setup.bat を実行（まだ setup していなければ正常です）"
    $aivis = Find-AivisExe
    Row "AivisSpeech Engine" ([bool]$aivis) $(if ($aivis) { $aivis } else { "なし" }) ".\setup.bat を実行（まだ setup していなければ正常です）"
    Row "Open-LLM-VTuber" (Test-Path "$($P.Olv)\run_server.py") $P.Olv ".\setup.bat を実行（まだ setup していなければ正常です）"
    Row "  画面（frontend）" (Test-Path "$($P.Olv)\frontend\index.html") "" ".\setup.bat を実行（まだ setup していなければ正常です）"
    Row "  conf.yaml" (Test-Path "$($P.Olv)\conf.yaml") "" ".\setup.bat を実行（まだ setup していなければ正常です）"
    Row "  ライブラリ（.venv）" (Test-Path "$($P.Olv)\.venv") "" ".\setup.bat を実行（まだ setup していなければ正常です）"
    $models = @()
    if (Test-Path $P.Ollama) {
        if (Test-Port $OllamaPort) {
            try { $models = @((Invoke-RestMethod "http://127.0.0.1:$OllamaPort/api/tags").models | ForEach-Object { $_.name }) } catch { $models = @() }
        }
    }
    if ($models.Count -gt 0) {
        Row "頭脳のモデル" ($models -contains $Model) ($models -join ", ") ".\maid.bat setup -Model $Model"
    } else {
        Say "  [-- ] 頭脳のモデル                 （Ollama 停止中のため未確認。start 後にもう一度 check）"
    }

    Say "起動状態（start 後に確認）" Cyan
    foreach ($s in @(@("Ollama", $OllamaPort), @("頭脳の中継", 11434), @("AivisSpeech Engine", 10101), @("ブリッジ", 10102), @("Discord ゲート", 12394), @("Open-LLM-VTuber", 12393))) {
        $up = Test-Port $s[1]
        Write-Host ("  [{0}] {1,-28} 127.0.0.1:{2}" -f $(if ($up) { "ON " } else { "-- " }), $s[0], $s[1]) -ForegroundColor $(if ($up) { "Green" } else { "DarkGray" })
    }
    if ($script:ok) { Say "`n準備 OK。.\start.bat で起動できます" Green } else { Say "`nNG の項目を直してください" Yellow }
}

# ------------------------------------------------------------------ start
# 画面（Open-LLM-VTuber の Web 画面）の設定の初期値を、ページを開く前に入れておく。
# - WebSocket URL: Discord ゲートが使えればゲート、だめなら本体に直結（毎回 maid が決める）
# - マイク・VAD: 最初の 1 回だけ入れる（あとで画面から変えたものは上書きしない）
function Write-FrontendDefaults([string]$WsUrl) {
    $front = "$($P.Olv)\frontend"
    $index = "$front\index.html"
    if (-not (Test-Path $index)) { return }
    $js = @"
// maid が起動のたびに書き換えるファイル（scripts\maid.ps1）
(function () {
  try {
    var ls = window.localStorage;
    ls.setItem("wsUrl", JSON.stringify("$WsUrl"));
    ls.setItem("baseUrl", JSON.stringify("http://127.0.0.1:12393"));
    if (ls.getItem("maidDefaults") !== "2") {
      ls.setItem("micOn", "true");
      ls.setItem("autoStopMic", "false");            // 話している途中でも割り込めるように
      ls.setItem("autoStartMicOn", "true");          // 割り込んだあともマイクを戻す
      ls.setItem("autoStartMicOnConvEnd", "true");   // 返事が終わったらマイクを戻す（毎回クリック不要）
      ls.setItem("vadSettings", JSON.stringify({ positiveSpeechThreshold: 50, negativeSpeechThreshold: 35, redemptionFrames: 14 }));
      ls.setItem("maidDefaults", "2");
    }
  } catch (e) {}
})();
"@
    [IO.File]::WriteAllText("$front\maid-defaults.js", $js, (New-Object Text.UTF8Encoding $false))
    $html = [IO.File]::ReadAllText($index)
    if ($html -notmatch "maid-defaults\.js") {
        $html = $html -replace "<head>", "<head>`n    <script src=`"./maid-defaults.js`"></script>"
        [IO.File]::WriteAllText($index, $html, (New-Object Text.UTF8Encoding $false))
    }
}

# ゲート経由で本体につながり、最初のメッセージが返ってくるか確かめる
function Test-Gate {
    try {
        $ws = New-Object System.Net.WebSockets.ClientWebSocket
        $cts = New-Object System.Threading.CancellationTokenSource 10000
        $ws.ConnectAsync([Uri]"ws://127.0.0.1:12394/client-ws", $cts.Token).Wait()
        $buf = New-Object byte[] 65536
        $r = $ws.ReceiveAsync([ArraySegment[byte]]::new($buf), $cts.Token).Result
        try { $ws.CloseAsync([System.Net.WebSockets.WebSocketCloseStatus]::NormalClosure, "", [Threading.CancellationToken]::None).Wait(2000) | Out-Null } catch {}
        return ($r.Count -gt 0)
    } catch { return $false }
}

function Invoke-Start {
    if (-not (Test-Path "$($P.Olv)\run_server.py")) { throw "Open-LLM-VTuber がありません。先に setup.bat を実行してください。" }
    Say "頭脳（Ollama + 中継）" Cyan; Start-Brain
    Say "AivisSpeech Engine" Cyan; Start-Aivis

    Say "ブリッジ (10102)" Cyan
    if (-not (Test-Port 10102)) {
        $a = @("run", "--no-project", "--python", $PyVer, "python", "`"$Root\bridge\aivis_openai_bridge.py`"", "--voice", "`"$Voice`"", "--speed", "$Speed")
        Start-Window "bridge" $P.Uv $a
    }

    if (-not $NoDiscordGate) {
        Say "Discord ゲート (12394)" Cyan
        if (-not (Test-Port 12394)) {
            # websockets>=13 の > は cmd でリダイレクトにならないよう引用符で囲む
            $a = @("run", "--no-project", "--python", $PyVer, "--with", "`"websockets>=13`"", "python", "`"$Root\gate\discord_gate.py`"")
            Start-Window "discord-gate" $P.Uv $a
        }
    }

    $direct = "ws://127.0.0.1:12393/client-ws"
    Write-FrontendDefaults $direct

    Say "Open-LLM-VTuber (12393)" Cyan
    if (-not (Test-Port 12393)) {
        Start-Window "open-llm-vtuber" $P.Uv @("run", "--project", "`"$($P.Olv)`"", "--directory", "`"$($P.Olv)`"", "run_server.py")
    }
    if (-not (Wait-Port 12393 1200 "Open-LLM-VTuber（初回は数分かかります）")) {
        throw "Open-LLM-VTuber が起動しませんでした（maid-open-llm-vtuber のウィンドウを確認してください）。"
    }

    $wsUrl = $direct
    if (-not $NoDiscordGate) {
        if ((Wait-Port 12394 60) -and (Test-Gate)) {
            $wsUrl = "ws://127.0.0.1:12394/client-ws"
            Say "  Discord ゲート: 使用中（Discord で通話中は声に反応しません）" Green
        } else {
            Say "  Discord ゲート: うまく動かないので使わずに直結します（maid-discord-gate のウィンドウを確認してください）" Yellow
        }
    }
    Write-FrontendDefaults $wsUrl

    try {
        Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:12393/maid-defaults.js" | Out-Null
    } catch {
        Say "  画面の初期設定 (maid-defaults.js) が配信されていません。マイク設定は画面から手で変えてください。" Yellow
    }
    Say "  頭脳: $Model / 声: $Voice" Gray
    if (-not $NoBrowser) {
        # Windows 標準の Edge をアプリ風のウィンドウで開く（インストール不要）。
        # 末尾の ?t= は、Edge が古いページを使い回さないようにするため
        $t = [DateTimeOffset]::Now.ToUnixTimeSeconds()
        Start-Process "msedge.exe" "--app=http://127.0.0.1:12393/?t=$t"
    }
    Say "`n準備完了。画面を閉じても裏の部品は動き続けます。" Green
    Say "全部止めるときは、このウィンドウで Ctrl+C（または .\maid.bat stop）。" Green
    try {
        while (Test-Port 12393) { Start-Sleep -Seconds 2 }
        Say "Open-LLM-VTuber が終了しました。" Yellow
    } finally {
        $n = Stop-Maid
        Say "裏の部品を止めました ($n 個)" Gray
    }
}

# ------------------------------------------------------------------ doctor
# 起動中の部品を、声 → 頭脳 → 本体 の順に実際に動かしてみて、どこで止まっているかを表示する
function Invoke-Doctor {
    function Res([string]$name, [bool]$good, [string]$detail) {
        $mark = if ($good) { "OK " } else { "NG " }
        Write-Host ("  [{0}] {1,-22} {2}" -f $mark, $name, $detail) -ForegroundColor $(if ($good) { "Green" } else { "Yellow" })
    }
    function Post-Json([string]$url, $obj, [int]$timeout = 120) {
        $body = [Text.Encoding]::UTF8.GetBytes(($obj | ConvertTo-Json -Depth 6 -Compress))
        return Invoke-WebRequest -UseBasicParsing -Method Post -Uri $url -Body $body -ContentType "application/json; charset=utf-8" -TimeoutSec $timeout
    }
    Say "設定: 頭脳 = $Model / 声 = $Voice" Cyan

    Say "頭脳" Cyan
    $up = Test-Port $OllamaPort
    Res "Ollama (11436)" $up $(if ($up) { "起動中" } else { "止まっています → start.bat を実行（maid-ollama のウィンドウを確認）" })
    if ($up) {
        try {
            $names = @((Invoke-RestMethod "http://127.0.0.1:$OllamaPort/api/tags").models | ForEach-Object { $_.name })
            $has = ($names -contains $Model) -or ($names -contains "$($Model):latest")
            Res "モデル" $has $(if ($has) { $Model } else { "$Model が取得されていません（取得済み: $($names -join ', ')）→ .\maid.bat setup -Model $Model" })
        } catch { Res "モデル" $false "一覧を取得できません: $($_.Exception.Message)" }
    }
    $up = Test-Port 11434
    Res "頭脳の中継 (11434)" $up $(if ($up) { "起動中" } else { "止まっています（maid-brain-proxy のウィンドウを確認）" })
    if ($up) {
        try {
            $sw = [Diagnostics.Stopwatch]::StartNew()
            $r = Post-Json "http://127.0.0.1:11434/v1/chat/completions" @{ model = $Model; stream = $false; max_tokens = 40; messages = @(@{ role = "user"; content = "こんにちは。一言で返事して。" }) } 300
            $text = ([Text.Encoding]::UTF8.GetString($r.RawContentStream.ToArray()) | ConvertFrom-Json).choices[0].message.content
            Res "頭脳の返事" ([bool]$text) ("「{0}」 ({1:N1} 秒、初回はモデル読み込みで遅い)" -f $text, $sw.Elapsed.TotalSeconds)
        } catch { Res "頭脳の返事" $false "失敗: $($_.Exception.Message)" }
    }

    Say "声" Cyan
    $up = Test-Port 10101
    Res "AivisSpeech (10101)" $up $(if ($up) { "起動中" } else { "止まっています（maid-aivisspeech のウィンドウを確認）" })
    $up = Test-Port 10102
    Res "声の中継 (10102)" $up $(if ($up) { "起動中" } else { "止まっています（maid-bridge のウィンドウを確認）" })
    if ($up) {
        try {
            $r = Post-Json "http://127.0.0.1:10102/v1/audio/speech" @{ model = "aivisspeech"; voice = "default"; input = "テストです"; response_format = "wav" }
            Res "声の合成" ($r.RawContentLength -gt 1000) "$([math]::Round($r.RawContentLength / 1KB)) KB の音声ができました"
        } catch {
            $msg = $_.Exception.Message
            try { $msg = (New-Object IO.StreamReader($_.Exception.Response.GetResponseStream(), [Text.Encoding]::UTF8)).ReadToEnd() } catch {}
            Res "声の合成" $false "失敗: $msg"
        }
    }

    Say "本体と画面" Cyan
    $up = Test-Port 12393
    Res "Open-LLM-VTuber (12393)" $up $(if ($up) { "起動中" } else { "止まっています（maid-open-llm-vtuber のウィンドウを確認）" })
    if ($up) {
        try { Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:12393/maid-defaults.js" | Out-Null; Res "画面の初期設定" $true "配信されています" }
        catch { Res "画面の初期設定" $false "maid-defaults.js が配信されていません" }
    }
    if (-not $NoDiscordGate) {
        $g = (Test-Port 12394) -and (Test-Gate)
        Res "Discord ゲート (12394)" $g $(if ($g) { "本体まで通じています" } else { "使えません（画面は本体に直結されます）" })
    }
    $log = Get-ChildItem "$($P.Olv)\logs\debug_*.log" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime | Select-Object -Last 1
    if ($log) { Say "`n本体のログ: $($log.FullName)" Gray }
}

# ------------------------------------------------------------------ others
switch ($Command) {
    "setup" { Invoke-Setup }
    "check" { Invoke-Check }
    "doctor" { Invoke-Doctor }
    "start" { Invoke-Start }
    "bench" {
        Start-Brain
        $models = if ($Arg) { $Arg -split "," } else { @($Model) }
        $a = @("$Root\scripts\bench_latency.py", "--ollama", "http://127.0.0.1:$OllamaPort/v1")
        foreach ($m in $models) { $a += @("--model", $m) }
        Invoke-UvPython $a
    }
    "add-voice" {
        if (-not $Arg) { throw "使い方: maid.bat add-voice https://hub.aivis-project.com/aivm-models/..." }
        Start-Aivis
        & curl.exe --fail -sS -X POST -F "url=$Arg" "http://127.0.0.1:10101/aivm_models/install"
        if ($LASTEXITCODE -ne 0) { throw "追加に失敗しました。URL を確認してください。" }
        Say "追加しました。ブリッジ起動中なら http://127.0.0.1:10102/v1/voices で名前を確認できます。" Green
    }
    "stop" {
        $n = Stop-Maid
        Say "止めました ($n 個)" Green
    }
    "models" {
        Start-OllamaServer
        & $P.Ollama list
    }
    "remove-model" {
        if (-not $Arg) { throw "使い方: maid.bat remove-model <モデル名>" }
        Start-OllamaServer
        & $P.Ollama rm $Arg
    }
    "discord-check" { Invoke-UvPython @("$Root\gate\discord_gate.py", "--check") @("websockets>=13") }
    default {
        Get-Content $PSCommandPath -Encoding UTF8 | Select-Object -First 16 | ForEach-Object { $_ -replace "^#\s?", "" }
    }
}
