# maid 管理スクリプト（Windows PowerShell 5.1 以降）
#
#   必要なものは全部 maid\runtime\ に入れる。インストーラー・PATH の変更・winget は使わない。
#   片付けるときは maid フォルダを消せばよい（例外は docs\SETUP_WINDOWS.md の「片付け方」を参照）。
#
#   maid.bat setup            必要なものを runtime\ にダウンロードする（何度実行してもよい）
#   maid.bat start            起動する（start.bat と同じ）
#   maid.bat stop             裏に残っている maid の部品を全部止める
#   maid.bat doctor           起動中に、返事が来ない・声が出ないなどの原因を調べる
#   maid.bat logs <名前>      裏の部品のログを別ウィンドウで見る（ollama / brain-proxy / tts-bridge など）
#   maid.bat clean            ログ・一時ファイル・使っていないモデルを消す（-History で会話の履歴も）

param(
    [Parameter(Position = 0)][string]$Command = "help",
    [Parameter(Position = 1)][string]$Arg = "",
    [switch]$History                 # clean で会話の履歴も消す
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # PowerShell 5.1 のダウンロードが遅くなるのを防ぐ
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

# ------------------------------------------------------------------ 場所・ポート
$Root = Split-Path -Parent $PSScriptRoot
$Rt = Join-Path $Root "runtime"
$Svc = Join-Path $Root "services"
$P = @{
    Uv          = "$Rt\uv\uv.exe"
    SevenZip    = "$Rt\tools\7zr.exe"
    Ollama      = "$Rt\ollama\ollama.exe"
    OllamaModel = "$Rt\ollama-models"
    AivisDir    = "$Rt\aivisspeech-engine"
    Olv         = "$Rt\Open-LLM-VTuber"
    Downloads   = "$Rt\downloads"
    Logs        = "$Rt\logs"
}
# 本物の Ollama は Ollama に、いつもの 11434 には頭脳の中継 (services\llm_proxy.py) を置く。
# Open-LLM-VTuber の conf.yaml は 11434 を見たままでよい
$Ports = @{
    Ollama     = 11436
    BrainProxy = 11434
    Aivis      = 10101
    TtsBridge  = 10102
    Olv        = 12393
    Gate       = 12394
}
$OlvUrl = "http://127.0.0.1:$($Ports.Olv)"
$OlvTag = "v1.2.1"
$OlvFrontendCommit = "06a659b114fff788cf0daaa86e484576db4975bf"   # v1.2.1 が参照しているビルド済み画面
$PyVer = "3.10"   # maid の部品も Open-LLM-VTuber と同じ Python を使う（Python を 2 つ入れないため）
$ReazonName = "sherpa-onnx-zipformer-ja-reazonspeech-2024-08-01"   # 音声認識のモデル（日本語専用）
$SearchPackages = @("ddgs", "trafilatura")                          # 検索と本文の抜き出し

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
$SilenceMs = [int](Get-Setting "silence_ms")           # 無音がこの長さ続いたら AI に送る（ミリ秒）
$Search = [string](Get-Setting "search")               # auto / explicit / off
$SearchFiller = [string]$UserSettings.search_filler    # 調べる前にすぐ言う一言（空なら言わない）
if ($null -eq $UserSettings.search_filler) { $SearchFiller = [string]$Defaults.search_filler }

# ------------------------------------------------------------------ このプロセスの中だけの環境変数（Windows の設定は変えない）
$env:UV_CACHE_DIR = "$Rt\cache\uv"
$env:UV_PYTHON_INSTALL_DIR = "$Rt\python"
$env:UV_PYTHON_BIN_DIR = "$Rt\python\bin"
$env:UV_PYTHON_INSTALL_REGISTRY = "0"       # Python をレジストリに登録しない
$env:UV_PYTHON_PREFERENCE = "only-managed"  # PC に入っている Python は使わない
$env:HF_HOME = "$Rt\cache\huggingface"      # ライブラリが勝手にダウンロードしても runtime\ に入るように
$env:TORCH_HOME = "$Rt\cache\torch"
$env:OLLAMA_MODELS = $P.OllamaModel
$env:OLLAMA_HOST = "127.0.0.1:$($Ports.Ollama)"
$env:OLLAMA_KEEP_ALIVE = "-1"   # モデルを GPU に載せっぱなしにする（外れると読み込み直しに 1 分以上かかる）
$env:PYTHONUNBUFFERED = "1"

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

# Open-LLM-VTuber への修正（scripts\olv_patch.py）。conf.yaml を扱うので Open-LLM-VTuber の環境で動かす
function Invoke-OlvPatch([string[]]$PatchArgs) {
    # 出力は画面に出すだけにする（関数の戻り値に混ざらないように）
    & $P.Uv run --project $P.Olv python "$Root\scripts\olv_patch.py" @($PatchArgs + @("--olv", $P.Olv)) | Out-Host
    return ($LASTEXITCODE -eq 0)
}

# 裏で動く部品はウィンドウを出さず、出力を runtime\logs\<名前>.log に書く。
# （コンソールに出すと、ウィンドウをクリックして「選択」状態になったとき部品ごと止まってしまうため）
# start.bat のウィンドウにぶら下がるので、そこで Ctrl+C すると一緒に止まる
function Start-Part([string]$Name, [string]$Exe, [string[]]$ArgList) {
    New-Item -ItemType Directory -Force -Path $P.Logs | Out-Null
    $out = "$($P.Logs)\$Name.log"; $err = "$($P.Logs)\$Name.err.log"
    foreach ($f in @($out, $err)) { if (Test-Path $f) { Remove-Item -Force $f -ErrorAction SilentlyContinue } }
    Start-Process -FilePath $Exe -ArgumentList $ArgList -NoNewWindow -RedirectStandardOutput $out -RedirectStandardError $err | Out-Null
}

# maid の部品（Python 製）を runtime\ の Python で起動する。引数に空白が入りうるものは呼ぶ側で引用符を付ける
function Start-PyPart([string]$Name, [string]$Script, [string[]]$ScriptArgs, [string[]]$With = @()) {
    $a = @("run", "--no-project", "--python", $PyVer)
    foreach ($w in $With) { $a += @("--with", "`"$w`"") }   # websockets>=13 の > を引用符で守る
    Start-Part $Name $P.Uv ($a + @("python", "`"$Script`"") + $ScriptArgs)
}

function Stop-Maid {
    # runtime\ の中のプログラムだけを止める。
    # 数はプログラムの個数（uv とその中の python、Ollama とその中の llama-server など）なので毎回変わる
    $procs = @(Get-Process | Where-Object { $_.Path -and $_.Path.StartsWith($Rt, [StringComparison]::OrdinalIgnoreCase) })
    foreach ($pr in $procs) { Stop-Process -Id $pr.Id -Force -ErrorAction SilentlyContinue }
    return $procs.Count
}

function Get-OllamaModels {
    return @((Invoke-RestMethod "http://127.0.0.1:$($Ports.Ollama)/api/tags").models | ForEach-Object { $_.name })
}

function Test-ModelIn([string[]]$Names) {
    return ($Names -contains $Model) -or ($Names -contains "$($Model):latest")
}

# ------------------------------------------------------------------ 部品の起動
function Start-OllamaServer {
    if (Test-Port $Ports.Ollama) { return }
    if (-not (Test-Path $P.Ollama)) { throw "Ollama がありません。先に setup.bat を実行してください。" }
    Start-Part "ollama" $P.Ollama @("serve")
    if (-not (Wait-Port $Ports.Ollama 60 "Ollama")) { throw "Ollama が起動しませんでした（.\maid.bat logs ollama）。" }
}

function Start-Brain {
    Start-OllamaServer
    if (Test-Port $Ports.BrainProxy) { return }
    Start-PyPart "brain-proxy" "$Svc\llm_proxy.py" @("--port", "$($Ports.BrainProxy)", "--ollama", "http://127.0.0.1:$($Ports.Ollama)",
        "--model", "`"$Model`"", "--search", $Search, "--search-filler", "`"$SearchFiller`"") $SearchPackages
    if (-not (Wait-Port $Ports.BrainProxy 120 "頭脳の中継")) { throw "頭脳の中継が起動しませんでした（.\maid.bat logs brain-proxy）。" }
}

function Start-Aivis {
    if (Test-Port $Ports.Aivis) { return }
    $exe = Find-AivisExe
    if (-not $exe) { throw "AivisSpeech Engine がありません。先に setup.bat を実行してください。" }
    Start-Part "aivisspeech" $exe @("--load_all_models", "--port", "$($Ports.Aivis)")
    # 初回はモデル（約 1GB）をダウンロードするので長めに待つ
    if (-not (Wait-Port $Ports.Aivis 900 "AivisSpeech Engine（初回は数分かかります）")) { throw "AivisSpeech Engine が起動しませんでした（.\maid.bat logs aivisspeech）。" }
}

function Start-TtsBridge {
    if (Test-Port $Ports.TtsBridge) { return }
    Start-PyPart "tts-bridge" "$Svc\tts_bridge.py" @("--port", "$($Ports.TtsBridge)", "--aivis-url", "http://127.0.0.1:$($Ports.Aivis)", "--voice", "`"$Voice`"", "--speed", "$Speed")
}

function Start-Gate {
    if (Test-Port $Ports.Gate) { return }
    Start-PyPart "discord-gate" "$Svc\discord_gate.py" @("--port", "$($Ports.Gate)", "--backend", "ws://127.0.0.1:$($Ports.Olv)") @("websockets>=13")
}

# 日本語の音声認識モデル (ReazonSpeech, sherpa-onnx 版) を取ってくる（初回だけ）
function Install-ReazonSpeech {
    if (Test-Path "$($P.Olv)\models\$ReazonName\encoder-epoch-99-avg-1.int8.onnx") { return }
    Say "  日本語の音声認識モデル (ReazonSpeech) をダウンロードします（約 700MB、初回だけ）" Cyan
    $archive = "$($P.Downloads)\$ReazonName.tar.bz2"
    Get-File "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/$ReazonName.tar.bz2" $archive
    if (-not (Invoke-OlvPatch @("extract-asr", $archive))) { throw "音声認識モデルの展開に失敗しました。" }
    Remove-Item -Force $archive -ErrorAction SilentlyContinue   # 使うのは展開したものだけ
}

function Set-Frontend([string]$WsUrl) {
    if (-not (Invoke-OlvPatch @("frontend", "--ws-url", $WsUrl, "--base-url", $OlvUrl, "--silence-ms", "$SilenceMs"))) {
        Say "  画面の初期設定を書けませんでした（マイク設定は画面から手で変えてください）" Yellow
    }
}

# ゲート経由で本体につながり、最初のメッセージが返ってくるか確かめる
function Test-Gate {
    try {
        $ws = New-Object System.Net.WebSockets.ClientWebSocket
        $cts = New-Object System.Threading.CancellationTokenSource 10000
        $ws.ConnectAsync([Uri]"ws://127.0.0.1:$($Ports.Gate)/client-ws", $cts.Token).Wait()
        $buf = New-Object byte[] 65536
        $r = $ws.ReceiveAsync([ArraySegment[byte]]::new($buf), $cts.Token).Result
        try { $ws.CloseAsync([System.Net.WebSockets.WebSocketCloseStatus]::NormalClosure, "", [Threading.CancellationToken]::None).Wait(2000) | Out-Null } catch {}
        return ($r.Count -gt 0)
    } catch { return $false }
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
    }

    Say "[5/6] ライブラリ（数 GB。初回は時間がかかります）" Cyan
    Push-Location $P.Olv
    try { & $P.Uv sync; if ($LASTEXITCODE -ne 0) { throw "uv sync に失敗しました。" } } finally { Pop-Location }
    $with = @(); foreach ($w in @($SearchPackages + "websockets>=13")) { $with += @("--with", $w) }
    & $P.Uv run --no-project --python $PyVer @with python -c "print('maid の部品用 Python OK')"
    if ($LASTEXITCODE -ne 0) { throw "maid の部品用のライブラリを入れられませんでした。" }

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

    Say "`nセットアップ完了。次は start.bat" Green
}

# ------------------------------------------------------------------ start
function Invoke-Start {
    if (-not (Test-Path "$($P.Olv)\run_server.py")) { throw "Open-LLM-VTuber がありません。先に setup.bat を実行してください。" }
    Say "頭脳（Ollama + 中継）" Cyan; Start-Brain
    Say "声（AivisSpeech + 中継）" Cyan; Start-Aivis; Start-TtsBridge
    Say "Discord ゲート" Cyan; Start-Gate

    Say "Open-LLM-VTuber の準備" Cyan
    Set-Frontend "ws://127.0.0.1:$($Ports.Olv)/client-ws"
    Install-ReazonSpeech
    if (-not (Invoke-OlvPatch @("conf"))) { Say "  conf.yaml を確認できませんでした（今の設定のまま起動します）" Yellow }

    Say "Open-LLM-VTuber" Cyan
    if (-not (Test-Port $Ports.Olv)) {
        Start-Part "open-llm-vtuber" $P.Uv @("run", "--project", "`"$($P.Olv)`"", "--directory", "`"$($P.Olv)`"", "run_server.py")
    }
    if (-not (Wait-Port $Ports.Olv 1200 "Open-LLM-VTuber（初回は数分かかります）")) {
        throw "Open-LLM-VTuber が起動しませんでした（.\maid.bat logs open-llm-vtuber）。"
    }

    # 画面がつなぐ先: Discord ゲートが本体まで通じていればゲート、だめなら本体に直結のまま
    if ((Wait-Port $Ports.Gate 60) -and (Test-Gate)) {
        Set-Frontend "ws://127.0.0.1:$($Ports.Gate)/client-ws"
        Say "  Discord ゲート: 使用中（Discord で通話中は声に反応しません）" Green
    } else {
        Say "  Discord ゲート: うまく動かないので使わずに直結します（.\maid.bat logs discord-gate）" Yellow
    }

    Say "  頭脳: $Model / 声: $Voice / 検索: $Search" Gray
    # Windows 標準の Edge をアプリ風のウィンドウで開く（インストール不要）。?t= は古いページを使い回させないため
    Start-Process "msedge.exe" "--app=$OlvUrl/?t=$([DateTimeOffset]::Now.ToUnixTimeSeconds())"
    Say "`n準備完了。画面を閉じても裏の部品は動き続けます。ログ: .\maid.bat logs <名前>" Green
    Say "全部止めるときは、このウィンドウで Ctrl+C（または .\maid.bat stop）。" Green
    try {
        while (Test-Port $Ports.Olv) { Start-Sleep -Seconds 2 }
        Say "Open-LLM-VTuber が終了しました。" Yellow
    } finally {
        $n = Stop-Maid
        Say "残っていたプログラムを止めました ($n 個)" Gray
    }
}

# ------------------------------------------------------------------ doctor
# 起動中の部品を、頭脳 → 声 → 本体 の順に実際に動かしてみて、どこで止まっているかを表示する
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
            $has = Test-ModelIn $names
            Res "モデル" $has $(if ($has) { $Model } else { "$Model が取得されていません（取得済み: $($names -join ', ')）→ .\setup.bat" })
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

    Say "声" Cyan
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

    Say "本体と画面" Cyan
    $up = Test-Port $Ports.Olv
    Res "Open-LLM-VTuber ($($Ports.Olv))" $up $(if ($up) { "起動中" } else { Down "open-llm-vtuber" })
    if ($up) {
        try { Invoke-WebRequest -UseBasicParsing "$OlvUrl/maid-defaults.js" | Out-Null; Res "画面の初期設定" $true "配信されています" }
        catch { Res "画面の初期設定" $false "maid-defaults.js が配信されていません" }
    }
    $g = (Test-Port $Ports.Gate) -and (Test-Gate)
    Res "Discord ゲート ($($Ports.Gate))" $g $(if ($g) { "本体まで通じています" } else { "使えません（画面は本体に直結されます）" })
    $log = Get-ChildItem "$($P.Olv)\logs\debug_*.log" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime | Select-Object -Last 1
    if ($log) { Say "`n本体のログ: $($log.FullName)" Gray }
}

# ------------------------------------------------------------------ clean / logs
function Invoke-Clean {
    if (Test-Port $Ports.Olv) { throw "動いている間は消せないファイルがあります。先に .\maid.bat stop を実行してください。" }
    # ログ・一時ファイル・ダウンロード済みのファイル・前に使っていた音声認識モデル (SenseVoice)
    $oldAsr = @(Get-ChildItem "$($P.Olv)\models\sherpa-onnx-sense-voice-*" -Directory -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName })
    $targets = @($P.Logs, $P.Downloads, "$($P.Olv)\logs", "$($P.Olv)\cache") + $oldAsr
    if ($History) {
        Say "会話の履歴（$($P.Olv)\chat_history）も全部消します。元に戻せません。" Yellow
        if ((Read-Host "消してよければ yes と入力") -ne "yes") { Say "やめました" Gray; return }
        $targets += "$($P.Olv)\chat_history"
    }
    $bytes = 0
    foreach ($t in $targets) {
        if (-not (Test-Path $t)) { continue }
        Get-ChildItem $t -Recurse -File -ErrorAction SilentlyContinue | ForEach-Object {
            $len = $_.Length
            try { Remove-Item -Force $_.FullName -ErrorAction Stop; $bytes += $len } catch {}
        }
    }
    foreach ($d in $oldAsr) { Remove-Item -Recurse -Force $d -ErrorAction SilentlyContinue }
    Remove-Item -Force "$($P.Olv)\conf.yaml.backup" -ErrorAction SilentlyContinue   # Open-LLM-VTuber が起動のたびに作る控え
    Say ("消しました（{0:N1} MB）" -f ($bytes / 1MB)) Green

    # 使っていない頭脳のモデル（試したモデルなど。1 つ数 GB）
    if (Test-Path $P.Ollama) {
        Start-OllamaServer
        $unused = @(Get-OllamaModels | Where-Object { $_ -ne $Model -and $_ -ne "$($Model):latest" })
        if ($unused.Count -gt 0) {
            Say "使っていない頭脳のモデル: $($unused -join ', ')" Yellow
            if ((Read-Host "消してよければ yes と入力") -eq "yes") {
                foreach ($m in $unused) { & $P.Ollama rm $m }
            }
        }
        Stop-Maid | Out-Null
    }
    if (-not $History) { Say "会話の履歴も消すときは .\maid.bat clean -History" Gray }
}

function Invoke-Logs {
    $names = @(Get-ChildItem "$($P.Logs)\*.log" -ErrorAction SilentlyContinue | Where-Object { $_.Name -notlike "*.err.log" } | ForEach-Object { $_.BaseName })
    if (-not $Arg) {
        Say "使い方: maid.bat logs <名前>   （名前: $($names -join ', ')）" Gray
        Say "本体の詳しいログ: $($P.Olv)\logs\" Gray
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
    "stop" { Say "止めました ($(Stop-Maid) 個)" Green }
    "doctor" { Invoke-Doctor }
    "logs" { Invoke-Logs }
    "clean" { Invoke-Clean }
    default {
        Get-Content $PSCommandPath -Encoding UTF8 | Select-Object -Skip 5 -First 6 | ForEach-Object { $_ -replace "^#\s?", "" }
    }
}
