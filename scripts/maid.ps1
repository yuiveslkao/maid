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
#   maid.bat stop             裏に残っている maid の部品を全部止める

param(
    [Parameter(Position = 0)][string]$Command = "help",
    [Parameter(Position = 1)][string]$Arg = "",
    [string]$Model = "qwen3:8b",     # Ollama のモデル
    [string]$Voice = "default",      # AivisSpeech のスタイル ID か「話者名/スタイル名」
    [double]$Speed = 1.0,
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
$env:OLLAMA_HOST = "127.0.0.1:11434"
$env:PYTHONUNBUFFERED = "1"   # ログがすぐファイルに出るように
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

# 裏で動く部品はウィンドウを出さず、ログを runtime\logs\ に書く。
# このウィンドウにぶら下がるので、このウィンドウを閉じる / Ctrl+C で一緒に止まる。
function Start-Background([string]$Name, [string]$Exe, [string[]]$ArgList) {
    $logs = "$Rt\logs"
    New-Item -ItemType Directory -Force -Path $logs | Out-Null
    Start-Process -FilePath $Exe -ArgumentList $ArgList -NoNewWindow `
        -RedirectStandardOutput "$logs\$Name.log" -RedirectStandardError "$logs\$Name.err.log" | Out-Null
}

function Start-Ollama {
    if (Test-Port 11434) { return }
    if (-not (Test-Path $P.Ollama)) { throw "Ollama がありません。先に maid.bat setup を実行してください。" }
    Start-Background "ollama" $P.Ollama @("serve")
    if (-not (Wait-Port 11434 60 "Ollama")) { throw "Ollama が起動しませんでした。" }
}

function Start-Aivis {
    if (Test-Port 10101) { return }
    $exe = Find-AivisExe
    if (-not $exe) { throw "AivisSpeech Engine がありません。先に maid.bat setup を実行してください。" }
    $a = @("--load_all_models")
    if ($AivisGpu) { $a += "--use_gpu" }
    Start-Background "aivisspeech" $exe $a
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
    Start-Ollama
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
        $wasRunning = Test-Port 11434
        if ($wasRunning) {
            try { $models = @((Invoke-RestMethod "http://127.0.0.1:11434/api/tags").models | ForEach-Object { $_.name }) } catch { $models = @() }
        }
    }
    if ($models.Count -gt 0) {
        Row "頭脳のモデル" ($models -contains $Model) ($models -join ", ") ".\maid.bat setup -Model $Model"
    } else {
        Say "  [-- ] 頭脳のモデル                 （Ollama 停止中のため未確認。start 後にもう一度 check）"
    }

    Say "起動状態（start 後に確認）" Cyan
    foreach ($s in @(@("Ollama", 11434), @("AivisSpeech Engine", 10101), @("ブリッジ", 10102), @("Discord ゲート", 12394), @("Open-LLM-VTuber", 12393))) {
        $up = Test-Port $s[1]
        Write-Host ("  [{0}] {1,-28} 127.0.0.1:{2}" -f $(if ($up) { "ON " } else { "-- " }), $s[0], $s[1]) -ForegroundColor $(if ($up) { "Green" } else { "DarkGray" })
    }
    if ($script:ok) { Say "`n準備 OK。.\start.bat で起動できます" Green } else { Say "`nNG の項目を直してください" Yellow }
}

# ------------------------------------------------------------------ start
function Invoke-Start {
    Say "Ollama" Cyan; Start-Ollama
    Say "AivisSpeech Engine" Cyan; Start-Aivis

    Say "ブリッジ (10102)" Cyan
    if (-not (Test-Port 10102)) {
        $a = @("run", "--no-project", "--python", $PyVer, "python", "`"$Root\bridge\aivis_openai_bridge.py`"", "--voice", "`"$Voice`"", "--speed", "$Speed")
        Start-Background "bridge" $P.Uv $a
    }

    if (-not $NoDiscordGate) {
        Say "Discord ゲート (12394)" Cyan
        if (-not (Test-Port 12394)) {
            $a = @("run", "--no-project", "--python", $PyVer, "--with", "websockets>=13", "python", "`"$Root\gate\discord_gate.py`"")
            Start-Background "discord-gate" $P.Uv $a
        }
    }

    if (-not (Test-Path "$($P.Olv)\run_server.py")) { throw "Open-LLM-VTuber がありません。先に maid.bat setup を実行してください。" }
    if (-not $NoBrowser) {
        # 準備ができたら、Windows 標準の Edge をアプリ風のウィンドウで開く（インストール不要）
        $url = "http://127.0.0.1:12393"
        Start-Job -ScriptBlock {
            param($url)
            for ($i = 0; $i -lt 600; $i++) {
                try { $c = New-Object Net.Sockets.TcpClient; $c.Connect("127.0.0.1", 12393); $c.Close(); break } catch { Start-Sleep 1 }
            }
            Start-Process "msedge.exe" "--app=$url"
        } -ArgumentList $url | Out-Null
    }
    Say "裏の部品のログ: $Rt\logs\" DarkGray
    Say "Open-LLM-VTuber を起動します（このウィンドウを閉じるか Ctrl+C で、全部まとめて止まります）" Cyan
    Push-Location $P.Olv
    try { & $P.Uv run run_server.py } finally { Pop-Location }
}

# ------------------------------------------------------------------ others
switch ($Command) {
    "setup" { Invoke-Setup }
    "check" { Invoke-Check }
    "start" { Invoke-Start }
    "bench" {
        Start-Ollama
        $models = if ($Arg) { $Arg -split "," } else { @($Model) }
        $a = @("$Root\scripts\bench_latency.py")
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
        # runtime\ の中のプログラムだけを止める（前回の残りなど）
        $procs = @(Get-Process | Where-Object { $_.Path -and $_.Path.StartsWith($Rt, [StringComparison]::OrdinalIgnoreCase) })
        foreach ($pr in $procs) { Say "  停止: $($pr.ProcessName) ($($pr.Id))"; Stop-Process -Id $pr.Id -Force -ErrorAction SilentlyContinue }
        Say "止めました ($($procs.Count) 個)" Green
    }
    "discord-check" { Invoke-UvPython @("$Root\gate\discord_gate.py", "--check") @("websockets>=13") }
    default {
        Get-Content $PSCommandPath -Encoding UTF8 | Select-Object -First 13 | ForEach-Object { $_ -replace "^#\s?", "" }
    }
}
