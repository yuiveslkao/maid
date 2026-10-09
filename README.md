# maid

PC 上でいつでも音声で話せる、好みの声と性格のキャラクター兼調べものエージェント（個人用）。
基盤は [Open-LLM-VTuber](https://github.com/Open-LLM-VTuber/Open-LLM-VTuber) v1.2.1 で、本体は改造せず外付けで拡張します。

- 仕様と今後の計画: [docs/SPEC.md](docs/SPEC.md)
- まず動かす（Phase 1・完全ローカル）: [docs/SETUP_WINDOWS.md](docs/SETUP_WINDOWS.md)

## 中身

| パス | 内容 |
|---|---|
| `config/open-llm-vtuber/conf.yaml` | Open-LLM-VTuber 用の設定（ローカル LLM・SenseVoice・AivisSpeech・低遅延向け） |
| `bridge/aivis_openai_bridge.py` | AivisSpeech Engine を OpenAI 互換 TTS として見せる中継サーバー（標準ライブラリのみ） |
| `gate/discord_gate.py` | Discord で通話中は声に反応しないようにする中継（画面と本体の間に挟む） |
| `scripts/start.ps1` / `start.bat` | AivisSpeech → ブリッジ → Discord ゲート → Open-LLM-VTuber を一括起動 |
| `scripts/bench_latency.py` | LLM と TTS の応答速度を測る |
| `tests/` | テスト（`uv run --no-project --with "websockets>=13" --with openai python -m unittest discover -s tests`） |
