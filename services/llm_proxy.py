"""頭脳の中継。Ollama の前に置き、Open-LLM-VTuber からのチャットに手を加える。

- 「考えてから答える」モードを切る（reasoning_effort: "none"）。切らないと、見えないところで数秒考えてから話し始める。
- 使うモデルを maid.settings.json の model にすり替える（conf.yaml のモデル名は使わない）。
- 返事の長さに上限を付け、同じ言葉の繰り返し（無限ループ）を見つけたら打ち切る。
- 今の日時を伝える。必要なときは Web 検索して結果を渡し、返事の最後に出典を付ける（services/web_search.py）。

それ以外のリクエスト（/api/... など）はそのまま Ollama に渡す。

    python services/llm_proxy.py --port 11434 --ollama http://127.0.0.1:11436 --model <モデル名> --search auto
"""

from __future__ import annotations

import argparse
import http.client
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import web_search  # noqa: E402  （検索しないときは追加パッケージを読み込まない）

log = logging.getLogger("llm_proxy")

HOP_HEADERS = {"connection", "keep-alive", "transfer-encoding", "content-length", "host", "proxy-connection", "upgrade"}
# チャットに足す設定（リクエストに値があればそちらを優先）
CHAT_DEFAULTS = {"reasoning_effort": "none", "top_p": 0.8, "max_tokens": 300, "presence_penalty": 0.5}
SEARCH_PAGES = 3          # 本文を読むページ数
FOLLOWUP_TURNS = 2        # 調べたあと、続きの質問に前回の結果を渡す回数
FOLLOWUP_SECONDS = 600    # 〃 の期限

WEEKDAYS = "月火水木金土日"
# 過去の返事に付けた出典（今の形と前の形）
SOURCES_RE = re.compile(r"\s*(<think>出典: .*?</think>|\[出典: [^\]]*\])", re.S)
CLOCK_NOTE = "現在の日時は {now}。時刻・日付・曜日を聞かれたら、この日時を使って正確に答える（調べなくてよい）。"
SEARCH_NOTE = ("（補足）Web 検索の結果が渡されたときは、それを使って最新の情報を答えてよい。"
               "結果が渡されていないのに、天気・ニュース・日付などの最新情報を推測で答えない。")
QUERY_PROMPT = ("あなたは検索語を作る係です。会話の流れを踏まえて、最後の発言で調べたいことを Web 検索するための"
                "短い日本語の検索語を 1 行だけ出力してください。「調べて」だけのときは、その前の話題を調べる。"
                "説明や記号は付けない。会話相手の名前や家族の情報など、個人的な情報は入れない。"
                "検索の必要がなければ「なし」とだけ出力する。")
QUERY_CONTEXT_MESSAGES = 6  # 検索語を作るときに見せる直前の発言の数
STOP_EVENT = b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'


def with_defaults(req: dict, model: str | None) -> dict:
    out = {**req}
    if model and "model" in out:
        out["model"] = model
    for k, v in CHAT_DEFAULTS.items():
        if out.get(k) is None and not (k == "reasoning_effort" and "reasoning" in out):
            out[k] = v
    return out


def rewrite_model(body: bytes, model: str | None) -> bytes:
    """/api/chat などのモデル名だけをすり替える（Open-LLM-VTuber が起動時にモデルを読み込ませる呼び出し）。"""
    try:
        req = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return body
    if not model or not isinstance(req, dict) or "model" not in req:
        return body
    return json.dumps({**req, "model": model}, ensure_ascii=False).encode("utf-8")


def is_looping(text: str) -> bool:
    """末尾が同じ並びの繰り返しになっていたら True（例: 「ぱぱ、ぱぱ、ぱぱ、ぱぱ、」）。"""
    t = text[-400:]
    for n in range(1, 81):
        reps = 8 if n <= 4 else (4 if n <= 20 else 3)
        if len(t) >= n * reps and t[-n * reps:] == t[-n:] * reps:
            return True
    return False


def content_of(event: dict) -> str:
    """OpenAI 形式の応答（ストリームの 1 イベント、または全体）の本文。"""
    out = []
    for c in event.get("choices") or []:
        part = c.get("delta") or c.get("message") or {}
        if isinstance(part.get("content"), str):
            out.append(part["content"])
    return "".join(out)


