"""Ollama の前に置く小さな中継。Open-LLM-VTuber からのリクエストに、速く答えるための設定を足す。

- 「考えてから答える」モード（thinking）を切る（reasoning_effort: "none"）。
  Ollama は考えている部分を本文とは別に送るので、Open-LLM-VTuber からは見えないまま
  数秒待たされていた。
- サンプリングの既定値（top_p）を足す。リクエストに既に値があればそちらを優先する。
- --model を指定すると、使うモデルをすり替える（conf.yaml を書き換えずにモデルを切り替えられる）。
- 同じ言葉の繰り返し（無限ループ）を見つけたら打ち切る。返事の長さにも上限を付ける。

それ以外のリクエスト（/api/... など）はそのまま Ollama に渡す。標準ライブラリだけで動く。

    python bridge/llm_proxy.py --port 11434 --ollama http://127.0.0.1:11436
"""

from __future__ import annotations

import argparse
import http.client
import json
import logging
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("llm_proxy")

HOP_HEADERS = {"connection", "keep-alive", "transfer-encoding", "content-length", "host", "proxy-connection", "upgrade"}


def rewrite_model(body: bytes, model: str | None) -> bytes:
    if not model:
        return body
    try:
        req = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return body
    if not isinstance(req, dict) or "model" not in req:
        return body
    req["model"] = model
    return json.dumps(req, ensure_ascii=False).encode("utf-8")


# 返事の上限と繰り返しの抑制（リクエストに値があればそちらを優先）。make_handler で上書きできる
DEFAULTS = {"max_tokens": 300, "presence_penalty": 0.5}


def rewrite_chat_request(body: bytes, think: bool, top_p: float | None, defaults: dict | None = None) -> bytes:
    try:
        req = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return body
    if not isinstance(req, dict):
        return body
    if not think and "reasoning_effort" not in req and "reasoning" not in req:
        req["reasoning_effort"] = "none"
    if top_p is not None and req.get("top_p") is None:
        req["top_p"] = top_p
    for k, v in (DEFAULTS if defaults is None else defaults).items():
        if v is not None and req.get(k) is None:
            req[k] = v
    return json.dumps(req, ensure_ascii=False).encode("utf-8")


def is_looping(text: str) -> bool:
    """末尾が同じ並びの繰り返しになっていたら True（例: 「ぱぱ、ぱぱ、ぱぱ、ぱぱ、」）。"""
    t = text[-400:]
    for n in range(1, 81):
        reps = 8 if n <= 4 else (4 if n <= 20 else 3)
        if len(t) >= n * reps and t[-n * reps:] == t[-n:] * reps:
            return True
    return False


def _content(event: dict) -> str:
    out = []
    for c in event.get("choices") or []:
        part = c.get("delta") or c.get("message") or {}
        if isinstance(part.get("content"), str):
            out.append(part["content"])
    return "".join(out)


def _has_answer(event: dict) -> bool:
    """OpenAI 形式の応答（ストリームの 1 イベント、または非ストリームの全体）に中身があるか。"""
    for c in event.get("choices") or []:
        part = c.get("delta") or c.get("message") or {}
        if part.get("content") or part.get("tool_calls") or part.get("reasoning"):
            return True
    return False


STOP_EVENT = b'\ndata: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'


