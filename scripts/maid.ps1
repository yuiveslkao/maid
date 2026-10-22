# maid 管理スクリプト（Windows PowerShell 5.1 以降）
#
#   必要なものは全部 maid\runtime\ に入れる。インストーラー・PATH の変更・winget は使わない。
#   片付けるときは maid フォルダを消せばよい（例外は docs\SETUP_WINDOWS.md の「片付け方」を参照）。
#
#   maid.bat setup            必要なものを runtime\ にダウンロードする（何度実行してもよい）
#   maid.bat start            起動の画面を出してから起動する（start.bat と同じ。使うもの・アイコンを選ぶ）
#   maid.bat run              起動の画面を出さずに、いつもの設定ですぐ起動する
#   maid.bat stop             裏に残っている maid の部品を全部止める
#   maid.bat doctor           起動中に、返事が来ない・声が出ないなどの原因を調べる
#   maid.bat logs <名前>      裏の部品のログを別ウィンドウで見る（app / brain-proxy / tts-bridge など）
#   maid.bat clean            ログ・一時ファイル・使っていないものを消す（-History で会話の画面の履歴も）
#   maid.bat clean -Memory    覚えたこと（メモ・前の会話の要約）だけを消し、会話の記録から覚え直させる

param(
    [Parameter(Position = 0)][string]$Command = "help",
    [Parameter(Position = 1)][string]$Arg = "",
    [switch]$History,                # clean で会話の画面の履歴も消す
    [switch]$Memory                  # clean で覚えたことを消して、会話の記録から覚え直させる
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # PowerShell 5.1 のダウンロードが遅くなるのを防ぐ
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

# ------------------------------------------------------------------ 場所・ポート
$Root = Split-Path -Parent $PSScriptRoot
$Rt = Join-Path $Root "runtime"
$Svc = Join-Path $Root "services"
$ReazonName = "sherpa-onnx-zipformer-ja-reazonspeech-2024-08-01"   # 音声認識のモデル（日本語専用）
$P = @{
    Uv          = "$Rt\uv\uv.exe"
    SevenZip    = "$Rt\tools\7zr.exe"
    Ollama      = "$Rt\ollama\ollama.exe"
    OllamaModel = "$Rt\ollama-models"
    AivisDir    = "$Rt\aivisspeech-engine"
    MaidVenv    = "$Rt\maid-venv"
    MaidPy      = "$Rt\maid-venv\Scripts\python.exe"                # maid の Python
    Asr         = "$Rt\models\$ReazonName"                          # 声を文字にする
    SileroVad   = "$Rt\models\silero_vad.onnx"                      # 話しはじめ・話し終わりを見つける
    Old         = "$Rt\Open-LLM-VTuber"                             # 前に使っていた基盤（clean で消す）
    Downloads   = "$Rt\downloads"
    Logs        = "$Rt\logs"
}
# 本物の Ollama は 11436 に、頭脳の中継 (services\llm_proxy.py) を 11434 に置く
$Ports = @{
    Ollama     = 11436
    BrainProxy = 11434
    Aivis      = 10101
    TtsBridge  = 10102
}
$Ports["brain-proxy"] = $Ports.BrainProxy; $Ports["tts-bridge"] = $Ports.TtsBridge
$PyVer = "3.10"
$AsrFiles = @("encoder-epoch-99-avg-1.int8.onnx", "decoder-epoch-99-avg-1.int8.onnx", "joiner-epoch-99-avg-1.int8.onnx", "tokens.txt")
# maid が使うライブラリ（検索・本文の抜き出し・PDF / 画面（WebView2）・画像・キャラの設定 / 声を文字にする）
$MaidPackages = @("ddgs", "trafilatura", "pypdf", "pywebview", "pillow", "pyyaml", "sherpa-onnx", "numpy")

# ------------------------------------------------------------------ 設定（maid.settings.json）
# 無ければ config\maid.settings.default.json をコピーして作る。書かれていない項目は既定値を使う
$SettingsPath = Join-Path $Root "maid.settings.json"
$DefaultSettingsPath = Join-Path $Root "config\maid.settings.default.json"
if (-not (Test-Path $SettingsPath)) { Copy-Item $DefaultSettingsPath $SettingsPath }
$Defaults = [IO.File]::ReadAllText($DefaultSettingsPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
$UserSettings = [IO.File]::ReadAllText($SettingsPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
function Get-Setting([string]$Name) {
    $v = $UserSettings.$Name
    if ($null -eq $v -or "$v" -eq "") { $v = $Defaults.$Name }
    return $v
}
$Model = [string](Get-Setting "model")                 # 頭脳のモデル（Ollama の名前）
$Voice = [string](Get-Setting "voice")                 # 声（話者名/スタイル名）
$Speed = [double](Get-Setting "speed")                 # 話す速さ
$SilenceMs = [int](Get-Setting "silence_ms")           # 無音がこの長さ続いたら話し終わり（ミリ秒）
$Search = [string](Get-Setting "search")               # auto / explicit / off
$SearchFiller = [string]$UserSettings.search_filler    # 調べる前にすぐ言う一言（空なら言わない）
if ($null -eq $UserSettings.search_filler) { $SearchFiller = [string]$Defaults.search_filler }
$UseMemory = [bool](Get-Setting "memory")              # 覚える（会話から、あなたのことや今のことを覚える）
$VoiceReply = [bool](Get-Setting "voice_reply")        # 声で返事する
$Mic = [bool](Get-Setting "mic")                       # マイクで聞く
$ProactiveMinutes = [double](Get-Setting "proactive_minutes")  # 会話がこれだけ無かったらキャラから話しかける（0 で話しかけない）
$EmbedModel = [string]$UserSettings.embed_model           # 記憶を探すモデル（空なら言葉の重なりだけで探す）
if ($null -eq $UserSettings.embed_model) { $EmbedModel = [string]$Defaults.embed_model }
$DataDir = Join-Path $Root "data"                      # 記憶・会話・アイコン。git には入れない

# ------------------------------------------------------------------ このプロセスの中だけの環境変数（Windows の設定は変えない）
$env:UV_CACHE_DIR = "$Rt\cache\uv"
$env:UV_PYTHON_INSTALL_DIR = "$Rt\python"
$env:UV_PYTHON_BIN_DIR = "$Rt\python\bin"
$env:UV_PYTHON_INSTALL_REGISTRY = "0"       # Python をレジストリに登録しない
$env:UV_PYTHON_PREFERENCE = "only-managed"  # PC に入っている Python は使わない
$env:OLLAMA_MODELS = $P.OllamaModel
$env:OLLAMA_HOST = "127.0.0.1:$($Ports.Ollama)"
$env:OLLAMA_KEEP_ALIVE = "-1"   # 頭脳のモデルを GPU に載せっぱなしにする（外れると読み込み直しに 1 分以上かかる）
$env:PYTHONUNBUFFERED = "1"
$env:PYTHONIOENCODING = "utf-8"   # ログを UTF-8 で書かせる（Windows の Python は Shift-JIS で書くので logs で文字化けする）

# ------------------------------------------------------------------ 共通
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

# maid の Python 環境（runtime\maid-venv）。ライブラリの一覧が変わったら作り直す（使わなくなったものを残さないため。
# ダウンロードしたものは runtime\cache に残っているので速い）
function Install-MaidVenv {
    $stamp = "$($P.MaidVenv)\maid-packages.txt"
    $want = $MaidPackages -join " "
    if ((Test-Path $P.MaidPy) -and (Test-Path $stamp) -and ((Get-Content $stamp -Raw).Trim() -eq $want)) { return }
    Say "  maid のライブラリを入れます（初回と、使うライブラリが変わったときだけ）" Cyan
    if (Test-Path $P.MaidVenv) { Remove-Item -Recurse -Force $P.MaidVenv }
    & $P.Uv venv --python $PyVer $P.MaidVenv | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "maid の Python を作れませんでした。" }
    & $P.Uv pip install --python $P.MaidPy @MaidPackages | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "maid のライブラリを入れられませんでした。" }
    Set-Content -Path $stamp -Value $want -Encoding ASCII
}

# 日本語の音声認識モデル (ReazonSpeech, sherpa-onnx 版。初回だけ約 700MB)
function Install-ReazonSpeech {
    if (Test-Path "$($P.Asr)\tokens.txt") { return }
    $old = "$($P.Old)\models\$ReazonName"
    if (Test-Path "$old\tokens.txt") {
        # 前に Open-LLM-VTuber の中に置いていたものを引っ越す（ダウンロードし直さない）
        New-Item -ItemType Directory -Force -Path (Split-Path $P.Asr) | Out-Null
        Move-Item $old $P.Asr
        return
    }
    Say "  日本語の音声認識モデル (ReazonSpeech) をダウンロードします（約 700MB、初回だけ）" Cyan
    $archive = "$($P.Downloads)\$ReazonName.tar.bz2"
    Get-File "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/$ReazonName.tar.bz2" $archive
    New-Item -ItemType Directory -Force -Path (Split-Path $P.Asr) | Out-Null
    # 必要なファイルだけ取り出す（tar.exe は Windows に標準で入っている）
    & tar.exe -xjf $archive -C (Split-Path $P.Asr) @($AsrFiles | ForEach-Object { "$ReazonName/$_" })
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path "$($P.Asr)\tokens.txt")) { throw "音声認識モデルの展開に失敗しました。" }
    Remove-Item -Force $archive -ErrorAction SilentlyContinue   # 使うのは展開したものだけ
}

