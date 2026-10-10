"""Ollama の前に置く小さな中継。Open-LLM-VTuber からのリクエストに、速く答えるための設定を足す。

- 「考えてから答える」モード（thinking）を切る（reasoning_effort: "none"）。
  Ollama は考えている部分を本文とは別に送るので、Open-LLM-VTuber からは見えないまま
  数秒待たされていた。
- サンプリングの既定値（top_p）を足す。リクエストに既に値があればそちらを優先する。
- --model を指定すると、使うモデルをすり替える（conf.yaml を書き換えずにモデルを切り替えられる）。

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


def rewrite_chat_request(body: bytes, think: bool, top_p: float | None) -> bytes:
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
    return json.dumps(req, ensure_ascii=False).encode("utf-8")


def make_handler(upstream: str, think: bool, top_p: float | None, model: str | None = None):
    up = urllib.parse.urlparse(upstream)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            log.debug("%s - %s", self.address_string(), fmt % args)

        def _forward(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            path = urllib.parse.urlparse(self.path).path
            if self.command == "POST" and path.rstrip("/").endswith("/chat/completions"):
                body = rewrite_chat_request(body, think, top_p)
            if self.command == "POST" and path.rstrip("/") in ("/v1/chat/completions", "/api/chat", "/api/generate"):
                body = rewrite_model(body, model)

            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_HEADERS}
            if body or self.command in ("POST", "PUT", "PATCH"):
                headers["Content-Length"] = str(len(body))
            conn = http.client.HTTPConnection(up.hostname, up.port or 80, timeout=600)
            try:
                conn.request(self.command, self.path, body=body or None, headers=headers)
                res = conn.getresponse()
            except OSError as e:
                msg = json.dumps({"error": {"message": f"Ollama ({upstream}) に接続できません: {e}"}}, ensure_ascii=False).encode()
                self.send_response(502)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)
                return

            self.send_response(res.status, res.reason)
            for k, v in res.getheaders():
                if k.lower() not in HOP_HEADERS:
                    self.send_header(k, v)
            if self.command == "HEAD" or res.status in (204, 304):
                self.send_header("Content-Length", "0")
                self.end_headers()
                conn.close()
                return
            # ストリーミングをそのまま流すため、chunked で中継する
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                while True:
                    chunk = res.read1(65536) if hasattr(res, "read1") else res.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass  # 割り込みなどでクライアントが切った
            finally:
                conn.close()

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
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    server = ThreadingHTTPServer((a.host, a.port), make_handler(a.ollama, a.think, a.top_p, a.model))
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