def make_handler(upstream: str, think: bool, top_p: float | None, model: str | None = None,
                 defaults: dict | None = None):
    up = urllib.parse.urlparse(upstream)
    # reasoning_effort を付けると何も返さなくなるモデル（考えるモードの無いロールプレイ用モデルなど）
    no_effort_models: set[str] = set()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            log.debug("%s - %s", self.address_string(), fmt % args)

        def _open(self, body: bytes):
            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_HEADERS}
            if body or self.command in ("POST", "PUT", "PATCH"):
                headers["Content-Length"] = str(len(body))
            conn = http.client.HTTPConnection(up.hostname, up.port or 80, timeout=600)
            conn.request(self.command, self.path, body=body or None, headers=headers)
            return conn, conn.getresponse()

        def _start(self, res, chunked: bool = True):
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

        def _pipe(self, res):
            """残りをそのまま流す（ストリーミングを保つため chunked で中継する）"""
            while True:
                chunk = res.read1(65536) if hasattr(res, "read1") else res.read(65536)
                if not chunk:
                    break
                self._chunk(chunk)
            self.wfile.write(b"0\r\n\r\n")

        def _try_chat(self, body: bytes, injected: bool, model_name: str, stream: bool) -> bool:
            """チャットを 1 回試す。reasoning_effort を付けたせいで空っぽだったら False（まだ何も返していない）。"""
            conn, res = self._open(body)
            try:
                if res.status != 200 or (not injected and not stream):
                    self._start(res)
                    self._pipe(res)
                    return True
                if not stream:
                    data = res.read()
                    try:
                        empty = not _has_answer(json.loads(data))
                    except ValueError:
                        empty = False
                    if empty:
                        return False
                    self._start(res)
                    self._chunk(data)
                    self.wfile.write(b"0\r\n\r\n")
                    return True
                return self._stream(res, injected)
            finally:
                conn.close()

        def _stream(self, res, injected: bool) -> bool:
            """SSE を 1 イベントずつ中継する。
            - injected のときは、中身のある最初のイベントまで溜めておく（空っぽなら False を返して聞き直す）
            - 繰り返し（無限ループ）を見つけたら、そこで返事を終わらせる（Ollama 側の生成も接続を切って止める）"""
            pending: list[bytes] = []
            answered = not injected
            text = ""
            started = False

            def flush():
                nonlocal pending, started
                if not started:
                    self._start(res)
                    started = True
                if pending:
                    self._chunk(b"".join(pending))
                    pending = []

            while True:
                line = res.readline()
                if not line:
                    break
                s = line.strip()
                if s == b"data: [DONE]" and not answered:
                    return False
                if s.startswith(b"data:") and s != b"data: [DONE]":
                    try:
                        ev = json.loads(s[5:])
                    except ValueError:
                        ev = {}
                    if _has_answer(ev):
                        answered = True
                    text += _content(ev)
                    if is_looping(text):
                        log.warning("同じ言葉の繰り返しを見つけたので返事を打ち切りました: …%s", text[-40:])
                        pending.append(line)
                        flush()
                        self._chunk(STOP_EVENT)
                        self.wfile.write(b"0\r\n\r\n")
                        return True
                pending.append(line)
                if answered and not s:  # イベントの区切り（空行）ごとに流す
                    flush()
            if not answered:
                return False
            flush()
            self.wfile.write(b"0\r\n\r\n")
            return True

        def _forward(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            path = urllib.parse.urlparse(self.path).path.rstrip("/")
            if self.command == "POST" and path in ("/v1/chat/completions", "/api/chat", "/api/generate"):
                body = rewrite_model(body, model)
            try:
                if self.command == "POST" and path.endswith("/chat/completions"):
                    try:
                        req = json.loads(body)
                    except ValueError:
                        req = {}
                    name = str(req.get("model", ""))
                    use_effort = not think and name not in no_effort_models
                    first = rewrite_chat_request(body, not use_effort, top_p, defaults)
                    injected = use_effort and b"reasoning_effort" in first and "reasoning_effort" not in req
                    if self._try_chat(first, injected, name, bool(req.get("stream"))):
                        return
                    # 「考えるモードなし」の指定を外してやり直す。次からこのモデルには付けない
                    log.warning("%s は考えるモードの指定を付けると返事が空になるので、指定を外して聞き直します", name)
                    no_effort_models.add(name)
                    self._try_chat(rewrite_chat_request(body, True, top_p, defaults), False, name, bool(req.get("stream")))
                    return
                conn, res = self._open(body)
                try:
                    if self.command == "HEAD" or res.status in (204, 304):
                        self._start(res, chunked=False)
                        return
                    self._start(res)
                    self._pipe(res)
                finally:
                    conn.close()
            except OSError as e:
                if isinstance(e, (BrokenPipeError, ConnectionResetError)):
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
    p = argparse.ArgumentParser(description="Ollama 用の低遅延中継")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=11434)
    p.add_argument("--ollama", default="http://127.0.0.1:11436", help="本物の Ollama の URL")
    p.add_argument("--think", action="store_true", help="考えるモードを切らない")
    p.add_argument("--top-p", type=float, default=0.8, help="リクエストに top_p が無いときの値（Qwen3 の推奨値）")
    p.add_argument("--model", default=None, help="使うモデルをこの名前にすり替える")
    p.add_argument("--max-tokens", type=int, default=DEFAULTS["max_tokens"], help="返事の長さの上限（トークン数）")
    p.add_argument("--presence-penalty", type=float, default=DEFAULTS["presence_penalty"], help="同じ言葉を繰り返しにくくする強さ (0 で無効)")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    server = ThreadingHTTPServer((a.host, a.port), make_handler(a.ollama, a.think, a.top_p, a.model,
                                                                 {"max_tokens": a.max_tokens, "presence_penalty": a.presence_penalty or None}))
    log.info("頭脳の中継: http://%s:%d  →  %s（考えるモード: %s / モデル: %s）", a.host, a.port, a.ollama,
             "ON" if a.think else "OFF", a.model or "conf.yaml のまま")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    sys.exit(main())
