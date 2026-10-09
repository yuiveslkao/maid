# maid 一括起動スクリプト（Windows / PowerShell）
#   AivisSpeech Engine → ブリッジ → Discord ゲート → Open-LLM-VTuber の順に起動する。Ollama は常駐アプリとして起動済みの前提。
#
#   使い方:  powershell -ExecutionPolicy Bypass -File scripts\start.ps1
#   オプション例:  -OlvDir D:\Open-LLM-VTuber -Voice "まい/ノーマル" -Speed 1.1 -AivisGpu

param(
    [string]$OlvDir = "$HOME\Open-LLM-VTuber",   # Open-LLM-VTuber を clone した場所
    [string]$AivisExe = "",                      # 空なら標準のインストール先を探す
    [string]$Voice = "default",                  # AivisSpeech のスタイル ID か「話者名/スタイル名」
    [double]$Speed = 1.0,
    [switch]$AivisGpu,                           # AivisSpeech を GPU (DirectML) で動かす。VRAM を食うので LLM と相談
    [switch]$NoDiscordGate                       # Discord 通話中に音声を止めるゲートを使わない
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot

function Test-Port([int]$Port) {
    try { $c = New-Object Net.Sockets.TcpClient; $c.Connect("127.0.0.1", $Port); $c.Close(); return $true } catch { return $false }
}

# 1. Ollama
if (-not (Test-Port 11434)) {
    Write-Host "Ollama が起動していないようです。スタートメニューから Ollama を起動してください。" -ForegroundColor Yellow
}

# 2. AivisSpeech Engine
if (Test-Port 10101) {
    Write-Host "AivisSpeech Engine は起動済み (10101)"
} else {
    if (-not $AivisExe) {
        $candidates = @(
            "$env:ProgramFiles\AivisSpeech\AivisSpeech-Engine\run.exe",
            "$env:LOCALAPPDATA\Programs\AivisSpeech\AivisSpeech-Engine\run.exe"
        )
        $AivisExe = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    }
    if (-not $AivisExe) { throw "AivisSpeech Engine (run.exe) が見つかりません。-AivisExe でパスを指定してください。" }
    $aivisArgs = @("--load_all_models")
    if ($AivisGpu) { $aivisArgs += "--use_gpu" }
    Write-Host "AivisSpeech Engine を起動: $AivisExe $aivisArgs"
    Start-Process -FilePath $AivisExe -ArgumentList $aivisArgs -WindowStyle Minimized
}

# 3. ブリッジ（標準ライブラリのみなので Python があれば動く）
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command py -ErrorAction SilentlyContinue }
if (Test-Port 10102) {
    Write-Host "ブリッジは起動済み (10102)"
} else {
    $bridgeArgs = @("`"$Root\bridge\aivis_openai_bridge.py`"", "--voice", "`"$Voice`"", "--speed", "$Speed")
    if ($python) {
        Start-Process -FilePath $python.Source -ArgumentList $bridgeArgs -WindowStyle Minimized
    } else {
        # Python が PATH に無い場合は uv で動かす（Open-LLM-VTuber の導入で uv は入っている）
        Start-Process -FilePath "uv" -ArgumentList (@("run", "--no-project", "python") + $bridgeArgs) -WindowStyle Minimized
    }
    Write-Host "ブリッジを起動: http://127.0.0.1:10102/v1"
}

# 4. Discord ゲート（画面の WebSocket URL を ws://127.0.0.1:12394/client-ws にしておく）
if (-not $NoDiscordGate) {
    if (Test-Port 12394) {
        Write-Host "Discord ゲートは起動済み (12394)"
    } else {
        $gateArgs = @("run", "--no-project", "--with", "websockets>=13", "python", "`"$Root\gate\discord_gate.py`"")
        Start-Process -FilePath "uv" -ArgumentList $gateArgs -WindowStyle Minimized
        Write-Host "Discord ゲートを起動: ws://127.0.0.1:12394/client-ws"
    }
}

# 5. Open-LLM-VTuber（このウィンドウで動かす。止めるときは Ctrl+C）
if (-not (Test-Path "$OlvDir\run_server.py")) { throw "Open-LLM-VTuber が $OlvDir に見つかりません。-OlvDir で指定してください。" }
Write-Host "Open-LLM-VTuber を起動します。準備ができたらデスクトップアプリか http://localhost:12393 を開いてください。"
Push-Location $OlvDir
try { uv run run_server.py } finally { Pop-Location }
