# maid

PC 上でいつでも文字と声で話せる、好みの声と性格のキャラクター兼調べものエージェント（個人用）。
Windows のアプリ（maid.exe）で、頭脳（LLM）・声・音声認識はすべて PC の中で動かします。

- 仕様と今後の計画: [docs/SPEC.md](docs/SPEC.md)
- セットアップと使い方: [docs/SETUP_WINDOWS.md](docs/SETUP_WINDOWS.md)

PC を汚さない構成です。インストーラーや PATH の変更は使わず、必要なものは全部 maid フォルダの `runtime\` に入ります。

## 仕組み

```
キーボード・貼った画像・画面のスクショ ─┐
マイク（画面で雑音・エコー除去）─▶ 話し終わりの判定 ─▶ 音声認識 ─┴─▶ maid.exe ─┬▶ 頭脳: llama-server（Gemma 4 E4B, GPU）
                              （Silero VAD）     （ReazonSpeech, CPU）        ├▶ 記憶: data\ ＋ llama-server（bge-m3, CPU）
                                                                              ├▶ Web 検索（検索語とページ取得だけ外へ）
                                                                              └▶ 声: AivisSpeech Engine ─▶ ヘッドホン
```

## 中身

| パス | 内容 |
|---|---|
| `src/Maid/` | maid.exe（C# / .NET 8・WinForms・WebView2）。ウィンドウ、部品の起動と後片付け、セットアップ、画面を撮る |
| `src/Maid/ui/` | 画面（セットアップ・起動の画面・会話）。exe に埋め込む |
| `src/Maid.Core/` | Windows 以外でも動く中身。頭脳への話しかけ（`Brain.cs`）、記憶（`Memory.cs`）、検索（`WebSearch.cs`）、声（`Voice.cs`）、耳（`Listen.cs`）、設定 |
| `tests/Maid.Tests/` | テスト（`dotnet test tests/Maid.Tests`。Linux でも動く） |
| `config/` | `maid.settings.json` と `character.yaml` の雛形（exe に埋め込む） |
| `.github/workflows/build.yml` | maid.exe を作る（Actions の Artifacts から zip でダウンロード） |
| `runtime/` | ダウンロードしたもの一式（git の管理外。消せば元通り） |
| `data/` | 記憶・会話・アイコン（git の管理外。消すと全部忘れる） |
