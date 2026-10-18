"""チャットだけの版の中身（画面は app/ui/chat.html）。

Open-LLM-VTuber を使わずに、頭脳の中継（services/llm_proxy.py）に直接話しかける。
検索・URL を読む・記憶・今の日時は、頭脳の中継がやるので、ここでは何もしなくてよい。

- キャラの設定（名前・persona_prompt）は Open-LLM-VTuber の conf.yaml から読む（フル版と同じキャラになる）
- 会話は data\\chat.json に残す（次に開いたとき続きから見える）
- 声で返事するときは、文ごとに声の中継（services/tts_bridge.py）に読ませ、画面で順に鳴らす
- しばらく話さなかったら、キャラから話しかける（フル版と同じく、話題は頭脳の中継が記憶から選ぶ）
"""

from __future__ import annotations

import base64
import json
import logging
import queue
import re
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable

log = logging.getLogger("chat")

HISTORY_KEEP = 40          # 頭脳に渡す・保存する発言の数
PROACTIVE_MARK = "Please say something that would be engaging and appropriate for the current context."
THINK_BLOCK = re.compile(r"<think>.*?</think>", re.S)
SENTENCE_END = re.compile(r"(?<=[。！？!?♪〜…])|\n")
UNSPOKEN = [re.compile(p, re.S) for p in (r"<think>.*?</think>", r"<think>.*$", r"\[[^\]]*\]", r"\([^)]*\)",
                                          r"（[^）]*）", r"\*[^*]*\*")]


def load_character(conf: Path | None) -> dict:
    """conf.yaml からキャラの名前・相手の名前・persona_prompt を読む。無ければ簡単な既定値。"""
    out = {"name": "メイド", "human": "ご主人様", "persona": "あなたは親しみやすいアシスタントです。日本語の話し言葉で、短く答えてください。"}
    if conf and conf.exists():
        try:
            import yaml

            cc = (yaml.safe_load(conf.read_text(encoding="utf-8")) or {}).get("character_config", {})
            out.update({k: str(cc[key]) for k, key in (("name", "character_name"), ("human", "human_name"), ("persona", "persona_prompt"))
                        if cc.get(key)})
        except Exception as e:
            log.warning("キャラの設定を読めませんでした (%s): %s", conf, e)
    return out


def spoken(text: str) -> str:
    """読み上げる部分だけ（出典や括弧の中は読まない）。"""
    for pat in UNSPOKEN:
        text = pat.sub("", text)
    return text.strip()


class Speaker:
    """返事を文ごとに区切って、声の中継に読ませる。できた声は play(base64 の wav) に渡す（届いた順に 1 つずつ）。"""

    def __init__(self, tts_url: str, play: Callable[[str], None]):
        self.tts_url = tts_url.rstrip("/")
        self.play = play
        self.buf = ""
        self.generation = 0
        self.queue: queue.Queue = queue.Queue()
        threading.Thread(target=self._worker, daemon=True).start()

    def reset(self) -> None:
        """割り込み: まだ読んでいない文は捨てる。"""
        self.generation += 1
        self.buf = ""

    def feed(self, chunk: str, final: bool = False) -> None:
        # <think>…</think>（出典）は読まないので、文に分ける前に取り除く。閉じていなければ閉じるまで待つ
        self.buf = THINK_BLOCK.sub("", self.buf + chunk)
        cut = self.buf.find("<think>")
        ready, held = (self.buf, "") if cut < 0 else (self.buf[:cut], self.buf[cut:])
        parts = SENTENCE_END.split(ready)
        tail = parts.pop()
        self.buf = "" if final else tail + held
        for part in parts + ([tail] if final else []):
            text = spoken(part)
            if text:
                self.queue.put((text, self.generation))

    def _worker(self) -> None:
        while True:
            text, generation = self.queue.get()
            if generation != self.generation:
                continue
            wav = self.synthesize(text)
            if wav and generation == self.generation:
                self.play(base64.b64encode(wav).decode())

    def synthesize(self, text: str) -> bytes | None:
        try:
            body = json.dumps({"input": text, "voice": "default", "response_format": "wav"}, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(self.tts_url + "/v1/audio/speech", data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as res:
                return res.read()
        except OSError as e:
            log.warning("声を作れませんでした: %s", e)
            return None


class ChatSession:
    def __init__(self, proxy_url: str, character: dict, history_path: Path | None = None):
        self.proxy_url = proxy_url.rstrip("/")
        self.character = character
        self.history_path = history_path
        self.history: list[dict] = self._load()
        self.lock = threading.Lock()
        self.busy = False
        self.last_activity = time.monotonic()
        self.cancel = threading.Event()

    def _load(self) -> list[dict]:
        try:
            data = json.loads(self.history_path.read_text(encoding="utf-8")) if self.history_path else []
            return [m for m in data if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)][-HISTORY_KEEP:]
        except (OSError, ValueError):
            return []

    def _save(self) -> None:
        if self.history_path:
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            self.history_path.write_text(json.dumps(self.history[-HISTORY_KEEP:], ensure_ascii=False, indent=1), encoding="utf-8")

    def send(self, text: str, on_chunk: Callable[[str], None], proactive: bool = False) -> str:
        """頭脳の中継に話しかけ、返事を少しずつ on_chunk に渡す。戻り値は返事の全文。

        キャラから話しかけるとき（proactive）は、話題は頭脳の中継が選ぶ。話しかけた内容は、相手が返事をしたときに
        頭脳の中継が足すので、ここの会話の記録には入れない（フル版と同じ）。"""
        with self.lock:
            self.busy = True
            self.cancel.clear()
            self.last_activity = time.monotonic()
            messages = [{"role": "system", "content": self.character["persona"]}] + self.history[-HISTORY_KEEP:]
            messages.append({"role": "user", "content": PROACTIVE_MARK if proactive else text})
            body = json.dumps({"model": "maid", "stream": True, "messages": messages}, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(self.proxy_url + "/v1/chat/completions", data=body, headers={"Content-Type": "application/json"})
            reply = ""
            try:
                with urllib.request.urlopen(req, timeout=600) as res:
                    for raw in res:
                        if self.cancel.is_set():
                            break
                        line = raw.decode("utf-8", "replace").strip()
                        if not line.startswith("data:") or line == "data: [DONE]":
                            continue
                        try:
                            ev = json.loads(line[5:])
                        except ValueError:
                            continue
                        for c in ev.get("choices") or []:
                            piece = (c.get("delta") or {}).get("content") or ""
                            if piece:
                                reply += piece
                                on_chunk(piece)
            except OSError as e:
                log.warning("頭脳の中継に話しかけられませんでした: %s", e)
                piece = "（頭脳につながりませんでした。maid が起動しているか確かめてください）"
                on_chunk(piece)
                return piece
            finally:
                self.busy = False
                self.last_activity = time.monotonic()
            if not proactive:
                self.history += [{"role": "user", "content": text}, {"role": "assistant", "content": reply}]
                self._save()
            return reply

    def clear(self) -> None:
        self.history = []
        self._save()


def proactive_loop(session: ChatSession, minutes: float, speak: Callable[[], None], idle: Callable[[], float],
                   stop: threading.Event, away_seconds: float = 900, poll: float = 20) -> None:
    """会話が minutes 分無く、PC を使っているときに speak() を呼ぶ（キャラから話しかける）。"""
    if minutes <= 0:
        return
    while not stop.wait(poll):
        quiet = time.monotonic() - session.last_activity
        if not session.busy and quiet >= minutes * 60 and idle() < away_seconds:
            session.last_activity = time.monotonic()
            speak()
