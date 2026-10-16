# maid

PC 上でいつでも音声で話せる、好みの声と性格のキャラクター兼調べものエージェント（個人用）。
基盤は [Open-LLM-VTuber](https://github.com/Open-LLM-VTuber/Open-LLM-VTuber) v1.2.1 で、本体は改造せず外付けで拡張します。

- 仕様と今後の計画: [docs/SPEC.md](docs/SPEC.md)
- セットアップと使い方: [docs/SETUP_WINDOWS.md](docs/SETUP_WINDOWS.md)

PC を汚さない構成です。インストーラーや PATH の変更は使わず、必要なものは全部 `runtime\` に入ります。

```bat
setup.bat          rem 必要なものを runtime\ にダウンロード
start.bat          rem 起動
```

## 仕組み

```
マイク ─▶ 画面（Edge）─▶ Discord ゲート ─▶ Open-LLM-VTuber ─┬─▶ 音声認識（ReazonSpeech, CPU）
                                                            ├─▶ 頭脳の中継 ─┬▶ Ollama（GPU）
                                                            │               └▶ Web 検索（検索語とページ取得だけ外へ）
                                                            └─▶ 声の中継 ─▶ AivisSpeech ─▶ ヘッドホン
```

Open-LLM-VTuber 本体は改造せず、前後に中継を挟んだり、設定で変えられない部分だけを起動時に直したりしています。

## 中身

| パス | 内容 |
|---|---|
| `maid.bat` / `setup.bat` / `start.bat` | 入口。中身は `scripts/maid.ps1` |
| `scripts/maid.ps1` | ダウンロード・確認・起動・停止・診断など（`maid.bat` で一覧） |
| `scripts/olv_patch.py` | Open-LLM-VTuber に当てる修正（画面のマイク設定、conf.yaml を maid の形にそろえる、音声認識モデルの展開） |
| `services/llm_proxy.py` | 頭脳の中継。考えるモードを切る、モデルのすり替え、繰り返しの打ち切り、今の日時と検索結果を渡す |
| `services/memory.py` | 記憶。会話ログ・前の会話の要約・覚えていること（`data/` に保存） |
| `services/web_search.py` | Web 検索（DuckDuckGo）とページ本文（PDF も）の抜き出し。外に出るのは検索語と開いたページだけ |
| `services/tts_bridge.py` | 声の中継。AivisSpeech を OpenAI 互換の TTS として見せる |
| `services/discord_gate.py` | Discord ゲート。Discord で通話中は声を本体に渡さない |
| `frontend/maid-vrm.js` | 3D モデル（VRM）を画面に表示する |
| `config/maid.settings.default.json` | `maid.settings.json`（いつも使う設定）の既定値 |
| `config/open-llm-vtuber/conf.yaml` | Open-LLM-VTuber の設定の雛形（maid で使う項目だけ。setup で `runtime/` にコピーされる） |
| `tests/` | テスト（`runtime\uv\uv.exe run --no-project --python 3.10 --with "websockets>=13" --with openai --with ruamel.yaml --with pyyaml --with pypdf python -m unittest discover -s tests`） |
| `runtime/` | ダウンロードしたもの一式（git の管理外。消せば元通り） |
| `data/` | 記憶（git の管理外。消すと全部忘れる） |
| `models/` | 自分で用意した 3D モデルなど（git の管理外） |
