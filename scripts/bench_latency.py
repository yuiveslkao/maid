"""応答速度の計測。「話し終わってから声が出るまで」のうち、LLM と TTS の部分を測る。

    python scripts/bench_latency.py --model qwen3:8b
    python scripts/bench_latency.py --model qwen3:8b --model qwen3:4b   # 複数モデルを比較

標準ライブラリだけで動く。Ollama とブリッジ（AivisSpeech）を起動してから実行する。
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
import urllib.request

PROMPTS = ["ねえ、今日ちょっと疲れちゃった", "おすすめの夜ごはんある？", "明日って何曜日だっけ"]
CLAUSE_END = "、。！？!?,."
SYSTEM = "あなたは明るいアシスタントです。日本語の話し言葉で1〜2文で短く答えてください。"


def gpu_share(base_url: str, model: str) -> str:
    """Ollama の /api/ps から、モデルが GPU に何割載っているかを返す。"""
    try:
        with urllib.request.urlopen(base_url.rstrip("/").removesuffix("/v1") + "/api/ps", timeout=10) as res:
            for m in json.loads(res.read()).get("models", []):
                if m.get("name") == model or m.get("model") == model:
                    size, vram = m.get("size") or 0, m.get("size_vram") or 0
                    if size:
                        pct = vram / size * 100
                        warn = "" if pct >= 99 else "  ← 一部が CPU で動いていて遅くなります"
                        return f"{size / 2**30:.1f}GB のうち GPU {pct:.0f}%{warn}"
    except (OSError, ValueError):
        pass
    return "不明"


def llm_first_clause(base_url: str, model: str, prompt: str, think: bool = False) -> tuple[float, float, str, float]:
    """(最初のトークンまでの秒, 最初の読点/句点までの秒, 最初の文節, 考えていた秒)"""
    req = {
        "model": model, "stream": True, "temperature": 0.7, "top_p": 0.8,
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
    }
    if not think:
        req["reasoning_effort"] = "none"  # maid の頭脳の中継と同じ条件で測る
    body = json.dumps(req).encode()
    req = urllib.request.Request(base_url.rstrip("/") + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    start = time.perf_counter()
    ttft = None
    think_start = think_end = None
    text = ""
    with urllib.request.urlopen(req, timeout=120) as res:
        for raw in res:
            line = raw.decode("utf-8").strip()
            if not line.startswith("data:") or line == "data: [DONE]":
                continue
            d = json.loads(line[5:])["choices"][0]["delta"]
            if d.get("reasoning") or d.get("reasoning_content"):  # 本文とは別に送られる「考え中」
                now = time.perf_counter() - start
                think_start = now if think_start is None else think_start
                think_end = now
                continue
            delta = d.get("content") or ""
            if not delta:
                continue
            if ttft is None:
                ttft = time.perf_counter() - start
            text += delta
            if "<think>" in text and "</think>" not in text:
                continue  # 考え中の部分は読み上げられないので待つ
            visible = text.split("</think>")[-1].strip()
            if any(c in CLAUSE_END for c in visible):
                break
    thought = (think_end - think_start) if think_start is not None else 0.0
    if "</think>" in text:
        thought = max(thought, ttft or 0.0)
    return ttft or 0.0, time.perf_counter() - start, text.split("</think>")[-1].strip(), thought


def tts_time(bridge_url: str, text: str) -> float:
    body = json.dumps({"model": "aivisspeech", "voice": "default", "input": text, "response_format": "wav"}).encode()
    req = urllib.request.Request(bridge_url.rstrip("/") + "/audio/speech", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    start = time.perf_counter()
    with urllib.request.urlopen(req, timeout=60) as res:
        res.read()
    return time.perf_counter() - start


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--model", action="append", help="Ollama のモデル名（複数指定可）")
    p.add_argument("--ollama", default="http://127.0.0.1:11436/v1", help="Ollama 本体（maid では 11436）")
    p.add_argument("--think", action="store_true", help="考えるモードを切らずに測る")
    p.add_argument("--bridge", default="http://127.0.0.1:10102/v1")
    p.add_argument("--asr", type=float, default=0.15, help="音声認識にかかる想定秒（SenseVoice CPU で 0.1〜0.2 秒程度）")
    p.add_argument("--vad", type=float, default=0.45,
                   help="話し終わり判定の待ち時間の想定秒（画面の Redemption Frames × 0.032。初期値 35 なら 1.1 秒）")
    a = p.parse_args()

    for model in a.model or ["qwen3:8b"]:
        print(f"\n=== {model} ===")
        print("  読み込み中...（モデルを切り替えた直後は 1 分ほどかかります）", flush=True)
        llm_first_clause(a.ollama, model, "こんにちは", a.think)  # ウォームアップ（モデル読み込み）
        print(f"  メモリ: {gpu_share(a.ollama, model)}")
        firsts, clauses, ttses = [], [], []
        for prompt in PROMPTS:
            ttft, clause_t, clause, thought = llm_first_clause(a.ollama, model, prompt, a.think)
            try:
                tts_t = tts_time(a.bridge, clause or "うん")
            except OSError as e:
                print(f"  TTS に接続できません ({e})。LLM だけ測ります。")
                tts_t = 0.0
            firsts.append(ttft)
            clauses.append(clause_t)
            ttses.append(tts_t)
            note = f" / うち考え中 {thought*1000:4.0f}ms（考えるモードが ON になっています）" if thought > 0.05 else ""
            print(f"  「{prompt}」→「{clause}」  最初の文節 {clause_t*1000:4.0f}ms / TTS {tts_t*1000:4.0f}ms{note}")
        llm, tts = statistics.median(clauses), statistics.median(ttses)
        total = a.vad + a.asr + llm + tts
        print(f"  中央値: LLM 最初の文節 {llm*1000:.0f}ms + TTS {tts*1000:.0f}ms")
        print(f"  想定の体感遅延: VAD {a.vad:.2f}s + ASR {a.asr:.2f}s + LLM {llm:.2f}s + TTS {tts:.2f}s ≒ {total:.2f}s")


if __name__ == "__main__":
    main()
