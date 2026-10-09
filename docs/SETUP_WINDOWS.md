# Phase 1 セットアップ（Windows・完全ローカル）

ゴール: **ネットに何も出さずに、マイクで話しかけるとキャラが AivisSpeech の声で返事をする**状態にする。

```
マイク ──▶ 画面 ──▶ Discord ゲート (12394) ──▶ [Open-LLM-VTuber (12393)]  VAD（話し始め/終わり検出）→ SenseVoice（文字起こし, CPU）
                │
                ├─▶ Ollama（頭脳, GPU） ── 文ごとにストリーミング
                │
                └─▶ ブリッジ (127.0.0.1:10102) ─▶ AivisSpeech Engine (127.0.0.1:10101) ─▶ ヘッドホン
```

すべて `127.0.0.1`（自分の PC の中）で通信します。ネットを使うのは **初回のダウンロード時だけ** です。

---

## 0. 事前に入れるもの

PowerShell で:

```powershell
winget install --id Git.Git
winget install --id Gyan.FFmpeg
winget install --id astral-sh.uv
winget install --id Python.Python.3.12   # ブリッジ用（無くても uv で動きます）
```

入れたら PowerShell を開き直してください。

## 1. Ollama（頭脳）

1. <https://ollama.com/download> から Windows 版を入れる（入れると常駐します）。
2. モデルを取得:

   ```powershell
   ollama pull qwen3:8b
   ```

   - 8GB VRAM（RTX 3060 Ti）で快適に動くのは **7〜9B クラスの Q4 量子化** までです。
   - もっと速さが欲しければ 4B クラスも試して、`scripts/bench_latency.py` で比べてください（後述）。
   - 2026 年時点で新しい世代の日本語に強いモデルが出ていれば、同じサイズ帯でそちらを使って構いません。
     **ツール呼び出し（function calling）対応**のモデルを選ぶと Phase 2（検索）にそのまま使えます。
   - 「考えてから答える」タイプ（thinking）のモデルは遅くなるので、非 thinking モードで使います
     （Qwen3 は `conf.yaml` の persona 末尾の `/no_think` で切っています）。

## 2. AivisSpeech（声）

