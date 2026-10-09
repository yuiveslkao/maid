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

## 0. 方針: PC を汚さない

- **インストーラー・winget・PATH の変更は使いません。** 必要なものは全部 `maid\runtime\` に入ります。
- Python や Git を PC に入れる必要もありません（Python は `runtime\` の中に専用のものが入ります）。
- やめたくなったら **maid フォルダを消せば終わり**です（例外は「9. 片付け方」）。

## 1. PC に元から必要なもの（確認だけ）

| もの | 確認方法 | 普通は |
|---|---|---|
| Windows 10 (1803) 以降 / 11 | — | 入っている |
| PowerShell 5.1 以降、curl.exe、tar.exe | Windows に標準で入っている | 入っている |
| NVIDIA ドライバー | PowerShell で `nvidia-smi` → GPU 名が出れば OK | ゲームをしていれば入っている |
| Visual C++ ランタイム | `C:\Windows\System32\msvcp140.dll` があれば OK | Discord やゲームと一緒に入っている |
| 空き容量 | 20GB 程度 | — |

あとで出てくる `maid.bat check` が全部まとめて確認してくれるので、ここは読むだけで大丈夫です。

> **PowerShell で実行するときは先頭に `.\` を付けます**（例: `.\maid.bat check`）。PowerShell は今いるフォルダのコマンドをそのままでは実行しないためです。エクスプローラーからダブルクリックするなら不要です。

## 2. maid を置く

GitHub のこのリポジトリのページで、ブランチ `claude/stoic-davinci-cg39zh` を選んで **Code → Download ZIP**。
好きな場所（例: `D:\maid`）に展開します。**パスに日本語や空白が無い場所**だと安心です。

## 3. 必要なものをダウンロードする

`setup.bat` をダブルクリック（または `maid.bat setup`）。次のものを `runtime\` に入れます。

| もの | 用途 | 大きさの目安 |
|---|---|---|
| uv | Python とライブラリの管理（Python 本体も `runtime\` に入れる） | 数十 MB |
| Ollama（zip 版） | 頭脳を動かす | 約 2GB |
| AivisSpeech Engine（エンジンだけ） | 声 | 約 1〜2GB |
| 7zr.exe | AivisSpeech の展開用（7-Zip 公式の単体版） | 1MB 未満 |
| Open-LLM-VTuber v1.2.1 ＋ 画面 | 基盤 | 数 GB（ライブラリ込み） |
| 頭脳のモデル（qwen3:8b） | — | 約 5GB |

- 何度実行しても大丈夫です（そろっているものは飛ばします）。途中で失敗したらもう一度実行してください。
- 別のモデルにしたいときは `maid.bat setup -Model qwen3:4b` のように指定します。
  - 8GB VRAM（RTX 3060 Ti）で快適に動くのは **7〜9B クラスの Q4 量子化** までです。
  - 2026 年時点で新しい世代の日本語に強いモデルが出ていれば、同じサイズ帯でそちらを使って構いません。
    **ツール呼び出し（function calling）対応**のモデルを選ぶと Phase 2（検索）にそのまま使えます。
  - 「考えてから答える」タイプ（thinking）のモデルは遅くなるので、非 thinking モードで使います
    （Qwen3 は `conf.yaml` の persona 末尾の `/no_think` で切っています）。
- ffmpeg は不要です（声は wav でやりとりするため）。

終わったら確認:

```bat
maid.bat check
```

`NG` が出たら、その下に出る対処を見てください。

## 4. キャラの設定

`runtime\Open-LLM-VTuber\conf.yaml` の `character_name` / `persona_prompt` を書き換えます。
（元の雛形は `config\open-llm-vtuber\conf.yaml`。setup は既にある conf.yaml を上書きしません）

## 5. 起動

`start.bat` をダブルクリック。Ollama → AivisSpeech Engine → ブリッジ → Discord ゲート → Open-LLM-VTuber の順に起動し、
準備ができたら **Edge がアプリ風のウィンドウで画面を開きます**（Edge は Windows 標準なので追加インストール不要）。

- 裏で動く部品（Ollama・AivisSpeech・ブリッジ・Discord ゲート）はウィンドウを出さず、ログを `runtime\logs\` に書きます。
  **start.bat のウィンドウを閉じるか Ctrl+C で全部まとめて止まります。** 何か残ってしまったときは `maid.bat stop`。
- **終了するとき**: start.bat の黒いウィンドウを閉じる（または Ctrl+C）。Edge のウィンドウを閉じても画面が消えるだけで、裏の部品は動き続けます。
- **初回の起動は時間がかかります**: AivisSpeech の初期モデル（約 1GB）と音声認識モデル（約 230MB）をダウンロードするため。
- 初回はマイクの使用許可を聞かれるので「許可」してください。

```bat
rem 例: 声と話速を指定して起動 / AivisSpeech を GPU で動かす / 画面を自動で開かない
maid.bat start -Voice "まい/ノーマル" -Speed 1.1
maid.bat start -AivisGpu
maid.bat start -NoBrowser
```

> AivisSpeech を GPU で動かす（`-AivisGpu`）と合成は速くなりますが、VRAM を 1〜2GB 使い、LLM と取り合いになります。
> まずは CPU で試し、速さを測ってから決めてください。

### 声を選ぶ・増やす

- 使える声の一覧: 起動中に <http://127.0.0.1:10102/v1/voices>
- 声を増やす: [AivisHub](https://hub.aivis-project.com/) で好きなモデルのページを開き、その URL で
  `maid.bat add-voice https://hub.aivis-project.com/aivm-models/...`
  - モデルごとにライセンスがあります。個人利用ならほぼ問題ありませんが、念のため確認してください。