def sse_text(text: str) -> bytes:
    """返事の一部として流す SSE イベント（「ちょっと調べるね。」や出典の行）。"""
    ev = {"id": "maid", "object": "chat.completion.chunk", "created": int(time.time()), "model": "maid",
          "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}]}
    return b"data: " + json.dumps(ev, ensure_ascii=False).encode("utf-8") + b"\n\n"


def text_of(message: dict) -> str:
    c = message.get("content")
    if isinstance(c, list):
        return "".join(p.get("text", "") for p in c if isinstance(p, dict))
    return c if isinstance(c, str) else ""


def last_user_text(messages: list) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            return text_of(m)
    return ""


def insert_before_last_user(messages: list, text: str) -> list:
    out = list(messages)
    for k in range(len(out) - 1, -1, -1):
        if out[k].get("role") == "user":
            out.insert(k, {"role": "system", "content": text})
            return out
    return out + [{"role": "system", "content": text}]


def add_note(messages: list, note: str) -> list:
    """最初の system メッセージの末尾に一言足す（無ければ先頭に足す）。"""
    out = list(messages)
    for k, m in enumerate(out):
        if m.get("role") == "system" and isinstance(m.get("content"), str):
            out[k] = {**m, "content": m["content"].rstrip() + "\n\n" + note}
            return out
    return [{"role": "system", "content": note}] + out


def clean_history(messages: list, filler: str) -> list:
    """過去の返事から出典と「ちょっと調べるね。」を取り除く（残すと頭脳が真似して自分で書き始める）。"""
    out = []
    for m in messages:
        if m.get("role") == "assistant" and isinstance(m.get("content"), str):
            c = SOURCES_RE.sub("", m["content"])
            if filler and c.startswith(filler):
                c = c[len(filler):]
            m = {**m, "content": c}
        out.append(m)
    return out


def query_request(messages: list) -> list:
    """検索語づくりに渡すメッセージ（直前の会話を 1 つの文章にまとめる）。"""
    talk = [m for m in messages if m.get("role") in ("user", "assistant")][-QUERY_CONTEXT_MESSAGES:]
    lines = [("相手: " if m["role"] == "user" else "キャラ: ") + text_of(m).strip() for m in talk]
    if lines:
        lines[-1] = "最後の発言: " + text_of(talk[-1]).strip()
    return [{"role": "system", "content": QUERY_PROMPT}, {"role": "user", "content": "\n".join(lines)}]


def fallback_query(messages: list) -> str:
    """頭脳が検索語を作れなかったときの検索語。「調べて」だけなら、その前の相手の発言から作る。"""
    for m in reversed(messages):
        if m.get("role") == "user":
            q = web_search.fallback_query(text_of(m))
            if q:
                return q
    return ""


def preload(upstream: str, model: str) -> None:
    """起動してすぐ、頭脳のモデルを GPU に読み込ませておく（最初の返事が遅くならないように）。"""
    up = urllib.parse.urlparse(upstream)
    body = json.dumps({"model": model, "keep_alive": -1}).encode()
    for attempt in range(120):
        try:
            conn = http.client.HTTPConnection(up.hostname, up.port or 80, timeout=600)
            started = time.perf_counter()
            conn.request("POST", "/api/generate", body=body, headers={"Content-Type": "application/json"})
            res = conn.getresponse()
            res.read()
            conn.close()
            if res.status == 200:
                log.info("モデルを読み込みました (%.1f 秒)", time.perf_counter() - started)
            else:
                log.warning("モデルを読み込めませんでした (%s)。maid.settings.json の model と setup を確認してください", res.status)
            return
        except OSError:
            time.sleep(1)  # Ollama の起動待ち


def make_handler(upstream: str, model: str | None = None, search_mode: str = "off", search_filler: str = "",
                 search_fn=None):
    up = urllib.parse.urlparse(upstream)
    search_fn = search_fn or web_search.search
    # 直前の検索結果（「それっていつ？」のような続きの質問に使う）
    last_search: dict = {"ctx": None, "turns": 0}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            log.debug("%s - %s", self.address_string(), fmt % args)

        # ---------------- 送受信
        def _open(self, body: bytes, path: str | None = None, method: str | None = None):
            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_HEADERS}
            if body or (method or self.command) in ("POST", "PUT", "PATCH"):
                headers["Content-Length"] = str(len(body))
            conn = http.client.HTTPConnection(up.hostname, up.port or 80, timeout=600)
            conn.request(method or self.command, path or self.path, body=body or None, headers=headers)
            return conn, conn.getresponse()

        def _start(self, res=None, chunked: bool = True):
            """応答の頭を送る（1 回だけ）。res が無いときは SSE として始める。"""
            if self._started:
                return
            self._started = True
            if res is None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
            else:
                self.send_response(res.status, res.reason)
                for k, v in res.getheaders():
                    if k.lower() not in HOP_HEADERS:
                        self.send_header(k, v)
            self.send_header("Transfer-Encoding", "chunked") if chunked else self.send_header("Content-Length", "0")
            self.end_headers()

        def _chunk(self, data: bytes):
            if data:
                self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
                self.wfile.flush()

        def _end(self):
            self.wfile.write(b"0\r\n\r\n")

        def _pipe(self, res):
            """残りをそのまま流す（ストリーミングを保つため chunked で中継する）"""
            self._start(res)
            while True:
                chunk = res.read1(65536)
                if not chunk:
                    break
                self._chunk(chunk)
            self._end()

        # ---------------- 検索
        def _make_query(self, req: dict, msgs: list) -> str | None:
            """頭脳に、直前の会話を踏まえた短い検索語を作らせる。作れなければ発言から作る。「なし」なら None。"""
            q = with_defaults({"model": req.get("model", ""), "stream": False, "max_tokens": 40, "temperature": 0.2,
                               "messages": query_request(msgs)}, model)
            line = ""
            try:
                conn, res = self._open(json.dumps(q, ensure_ascii=False).encode("utf-8"), "/v1/chat/completions", "POST")
                try:
                    answer = content_of(json.loads(res.read())) if res.status == 200 else ""
                finally:
                    conn.close()
                line = answer.strip().splitlines()[0].strip(" 「」\"'`") if answer.strip() else ""
            except (OSError, ValueError):
                pass
            if line in ("なし", "無し", "NONE", "none"):
                return None
            return line[:60] or fallback_query(msgs)

        def _augment(self, req: dict) -> tuple[dict, bytes]:
            """日時と、要るなら検索結果を差し込む。戻り値は (新しいリクエスト, 返事の最後に足す出典)。"""
            msgs = clean_history(req["messages"], search_filler)
            history = list(msgs)
            if search_mode != "off":
                msgs = add_note(msgs, SEARCH_NOTE)
            # 今の日時は最後の発言のすぐ前に置く（キャラ設定の後ろに足すより、小さいモデルでも使ってくれる）
            now = datetime.now()
            msgs = insert_before_last_user(msgs, CLOCK_NOTE.format(
                now=f"{now:%Y年%m月%d日}（{WEEKDAYS[now.weekday()]}曜日）{now.hour}時{now.minute}分"))
            text = last_user_text(msgs)
            if req.get("stream") and web_search.needs_search(text, search_mode):
                if search_filler:
                    self._start()
                    self._chunk(sse_text(search_filler))  # 調べている間に読み上げが始まる
                query = self._make_query(req, history)
                if query is None and web_search.needs_search(text, "explicit"):  # はっきり頼まれたら必ず調べる
                    query = fallback_query(history)
                if query:
                    ctx = search_fn(query, SEARCH_PAGES)
                    log.info("検索「%s」→ %s", query, ", ".join(r.site for r in ctx.results) or "見つからず")
                    last_search.update(ctx=ctx, turns=0)
                    msgs = insert_before_last_user(msgs, web_search.build_context(ctx))
                    line = web_search.sources_line(ctx)
                    return {**req, "messages": msgs}, (sse_text(line) if line else b"")
            ctx = last_search["ctx"]
            if ctx is not None and last_search["turns"] < FOLLOWUP_TURNS and (now - ctx.when).total_seconds() < FOLLOWUP_SECONDS:
                last_search["turns"] += 1
                msgs = insert_before_last_user(msgs, web_search.build_context(ctx, followup=True))
            return {**req, "messages": msgs}, b""

        # ---------------- チャット
        def _chat(self, req: dict):
            suffix = b""
            if isinstance(req.get("messages"), list):
                req, suffix = self._augment(req)
            conn, res = self._open(json.dumps(with_defaults(req, model), ensure_ascii=False).encode("utf-8"))
            try:
                if res.status == 200 and req.get("stream"):
                    self._stream(res, suffix)
                elif self._started:  # 「ちょっと調べるね。」を送ったあとに失敗した
                    log.warning("頭脳がエラーを返しました (%s): %s", res.status, res.read()[:300])
                    self._chunk(STOP_EVENT)
                    self._end()
                else:
                    self._pipe(res)
            finally:
                conn.close()

        def _stream(self, res, suffix: bytes):
            """SSE を 1 イベントずつ中継する。suffix（出典）は返事の最後に足す。
            繰り返し（無限ループ）を見つけたら、そこで返事を終わらせる（接続を切って Ollama 側の生成も止める）。"""
            self._start(res)
            text = ""
            while True:
                line = res.readline()
                if not line:
                    break
                s = line.strip()
                if s == b"data: [DONE]":
                    self._chunk(suffix + line)
                    suffix = b""
                    continue
                if s.startswith(b"data:"):
                    try:
                        text += content_of(json.loads(s[5:]))
                    except ValueError:
                        pass
                    if is_looping(text):
                        log.warning("同じ言葉の繰り返しを見つけたので返事を打ち切りました: …%s", text[-40:])
                        self._chunk(line + b"\n" + suffix + STOP_EVENT)
                        self._end()
                        return
                self._chunk(line)
            self._chunk(suffix)
            self._end()

        def _forward(self):
            self._started = False
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            path = urllib.parse.urlparse(self.path).path.rstrip("/")
            try:
                if self.command == "POST" and path == "/v1/chat/completions":
                    try:
                        req = json.loads(body)
                    except ValueError:
                        req = None
                    if isinstance(req, dict):
                        return self._chat(req)
                if self.command == "POST" and path in ("/api/chat", "/api/generate"):
                    body = rewrite_model(body, model)
                conn, res = self._open(body)
                try:
                    if self.command == "HEAD" or res.status in (204, 304):
                        self._start(res, chunked=False)
                    else:
                        self._pipe(res)
                finally:
                    conn.close()
            except OSError as e:
                if isinstance(e, (BrokenPipeError, ConnectionResetError)) or self._started:
                    return  # 割り込みなどでクライアントが切った
                msg = json.dumps({"error": {"message": f"Ollama ({upstream}) に接続できません: {e}"}}, ensure_ascii=False).encode()
                try:
                    self.send_response(502)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(msg)))
                    self.end_headers()
                    self.wfile.write(msg)
                except OSError:
                    pass

        do_GET = do_POST = do_DELETE = do_PUT = do_HEAD = _forward

    return Handler


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="頭脳の中継（Ollama の前に置く）")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=11434)
    p.add_argument("--ollama", default="http://127.0.0.1:11436", help="本物の Ollama の URL")
    p.add_argument("--model", default=None, help="使うモデルをこの名前にすり替える")
    p.add_argument("--search", choices=("auto", "explicit", "off"), default="off",
                   help="Web 検索: auto=「調べて」と最新情報が要る話題 / explicit=「調べて」と言われたときだけ / off")
    p.add_argument("--search-filler", default="", help="調べ始めるときに先に言う一言（空なら言わない）")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    server = ThreadingHTTPServer((a.host, a.port), make_handler(a.ollama, a.model, a.search, a.search_filler))
    if a.model:
        threading.Thread(target=preload, args=(a.ollama, a.model), daemon=True).start()
    log.info("頭脳の中継: http://%s:%d  →  %s（モデル: %s / 検索: %s）", a.host, a.port, a.ollama, a.model or "そのまま", a.search)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    sys.exit(main())
