# maid.exe をこの PC で作る（build.bat から呼ぶ）。
#   .NET SDK はインストーラーを使わず runtime\dotnet に置く。ライブラリなども全部 runtime\ の中に置き、ユーザーフォルダには書かない。
#   できた maid.exe と lib\ は maid フォルダに置く。
#   人に渡す用（動くのに必要なファイルだけ）は dist\maid\ と dist\maid-win-x64.zip に置く。
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$Root = Split-Path -Parent $PSScriptRoot
$Rt = Join-Path $Root "runtime"
$Sdk = Join-Path $Rt "dotnet"
$Dotnet = Join-Path $Sdk "dotnet.exe"

# このプロセスの中だけの環境変数（Windows の設定は変えない）
$env:DOTNET_CLI_HOME = Join-Path $Rt "dotnet-home"
$env:NUGET_PACKAGES = Join-Path $Rt "nuget\packages"
$env:NUGET_HTTP_CACHE_PATH = Join-Path $Rt "nuget\http-cache"
$env:NUGET_PLUGINS_CACHE_PATH = Join-Path $Rt "nuget\plugins-cache"
$env:DOTNET_CLI_TELEMETRY_OPTOUT = "1"
$env:DOTNET_NOLOGO = "1"
$env:DOTNET_SKIP_FIRST_TIME_EXPERIENCE = "1"
$env:DOTNET_ADD_GLOBAL_TOOLS_TO_PATH = "0"
$env:DOTNET_GENERATE_ASPNET_CERTIFICATE = "false"
$env:DOTNET_ROOT = $Sdk
$env:DOTNET_MULTILEVEL_LOOKUP = "0"

if (-not (Test-Path $Dotnet)) {
    Write-Host ".NET SDK 8 を runtime\dotnet に入れます（初回だけ。約 300MB）" -ForegroundColor Cyan
    New-Item -ItemType Directory -Force (Join-Path $Rt "downloads") | Out-Null
    $script = Join-Path $Rt "downloads\dotnet-install.ps1"
    Invoke-WebRequest -UseBasicParsing "https://dot.net/v1/dotnet-install.ps1" -OutFile $script
    & $script -Channel 8.0 -InstallDir $Sdk -NoPath
    Remove-Item -Force $script
}

if (Get-Process maid -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq (Join-Path $Root "maid.exe") }) {
    throw "maid.exe が動いています。閉じてからもう一度 build.bat を実行してください。"
}

Write-Host "maid.exe を作っています..." -ForegroundColor Cyan
$out = Join-Path $Rt "build"
if (Test-Path $out) { Remove-Item -Recurse -Force $out }
# ビルドを速くするための常駐プロセスは使わない（終わったあとに残ってメモリを使い続けるため）
& $Dotnet publish (Join-Path $Root "src\Maid") -c Release -o $out --disable-build-servers "-p:RestoreConfigFile=$(Join-Path $Root 'nuget.config')"
if ($LASTEXITCODE -ne 0) { throw "maid.exe を作れませんでした（上のエラーを見てください）。" }

New-Item -ItemType Directory -Force (Join-Path $Root "lib") | Out-Null
Copy-Item -Force (Join-Path $out "maid.exe") $Root
Get-ChildItem $out -Filter *.dll | Copy-Item -Force -Destination (Join-Path $Root "lib")

# 人に渡す用: maid.exe・lib\・はじめに.txt だけ
$dist = Join-Path $Root "dist"
if (Test-Path $dist) { Remove-Item -Recurse -Force $dist }
$pack = Join-Path $dist "maid"
New-Item -ItemType Directory -Force (Join-Path $pack "lib") | Out-Null
Copy-Item (Join-Path $out "maid.exe") $pack
Get-ChildItem $out -Filter *.dll | Copy-Item -Destination (Join-Path $pack "lib")
Copy-Item (Join-Path $PSScriptRoot "dist-readme.txt") (Join-Path $pack "はじめに.txt")
Compress-Archive -Path $pack -DestinationPath (Join-Path $dist "maid-win-x64.zip")
Remove-Item -Recurse -Force $out
Write-Host "できました: $(Join-Path $Root 'maid.exe')" -ForegroundColor Green
Write-Host "人に渡す用: $(Join-Path $dist 'maid-win-x64.zip')" -ForegroundColor Green
