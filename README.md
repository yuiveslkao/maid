# maid

PC 上でいつでも音声で話せる、好みの声と性格のキャラクター兼調べものエージェント（個人用）。
基盤は [Open-LLM-VTuber](https://github.com/Open-LLM-VTuber/Open-LLM-VTuber) v1.2.1 で、本体は改造せず外付けで拡張します。

- 仕様と今後の計画: [docs/SPEC.md](docs/SPEC.md)
- まず動かす（Phase 1・完全ローカル）: [docs/SETUP_WINDOWS.md](docs/SETUP_WINDOWS.md)

PC を汚さない構成です。インストーラーや PATH の変更は使わず、必要なものは全部 `runtime\` に入ります。

```bat
setup.bat          rem 必要なものを runtime\ にダウンロード
maid.bat check     rem そろっているか確認
start.bat          rem 起動
```

## 中身

| パス | 内容 |
|---|---|
| `config/open-llm-vtuber/conf.yaml` | Open-LLM-VTuber 用の設定（ローカル LLM・SenseVoice・AivisSpeech・低遅延向け） |
| `bridge/aivis_openai_bridge.py` | AivisSpeech Engine を OpenAI 互換 TTS として見せる中継サーバー（標準ライブラリのみ） |
| `gate/discord_gate.py` | Discord で通話中は声に反応しないようにする中継（画面と本体の間に挟む） |
| `scripts/maid.ps1`（`maid.bat` / `setup.bat` / `start.bat`） | ダウンロード・確認・一括起動・速度計測・声の追加 |
| `scripts/bench_latency.py` | LLM と TTS の応答速度を測る |
| `tests/` | テスト（`runtime\uv\uv.exe run --no-project --with "websockets>=13" --with openai python -m unittest discover -s tests`） |