1. <https://aivis-project.com/> から **AivisSpeech** 本体を入れる。
2. 一度 AivisSpeech を起動して、初回ダウンロード（約 1GB）が終わるのを待つ。
3. 好みの声のモデルを入れる: AivisSpeech の「設定」→「音声合成モデルの管理」から [AivisHub](https://hub.aivis-project.com/) のモデルを追加できます。
   - モデルごとにライセンスがあります。個人利用ならほぼ問題ありませんが、念のため確認してください。
4. AivisSpeech 本体は閉じて OK（`start.bat` がエンジンだけを裏で起動します）。

> GPU で動かす（`-AivisGpu`）と合成は速くなりますが、VRAM を 1〜2GB 使い、LLM と取り合いになります。
> まずは CPU で試し、`bench_latency.py` の TTS の数字を見て決めてください。

## 3. Open-LLM-VTuber（基盤）

```powershell
cd $HOME
git clone --branch v1.2.1 --recursive https://github.com/Open-LLM-VTuber/Open-LLM-VTuber.git
cd Open-LLM-VTuber
uv sync
```

このリポジトリの設定ファイルをコピーします（`<maid>` はこのリポジトリを置いた場所）:

```powershell
Copy-Item <maid>\config\open-llm-vtuber\conf.yaml .\conf.yaml
```

- 音声認識モデル（SenseVoice, 約 230MB）は初回起動時に自動でダウンロードされます。
- キャラ名・性格は `conf.yaml` の `character_name` / `persona_prompt` を書き換えてください。

## 4. 起動

`<maid>\start.bat` をダブルクリック（または PowerShell で `scripts\start.ps1`）。

```powershell
# 例: Open-LLM-VTuber を別の場所に置いた / 声を指定する
.\scripts\start.ps1 -OlvDir D:\Open-LLM-VTuber -Voice "まい/ノーマル" -Speed 1.1
```

声の名前（またはスタイル ID）は、起動中に <http://127.0.0.1:10102/v1/voices> を開くと一覧が出ます。
ここで決めた声を常に使いたい場合は、`conf.yaml` の `openai_tts.voice` に書いても OK です。

## 5. 画面（GUI）

どちらかで開きます:

- **ブラウザ**: <http://localhost:12393>
- **デスクトップアプリ（おすすめ）**: [Open-LLM-VTuber-Web のリリース](https://github.com/Open-LLM-VTuber/Open-LLM-VTuber-Web/releases) から Windows 版を入れる。
  ウィンドウモードと、デスクトップに常駐する「ペットモード」があります。

**最初に一度だけ**、画面の設定（General）で **WebSocket URL** を
`ws://127.0.0.1:12394/client-ws` に変えてください（Base URL は `http://127.0.0.1:12393` のまま）。
これで Discord ゲートを通るようになり、**Discord で通話中は声に反応しなくなります**（チャット入力は使えます）。

画面の設定で次も確認してください:

| 設定（英語表記） | おすすめ | 理由 |
|---|---|---|
| マイク | 常時 ON | ミュートはマイクの物理ボタンで行う |
| Auto Stop Mic When AI Start Speaking | **OFF** | OFF にしておくと、キャラが話している途中に話しかけて割り込める（ヘッドホンなので自分の声だけに反応する） |
| Auto Start Mic When AI Interrupted / When Conversation End | ON | 割り込み後や会話後もマイクが待受に戻る |
| **Redemption Frames** | **35 → 12〜15** | **話し終わり判定の待ち時間**。1 フレーム ≒ 32ms なので初期値 35 は約 1.1 秒待つ。12〜15（約 0.4〜0.5 秒）にすると返事が大きく速くなる。言いかけで切られるなら少し戻す |
| Speech Prob Threshold | 50 → 雑音で反応するなら 60〜70 | 物音・キーボード音で反応しにくくなる |
| Allow AI to Speak Proactively / Idle seconds | 好みで | 指定秒数黙っているとキャラから話しかけてくる |

Live2D のモデル（初期は Mao）は表示されますが、Phase 1 では気にせず進めてください。

> 雑音対策をもっとしたい場合: RTX 3060 Ti は **NVIDIA Broadcast** のノイズ除去が使えます（マイクを「NVIDIA Broadcast」の仮想マイクに切り替える）。
> GPU を少し使うので、遅延が増えないか確認してください。

## 6. Discord 通話中に止まるか確かめる

1. Discord でボイスチャンネルに入る。
2. 画面に「Discord 通話中なので、声には反応しません」と出て、話しかけても返事をしなければ OK。
3. 通話を抜けると「また聞いています」と出て、元に戻る。

判定だけ確かめたいときは:

```powershell
uv run --no-project --with "websockets>=13" python gate\discord_gate.py --check
```

- 判定には、Windows がアプリごとのマイク使用状況を記録している情報（タスクバーのマイクアイコンと同じ）を使います。
  Windows の「設定 → プライバシーとセキュリティ → マイク」で「デスクトップアプリがマイクにアクセスできるようにする」が ON である必要があります（Discord で話せていれば ON です）。
- Discord の設定画面でマイクテストをしている間も「通話中」と判定されます。
- 通話中もキャラから話しかけてほしい場合は、`discord_gate.py` に `--allow-proactive` を付けて起動します。

## 7. 速さを測る

Ollama とブリッジが起動している状態で:

```powershell
python scripts\bench_latency.py --model qwen3:8b
python scripts\bench_latency.py --model qwen3:8b --model qwen3:4b   # 比較
```

「LLM が最初の読点/句点まで出すのにかかった時間」と「その部分を AivisSpeech で合成する時間」が出ます。
話し終わってから声が出るまで ≒ **VAD の待ち + 音声認識 + この 2 つ** です。

## 8. 外に出ていないか確認する

- `conf.yaml` で `use_mcpp: False`（検索ツールなし）になっている。
- 起動ログに出る URL がすべて `localhost` / `127.0.0.1` になっている。
- 不安なら、Windows のファイアウォールで Ollama / AivisSpeech / Open-LLM-VTuber の送信をブロックしても動きます（モデルのダウンロード後）。

## うまくいかないとき

| 症状 | 確認すること |
|---|---|
| 声が出ない | <http://127.0.0.1:10102/health> を開く。502 なら AivisSpeech Engine が起動していない |
| `voice '...' が見つかりません` | `/v1/voices` の名前と完全一致しているか（「話者名/スタイル名」） |
| 返事が遅い・最初だけ遅い | 初回はモデル読み込みで遅い（2 回目以降で判断）。`ollama ps` で GPU 100% になっているか |
| 返事に `<think>` が混じる / 遅い | thinking モードが ON。persona に `/no_think` があるか、非 thinking モデルに替える |
| 物音で反応する | VAD のしきい値を上げる / NVIDIA Broadcast |
| 画面が「接続できません」 | WebSocket URL が 12394 のとき、ゲートが起動しているか。ゲートなしで使うなら 12393 に戻す |
| Discord を抜けても反応しない | `--check` で判定を確認。Discord を完全に終了しても直らなければ教えてください |