function Install-SileroVad {
    Get-File "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx" $P.SileroVad
}

# 裏で動く部品はウィンドウを出さず、出力を runtime\logs\<名前>.log に書く。
# （コンソールに出すと、ウィンドウをクリックして「選択」状態になったとき部品ごと止まってしまうため）
# start.bat のウィンドウにぶら下がるので、そこで Ctrl+C すると一緒に止まる
function Start-Part([string]$Name, [string]$Exe, [string[]]$ArgList, [string]$Dir = $Root, [switch]$PassThru) {
    New-Item -ItemType Directory -Force -Path $P.Logs | Out-Null
    $out = "$($P.Logs)\$Name.log"; $err = "$($P.Logs)\$Name.err.log"
    foreach ($f in @($out, $err)) { if (Test-Path $f) { Remove-Item -Force $f -ErrorAction SilentlyContinue } }
    $proc = Start-Process -FilePath $Exe -ArgumentList $ArgList -WorkingDirectory $Dir -NoNewWindow -RedirectStandardOutput $out -RedirectStandardError $err -PassThru
    if ($PassThru) { return $proc }
}

# maid の部品（Python 製）を起動する。引数に空白が入りうるものは呼ぶ側で引用符を付ける
function Start-PyPart([string]$Name, [string]$Script, [string[]]$ScriptArgs) {
    if (Test-Port $Ports[$Name]) { return }
    Start-Part $Name $P.MaidPy (@("`"$Script`"") + $ScriptArgs)
}

# 止める前に、まだまとめていない会話を記憶にまとめる（頭脳の中継が動いているときだけ。数十秒〜数分）
function Save-Memory {
    if (-not (Test-Port $Ports.BrainProxy)) { return }
    Say "今日の会話を記憶にまとめています（もう一度 Ctrl+C で中断できます）..." Gray
    try { Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$($Ports.BrainProxy)/maid/consolidate" -TimeoutSec 900 | Out-Null }
    catch { Say "  まとめられませんでした（次に起動したときにまとめます）" Yellow }
}

function Stop-Maid {
    # runtime\ の中のプログラムだけを止める。
    # 数はプログラムの個数（Ollama とその中の llama-server など）なので毎回変わる
    $procs = @(Get-Process | Where-Object { $_.Path -and $_.Path.StartsWith($Rt, [StringComparison]::OrdinalIgnoreCase) })
    foreach ($pr in $procs) { Stop-Process -Id $pr.Id -Force -ErrorAction SilentlyContinue }
    return $procs.Count
}

function Get-OllamaModels {
    return @((Invoke-RestMethod "http://127.0.0.1:$($Ports.Ollama)/api/tags").models | ForEach-Object { $_.name })
}

function Test-ModelIn([string[]]$Names, [string]$Name) {
    return ($Names -contains $Name) -or ($Names -contains "$($Name):latest")
}

# ------------------------------------------------------------------ 部品の起動（待たずに起動だけする。待つのは Wait-Parts）
function Start-OllamaServer {
    if (Test-Port $Ports.Ollama) { return }
    if (-not (Test-Path $P.Ollama)) { throw "Ollama がありません。先に setup.bat を実行してください。" }
    Start-Part "ollama" $P.Ollama @("serve")
}

# 頭脳（Ollama と頭脳の中継）。頭脳の中継は起動するとすぐモデルを GPU に読み込ませる
function Start-Brain {
    Start-OllamaServer
    $a = @("--port", "$($Ports.BrainProxy)", "--ollama", "http://127.0.0.1:$($Ports.Ollama)",
        "--model", "`"$Model`"", "--search", $Search, "--search-filler", "`"$SearchFiller`"", "--embed-model=$EmbedModel")
    if ($UseMemory) { $a += @("--data", "`"$DataDir`"") }
    Start-PyPart "brain-proxy" "$Svc\llm_proxy.py" $a
}

# 声（AivisSpeech と声の中継）
function Start-Voice {
    if (-not (Test-Port $Ports.Aivis)) {
        $exe = Find-AivisExe
        if (-not $exe) { throw "AivisSpeech Engine がありません。先に setup.bat を実行してください。" }
        # 全部の声のモデルは読み込まない（使う声は声の中継が起動時に 1 回しゃべらせて読み込む）
        Start-Part "aivisspeech" $exe @("--port", "$($Ports.Aivis)")
    }
    Start-PyPart "tts-bridge" "$Svc\tts_bridge.py" @("--port", "$($Ports.TtsBridge)", "--aivis-url", "http://127.0.0.1:$($Ports.Aivis)", "--voice", "`"$Voice`"", "--speed", "$Speed")
}

# 起動を待つ。どれかが起動しなければ、見るべきログの名前を出して止める
$PartWaits = @{
    brain = @(@("Ollama", $Ports.Ollama, 60, "ollama"), @("頭脳の中継", $Ports.BrainProxy, 60, "brain-proxy"))
    voice = @(@("AivisSpeech Engine（初回は数分かかります）", $Ports.Aivis, 900, "aivisspeech"), @("声の中継", $Ports.TtsBridge, 60, "tts-bridge"))
}
function Wait-Parts([string[]]$Groups) {
    foreach ($g in $Groups) {
        foreach ($w in $PartWaits[$g]) {
            if (-not (Wait-Port $w[1] $w[2] $w[0])) { throw "$($w[0]) が起動しませんでした（.\maid.bat logs $($w[3])）。" }
        }
    }
}

# 終わるまで待つ（アプリを閉じる・Ctrl+C）。終わったら記憶をまとめて全部止める。
# Ctrl+C はこのウィンドウの部品全部に届いてしまう（頭脳が先に止まって記憶をまとめられない）。
# なので待っている間は Ctrl+C をキー入力として受け取り、記憶をまとめてから自分で部品を止める
function Wait-Quit($App) {
    $ctrlC = $false
    try { [Console]::TreatControlCAsInput = $true; $ctrlC = $true } catch {}
    try {
        while (-not $App.HasExited) {
            if ($ctrlC -and [Console]::KeyAvailable) {
                $k = [Console]::ReadKey($true)
                if ($k.Key -eq [ConsoleKey]::C -and ($k.Modifiers -band [ConsoleModifiers]::Control)) { break }
            }
            Start-Sleep -Milliseconds 300
        }
        if ($App.HasExited) { Say "アプリを終了しました。" Gray }
    } finally {
        if ($ctrlC) { try { [Console]::TreatControlCAsInput = $false } catch {} }   # まとめの途中でもう一度 Ctrl+C なら全部止まる
        Save-Memory
        $n = Stop-Maid
        Say "止めました ($n 個)" Gray
    }
}

# ------------------------------------------------------------------ setup
function Invoke-Setup {
    New-Item -ItemType Directory -Force -Path $Rt, $P.Downloads | Out-Null
    $dl = $P.Downloads

    Say "[1/5] uv（Python とライブラリの管理。Python も runtime\ に入れる）" Cyan
    if (-not (Test-Path $P.Uv)) {
        Get-File "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip" "$dl\uv.zip"
        Expand-Zip "$dl\uv.zip" "$Rt\uv"
    }

    Say "[2/5] Ollama（頭脳を動かす）" Cyan
    if (-not (Test-Path $P.Ollama)) {
        Get-File "https://github.com/ollama/ollama/releases/latest/download/ollama-windows-amd64.zip" "$dl\ollama.zip"
        Expand-Zip "$dl\ollama.zip" "$Rt\ollama"
    }

    Say "[3/5] AivisSpeech Engine（声）" Cyan
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

    Say "[4/5] ライブラリと音声認識のモデル" Cyan
    Install-MaidVenv
    Install-ReazonSpeech
    Install-SileroVad

    Say "[5/5] 頭脳のモデル ($Model$(if ($EmbedModel) { " と $EmbedModel" }))" Cyan
    Start-OllamaServer
    if (-not (Wait-Port $Ports.Ollama 60 "Ollama")) { throw "Ollama が起動しませんでした（.\maid.bat logs ollama）。" }
    foreach ($m in @($Model, $EmbedModel) | Where-Object { $_ }) {
        # 大きいので途中で通信が切れることがある。ollama pull は続きから再開できるので数回やり直す
        for ($i = 1; $i -le 5; $i++) {
            & $P.Ollama pull $m
            if ($LASTEXITCODE -eq 0) { break }
            if ($i -eq 5) { throw "モデル $m の取得に失敗しました。通信を確認して、もう一度 setup.bat を実行してください（続きから再開します）。" }
            Say "  取得が途中で止まりました。やり直します ($i/5)..." Yellow
            Start-Sleep -Seconds 3
        }
    }
    Stop-Maid | Out-Null
    if (Test-Path $P.Old) { Say "`n前に使っていた Open-LLM-VTuber（数 GB）は、もう使いません。.\maid.bat clean で消せます。" Yellow }
    Say "`nセットアップ完了。次は start.bat" Green
}

# ------------------------------------------------------------------ run（すぐ起動する）
function Invoke-Run {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    if (-not (Test-Path $P.Uv)) { throw "先に setup.bat を実行してください。" }
    Install-MaidVenv
    $listen = $false
    if ($Mic) {
        try {
            Install-ReazonSpeech
            Install-SileroVad
            $listen = $true
        } catch { Say "  音声認識を準備できませんでした。今回は文字だけで話します: $($_.Exception.Message)" Yellow }
    }
    Say "部品を起動しています（頭脳$(if ($VoiceReply) { '・声' })）" Cyan
    Start-Brain
    $groups = @("brain")
    if ($VoiceReply) { Start-Voice; $groups += "voice" }
    Wait-Parts $groups
    # 画面の設定やマイクの許可は runtime\webview に保存する
    $a = @("`"$Root\app\maid_app.py`"", "run", "--storage", "`"$Rt\webview`"", "--data", "`"$DataDir`"",
        "--proxy", "http://127.0.0.1:$($Ports.BrainProxy)", "--proactive-minutes", "$ProactiveMinutes")
    if ($VoiceReply) { $a += @("--tts", "http://127.0.0.1:$($Ports.TtsBridge)", "--voice") }
    if ($listen) { $a += @("--asr-dir", "`"$($P.Asr)`"", "--vad", "`"$($P.SileroVad)`"", "--silence-ms", "$SilenceMs", "--mic") }
    $app = Start-Part "app" $P.MaidPy $a -PassThru
    Say ("  頭脳: $Model / 検索: $Search / 声: $(if ($VoiceReply) { $Voice } else { 'なし' }) / マイク: $(if ($listen) { 'あり' } else { 'なし' }) （起動 {0:N0} 秒）" -f $sw.Elapsed.TotalSeconds) Gray
    Say "`n準備完了。ウィンドウを閉じると、記憶をまとめて全部止まります（このウィンドウで Ctrl+C でも）。" Green
    Wait-Quit $app
}

# ------------------------------------------------------------------ start（起動の画面を出してから起動する）
function Invoke-Start {
    if (-not (Test-Path $P.Uv)) { throw "先に setup.bat を実行してください。" }
    Install-MaidVenv
    & $P.MaidPy "$Root\app\maid_app.py" launcher --storage "$Rt\webview" --data "$DataDir" | Out-Host
    # 選んだ内容は maid.settings.json に書かれたので、このスクリプトを読み直して起動する
    if ($LASTEXITCODE -eq 10) { & $PSCommandPath run }
    else { Say "やめました" Gray }
}

# ------------------------------------------------------------------ doctor
# 起動中の部品を、頭脳 → 声 の順に実際に動かしてみて、どこで止まっているかを表示する
function Invoke-Doctor {
    function Res([string]$name, [bool]$good, [string]$detail) {
        $mark = if ($good) { "OK " } else { "NG " }
        Write-Host ("  [{0}] {1,-22} {2}" -f $mark, $name, $detail) -ForegroundColor $(if ($good) { "Green" } else { "Yellow" })
    }
    function Post-Json([string]$url, $obj, [int]$timeout = 120) {
        $body = [Text.Encoding]::UTF8.GetBytes(($obj | ConvertTo-Json -Depth 6 -Compress))
        return Invoke-WebRequest -UseBasicParsing -Method Post -Uri $url -Body $body -ContentType "application/json; charset=utf-8" -TimeoutSec $timeout
    }
    function Down([string]$log) { return "止まっています（.\maid.bat logs $log で確認）" }
    Say "設定: 頭脳 = $Model / 声 = $Voice / 検索 = $Search" Cyan

    Say "頭脳" Cyan
    $up = Test-Port $Ports.Ollama
    Res "Ollama ($($Ports.Ollama))" $up $(if ($up) { "起動中" } else { Down "ollama" })
    if ($up) {
        try {
            $names = Get-OllamaModels
            foreach ($m in @($Model, $EmbedModel) | Where-Object { $_ }) {
                $has = Test-ModelIn $names $m
                Res "モデル" $has $(if ($has) { $m } else { "$m が取得されていません（取得済み: $($names -join ', ')）→ .\setup.bat" })
            }
        } catch { Res "モデル" $false "一覧を取得できません: $($_.Exception.Message)" }
    }
    $up = Test-Port $Ports.BrainProxy
    Res "頭脳の中継 ($($Ports.BrainProxy))" $up $(if ($up) { "起動中" } else { Down "brain-proxy" })
    if ($up) {
        try {
            $sw = [Diagnostics.Stopwatch]::StartNew()
            $r = Post-Json "http://127.0.0.1:$($Ports.BrainProxy)/v1/chat/completions" @{ model = $Model; stream = $false; max_tokens = 40; messages = @(@{ role = "user"; content = "こんにちは。一言で返事して。" }) } 300
            $text = ([Text.Encoding]::UTF8.GetString($r.RawContentStream.ToArray()) | ConvertFrom-Json).choices[0].message.content
            Res "頭脳の返事" ([bool]$text) ("「{0}」 ({1:N1} 秒、初回はモデル読み込みで遅い)" -f $text, $sw.Elapsed.TotalSeconds)
        } catch { Res "頭脳の返事" $false "失敗: $($_.Exception.Message)" }
    }

    Say "声（声で返事するときだけ使う）" Cyan
    $up = Test-Port $Ports.Aivis
    Res "AivisSpeech ($($Ports.Aivis))" $up $(if ($up) { "起動中" } else { Down "aivisspeech" })
    $up = Test-Port $Ports.TtsBridge
    Res "声の中継 ($($Ports.TtsBridge))" $up $(if ($up) { "起動中" } else { Down "tts-bridge" })
    if ($up) {
        try {
            $r = Post-Json "http://127.0.0.1:$($Ports.TtsBridge)/v1/audio/speech" @{ model = "aivisspeech"; voice = "default"; input = "テストです"; response_format = "wav" }
            Res "声の合成" ($r.RawContentLength -gt 1000) "$([math]::Round($r.RawContentLength / 1KB)) KB の音声ができました"
        } catch {
            $msg = $_.Exception.Message
            try { $msg = (New-Object IO.StreamReader($_.Exception.Response.GetResponseStream(), [Text.Encoding]::UTF8)).ReadToEnd() } catch {}
            Res "声の合成" $false "失敗: $msg"
        }
    }

    Say "耳（マイクで聞くときだけ使う）" Cyan
    $has = (Test-Path "$($P.Asr)\tokens.txt") -and (Test-Path $P.SileroVad)
    Res "音声認識のモデル" $has $(if ($has) { $P.Asr } else { "ありません → .\setup.bat" })
    Say "`n聞こえた文や、アプリのエラーは .\maid.bat logs app で見られます。" Gray
}

# ------------------------------------------------------------------ clean / logs
function Invoke-Clean {
    if (Test-Port $Ports.BrainProxy) { throw "動いている間は消せないファイルがあります。先に .\maid.bat stop を実行してください。" }
    if ($Memory) {
        Say "覚えたこと（data\PROFILE.md / NOW.md / 前の会話の要約）を消して、次に起動したときに会話の記録（data\log）から覚え直させます。" Yellow
        if ((Read-Host "よければ yes と入力") -ne "yes") { Say "やめました" Gray; return }
        foreach ($f in @("PROFILE.md", "NOW.md", "USER.md", "MEMORY.md", "memory.db", "state.json")) { Remove-Item -Force "$DataDir\$f" -ErrorAction SilentlyContinue }
        Say "消しました。起動して 1 分ほどすると、裏で覚え直しが始まります（.\maid.bat logs brain-proxy で様子が見られます）。" Green
        return
    }
    $targets = @($P.Logs, $P.Downloads)
    if ($History) {
        Say "会話の画面の履歴（data\chat.json）を消します。覚えたことは消えません。" Yellow
        if ((Read-Host "消してよければ yes と入力") -ne "yes") { Say "やめました" Gray; return }
        $targets += "$DataDir\chat.json"
    }
    $bytes = 0
    foreach ($t in $targets) {
        if (-not (Test-Path $t)) { continue }
        Get-ChildItem $t -Recurse -File -ErrorAction SilentlyContinue | ForEach-Object {
            $len = $_.Length
            try { Remove-Item -Force $_.FullName -ErrorAction Stop; $bytes += $len } catch {}
        }
    }
    Say ("消しました（{0:N1} MB）" -f ($bytes / 1MB)) Green

    # 前に使っていた Open-LLM-VTuber（本体・ライブラリ・画面。数 GB）
    if (Test-Path $P.Old) {
        Say "前に使っていた Open-LLM-VTuber（$($P.Old)）は、もう使いません。" Yellow
        if ((Read-Host "消してよければ yes と入力") -eq "yes") {
            # キャラの設定と音声認識のモデルを、先に引っ越す
            if (Test-Path $P.MaidPy) { & $P.MaidPy "$Root\app\maid_app.py" character | Out-Null }
            try { Install-ReazonSpeech } catch { Say "  音声認識のモデルを引っ越せませんでした（setup.bat でダウンロードし直せます）" Yellow }
            if (-not (Test-Path "$Root\character.yaml")) { Say "キャラの設定を引っ越せなかったので、消すのをやめました。" Yellow; return }
            Remove-Item -Recurse -Force $P.Old
            Say "消しました。" Green
        }
    }

    # 使っていない頭脳のモデル（試したモデルなど。1 つ数 GB）
    if (Test-Path $P.Ollama) {
        Start-OllamaServer
        if (-not (Wait-Port $Ports.Ollama 60 "Ollama")) { throw "Ollama が起動しませんでした（.\maid.bat logs ollama）。" }
        $unused = @(Get-OllamaModels | Where-Object { -not (Test-ModelIn @($_) $Model) -and -not ($EmbedModel -and (Test-ModelIn @($_) $EmbedModel)) })
        if ($unused.Count -gt 0) {
            Say "使っていない頭脳のモデル: $($unused -join ', ')" Yellow
            if ((Read-Host "消してよければ yes と入力") -eq "yes") {
                foreach ($m in $unused) { & $P.Ollama rm $m }
            }
        }
        Stop-Maid | Out-Null
    }
    if (-not $History) { Say "会話の画面の履歴も消すときは .\maid.bat clean -History" Gray }
}

function Invoke-Logs {
    $names = @(Get-ChildItem "$($P.Logs)\*.log" -ErrorAction SilentlyContinue | Where-Object { $_.Name -notlike "*.err.log" } | ForEach-Object { $_.BaseName })
    if (-not $Arg) {
        Say "使い方: maid.bat logs <名前>   （名前: $($names -join ', ')）" Gray
        Say "コピーしたいときはメモ帳で開く: notepad runtime\logs\<名前>.log" Gray
        return
    }
    foreach ($f in @("$($P.Logs)\$Arg.log", "$($P.Logs)\$Arg.err.log")) {
        if (Test-Path $f) {
            # 別ウィンドウで流し続ける（そちらを選択しても部品は止まらない）
            Start-Process powershell -ArgumentList "-NoProfile", "-NoExit", "-Command", "`$host.UI.RawUI.WindowTitle='maid-log $Arg'; Get-Content -Encoding UTF8 -Tail 200 -Wait '$f'"
        }
    }
}

# ------------------------------------------------------------------ コマンド
switch ($Command) {
    "setup" { Invoke-Setup }
    "start" { Invoke-Start }
    "run" { Invoke-Run }
    "stop" { Save-Memory; Say "止めました ($(Stop-Maid) 個)" Green }
    "doctor" { Invoke-Doctor }
    "logs" { Invoke-Logs }
    "clean" { Invoke-Clean }
    default {
        Get-Content $PSCommandPath -Encoding UTF8 | Select-Object -Skip 5 -First 8 | ForEach-Object { $_ -replace "^#\s?", "" }
    }
}