- いつも使う声は `start.bat` を編集して `-Voice` を付けるか、`conf.yaml` の `openai_tts.voice` に書きます。

## 6. 画面の設定

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
> ただし別途インストールが必要で、GPU も少し使います。まずは VAD のしきい値調整で足りるか試してください。

## 7. Discord 通話中に止まるか確かめる

1. Discord でボイスチャンネルに入る。
2. 画面に「Discord 通話中なので、声には反応しません」と出て、話しかけても返事をしなければ OK。
3. 通話を抜けると「また聞いています」と出て、元に戻る。

判定だけ確かめたいときは:

```bat
maid.bat discord-check
```

- 判定には、Windows がアプリごとのマイク使用状況を記録している情報（タスクバーのマイクアイコンと同じ）を使います。
  Windows の「設定 → プライバシーとセキュリティ → マイク」で「デスクトップアプリがマイクにアクセスできるようにする」が ON である必要があります（Discord で話せていれば ON です）。
- Discord の設定画面でマイクテストをしている間も「通話中」と判定されます。

## 8. 速さを測る

start.bat で起動している状態で、別のウィンドウから:

```bat
maid.bat bench
maid.bat bench qwen3:8b,qwen3:4b
```

（比べるモデルは先に `maid.bat setup -Model qwen3:4b` で取得しておく）

「LLM が最初の読点/句点まで出すのにかかった時間」と「その部分を AivisSpeech で合成する時間」が出ます。
話し終わってから声が出るまで ≒ **VAD の待ち + 音声認識 + この 2 つ** です。

## 9. 片付け方

1. maid フォルダを消す（`runtime\` ごと消えます）。
2. maid フォルダの外に作られるもの（消したければ手で消す）:
   - `%APPDATA%\AivisSpeech-Engine` … AivisSpeech の声モデルと辞書（約 1GB）。保存先を変える設定がエンジンに無いため。
   - `%USERPROFILE%\.ollama` … Ollama の識別用の鍵ファイルなど（数 KB）。モデル本体は `runtime\` にあります。
   - Edge に残る「127.0.0.1 のマイク許可」と画面の設定。

## 10. 外に出ていないか確認する

- `conf.yaml` で `use_mcpp: False`（検索ツールなし）になっている。
- 起動ログに出る URL がすべて `localhost` / `127.0.0.1` になっている。
- 不安なら、Windows のファイアウォールで Ollama / AivisSpeech / Open-LLM-VTuber の送信をブロックしても動きます（モデルのダウンロード後）。

## うまくいかないとき

| 症状 | 確認すること |
|---|---|
| 声が出ない | <http://127.0.0.1:10102/health> を開く。502 なら AivisSpeech Engine が起動していない |
| `voice '...' が見つかりません` | `/v1/voices` の名前と完全一致しているか（「話者名/スタイル名」） |
| 返事が遅い・最初だけ遅い | 初回はモデル読み込みで遅い（2 回目以降で判断）。`runtime\ollama\ollama.exe ps` で GPU 100% になっているか |
| 返事に `<think>` が混じる / 遅い | thinking モードが ON。persona に `/no_think` があるか、非 thinking モデルに替える |
| 物音で反応する | VAD のしきい値を上げる / NVIDIA Broadcast |
| 画面が「接続できません」 | WebSocket URL が 12394 のとき、ゲートが起動しているか。ゲートなしで使うなら 12393 に戻す |
| Discord を抜けても反応しない | `--check` で判定を確認。Discord を完全に終了しても直らなければ教えてください |
