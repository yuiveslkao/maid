# maid

PC 上でいつでも文字と声で話せる、好みの声と性格のキャラクター兼調べものエージェント（個人用）。
Windows のアプリとして動き、頭脳（LLM）・声・音声認識はすべて PC の中で動かします。

- 仕様と今後の計画: [docs/SPEC.md](docs/SPEC.md)
- セットアップと使い方: [docs/SETUP_WINDOWS.md](docs/SETUP_WINDOWS.md)

PC を汚さない構成です。インストーラーや PATH の変更は使わず、必要なものは全部 `runtime\` に入ります。

```bat
setup.bat          rem 必要なものを runtime\ にダウンロード
start.bat          rem 起動の画面（使うもの・アイコンを選ぶ）→ 起動
```

## 仕組み

```
キーボード・貼った画像・画面のスクショ ─┐
マイク ─▶ 話し終わりの判定 ─▶ 音声認識 ─┴─▶ アプリ（WebView2）─▶ 頭脳の中継 ─┬▶ Ollama（GPU）
         （Silero VAD）     （ReazonSpeech, CPU）    │                       ├▶ 記憶（data\）
                                                    │                       └▶ Web 検索（検索語とページ取得だけ外へ）
                                                    └─▶ 声の中継 ─▶ AivisSpeech ─▶ ヘッドホン
```

## 中身

| パス | 内容 |
|---|---|
| `maid.bat` / `setup.bat` / `start.bat` | 入口。中身は `scripts/maid.ps1` |
| `scripts/maid.ps1` | ダウンロード・起動・停止・診断・片付け（`maid.bat` で一覧） |
| `app/maid_app.py` | アプリ（WebView2）。起動の画面と会話のウィンドウ |
| `app/chat.py` | 会話の中身。頭脳の中継に話しかけ、声で返事するときは文ごとに読ませる。キャラから話しかける |
| `app/listen.py` | 声を聞く。画面から届くマイクの音 → 話し終わりの判定（Silero VAD）→ 音声認識（ReazonSpeech） |
| `app/screen.py` | 画面を 1 枚撮る・貼られた画像を小さくする（頭脳に見せる） |
| `app/common.py` | 設定（`maid.settings.json`）・キャラ（`character.yaml`）・アイコン（`data\icons\`）の読み書き |
| `app/ui/` | 起動の画面（`launcher.html`）と会話の画面（`chat.html`） |
| `services/llm_proxy.py` | 頭脳の中継。考えるモードを切る、モデルのすり替え、繰り返しの打ち切り、今の日時と検索結果と記憶を渡す |
| `services/memory.py` | 記憶。会話ログ・前の会話の要約・覚えていること（`data/` に保存） |
| `services/web_search.py` | Web 検索（DuckDuckGo）とページ本文（PDF も）の抜き出し。外に出るのは検索語と開いたページだけ |
| `services/tts_bridge.py` | 声の中継。AivisSpeech を OpenAI 互換の TTS として見せる |
| `services/winutil.py` | Windows の小物（最後に PC を触ってからの秒数・Discord で通話中か） |
| `config/maid.settings.default.json` | `maid.settings.json`（いつも使う設定）の既定値 |
| `config/character.default.yaml` | `character.yaml`（キャラの名前・性格）の雛形 |
| `tests/` | テスト（`runtime\uv\uv.exe run --no-project --python 3.10 --with openai --with pyyaml --with pypdf --with numpy --with pillow python -m unittest discover -s tests`） |
| `runtime/` | ダウンロードしたもの一式（git の管理外。消せば元通り） |
| `data/` | 記憶・会話・アイコン（git の管理外。消すと全部忘れる） |
