# maid

PC 上でいつでも音声で話せる、好みの声と性格のキャラクター兼調べものエージェント（個人用）。
基盤は [Open-LLM-VTuber](https://github.com/Open-LLM-VTuber/Open-LLM-VTuber) v1.2.1 で、本体は改造せず外付けで拡張します。

- 仕様と今後の計画: [docs/SPEC.md](docs/SPEC.md)
- セットアップと使い方: [docs/SETUP_WINDOWS.md](docs/SETUP_WINDOWS.md)

PC を汚さない構成です。インストーラーや PATH の変更は使わず、必要なものは全部 `runtime\` に入ります。

```bat
setup.bat          rem 必要なものを runtime\ にダウンロード
maid.bat check     rem そろっているか確認
start.bat          rem 起動
```

## 仕組み

```
マイク ─▶ 画面（Edge）─▶ Discord ゲート ─▶ Open-LLM-VTuber ─┬─▶ 音声認識（ReazonSpeech, CPU）
                                                            ├─▶ 頭脳の中継 ─▶ Ollama（GPU）
                                                            └─▶ 声の中継 ─▶ AivisSpeech ─▶ ヘッドホン
```

Open-LLM-VTuber 本体は改造せず、前後に中継を挟んだり、設定で変えられない部分だけを起動時に直したりしています。

## 中身

| パス | 内容 |
|---|---|
| `maid.bat` / `setup.bat` / `start.bat` | 入口。中身は `scripts/maid.ps1` |
| `scripts/maid.ps1` | ダウンロード・確認・起動・停止・診断など（`maid.bat` で一覧） |
| `scripts/olv_patch.py` | Open-LLM-VTuber に当てる修正（画面のマイク設定、音声認識の設定、モデルの展開） |
| `scripts/bench_latency.py` | 頭脳と声の応答速度を測る |
| `services/llm_proxy.py` | 頭脳の中継。考えるモードを切る、モデルのすり替え、空の返事の聞き直し、繰り返しの打ち切り |
| `services/tts_bridge.py` | 声の中継。AivisSpeech を OpenAI 互換の TTS として見せる |
| `services/discord_gate.py` | Discord ゲート。Discord で通話中は声を本体に渡さない |
| `config/maid.settings.default.json` | `maid.settings.json`（いつも使う設定）の既定値 |
| `config/open-llm-vtuber/conf.yaml` | Open-LLM-VTuber の設定の雛形（setup で `runtime/` にコピーされる） |
| `tests/` | テスト（`runtime\uv\uv.exe run --no-project --with "websockets>=13" --with openai --with ruamel.yaml --with pyyaml python -m unittest discover -s tests`） |
| `runtime/` | ダウンロードしたもの一式（git の管理外。消せば元通り） |
