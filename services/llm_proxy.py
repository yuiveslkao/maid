"""Ollama の前に置く小さな中継。Open-LLM-VTuber からのリクエストに、速く答えるための設定を足す。

- 「考えてから答える」モード（thinking）を切る（reasoning_effort: "none"）。
  Ollama は考えている部分を本文とは別に送るので、Open-LLM-VTuber からは見えないまま
  数秒待たされていた。
- サンプリングの既定値（top_p）を足す。リクエストに既に値があればそちらを優先する。
- --model を指定すると、使うモデルをすり替える（conf.yaml を書き換えずにモデルを切り替えられる）。
- 同じ言葉の繰り返し（無限ループ）を見つけたら打ち切る。返事の長さにも上限を付ける。
- 今の日時を伝える。--search を付けると、必要なときに Web 検索して結果を差し込む（services/web_search.py）。

それ以外のリクエスト（/api/... など）はそのまま Ollama に渡す。標準ライブラリだけで動く。

    python services/llm_proxy.py --port 11434 --ollama http://127.0.0.1:11436
"""

from __future__ import annotations

import argparse
import http.client
import json
import logging
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import web_search  # noqa: E402  （検索を使わないときは中の関数を呼ばないので、追加パッケージは不要）

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


def sse_text(text: str) -> bytes:
    """返事の一部として流す SSE イベント（「ちょっと調べるね。」や出典の行）。"""
    ev = {"id": "maid", "object": "chat.completion.chunk", "created": int(time.time()), "model": "maid",
          "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}]}
    return b"data: " + json.dumps(ev, ensure_ascii=False).encode("utf-8") + b"\n\n"


def last_user_text(messages: list) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, list):  # [{"type": "text", "text": ...}] の形
                return "".join(p.get("text", "") for p in c if isinstance(p, dict))
            return c if isinstance(c, str) else ""
    return ""


def insert_before_last_user(messages: list, text: str) -> list:
    out = list(messages)
    for k in range(len(out) - 1, -1, -1):
        if out[k].get("role") == "user":
            out.insert(k, {"role": "system", "content": text})
            return out
    return out + [{"role": "system", "content": text}]


WEEKDAYS = "月火水木金土日"
SOURCES_RE = re.compile(r"\n?\[出典: [^\]]*\]")
SEARCH_NOTE = ("（補足）Web 検索の結果が渡されたときは、それを使って最新の情報を答えてよい。"
               "結果が渡されていないのに、天気・ニュース・日付などの最新情報を推測で答えない。")
QUERY_PROMPT = ("次の発言について、Web 検索に使う短い日本語の検索語を 1 行だけ出力してください。"
                "説明や記号は付けない。会話相手の名前や家族の情報など、個人的な情報は入れない。"
                "検索の必要がなければ「なし」とだけ出力する。")


def add_note(messages: list, note: str) -> list:
    """最初の system メッセージの末尾に一言足す（無ければ先頭に足す）。"""
    out = list(messages)
    for k, m in enumerate(out):
        if m.get("role") == "system" and isinstance(m.get("content"), str):
            out[k] = {**m, "content": m["content"].rstrip() + "\n\n" + note}
            return out
    return [{"role": "system", "content": note}] + out


def make_handler(upstream: str, think: bool, top_p: float | None, model: str | None = None,
                 defaults: dict | None = None, search_mode: str = "off", search_filler: str = "",
                 search_pages: int = 3, search_fn=None):
    up = urllib.parse.urlparse(upstream)
    # reasoning_effort を付けると何も返さなくなるモデル（考えるモードの無いロールプレイ用モデルなど）
    no_effort_models: set[str] = set()
    # 直前の検索結果（「それっていつ？」のような続きの質問に使う）
    last_search: dict = {"ctx": None, "turns": 0}
    if search_fn is None and search_mode != "off":
        search_fn = web_search.search

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            log.debug("%s - %s", self.address_string(), fmt % args)

        # ---------------- 送受信の部品
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
            while True:
                chunk = res.read1(65536) if hasattr(res, "read1") else res.read(65536)
                if not chunk:
                    break
                self._chunk(chunk)
            self._end()

        # ---------------- 検索
        def _make_query(self, req: dict, text: str) -> str | None:
            """頭脳に短い検索語を作らせる。作れなければ発言から作る。「なし」なら None。"""
            name = str(req.get("model", ""))
            q = {"model": name, "stream": False, "max_tokens": 40, "temperature": 0.2,
                 "messages": [{"role": "system", "content": QUERY_PROMPT}, {"role": "user", "content": text}]}
            if not think and name not in no_effort_models:
                q["reasoning_effort"] = "none"
            try:
                conn, res = self._open(json.dumps(q, ensure_ascii=False).encode("utf-8"), "/v1/chat/completions", "POST")
                try:
                    data = json.loads(res.read()) if res.status == 200 else {}
                finally:
                    conn.close()
                line = (_content(data) or "").strip().splitlines()[0].strip(" 「」\"'`") if _content(data).strip() else ""
            except (OSError, ValueError, IndexError):
                line = ""
            if line in ("なし", "無し", "NONE", "none"):
                return None
            return line[:60] or web_search.fallback_query(text)

        def _augment(self, req: dict) -> tuple[dict, bytes]:
            """検索が要るなら調べて結果を差し込む。戻り値は (新しいリクエスト, 返事の最後に足す出典)。"""
            # 過去の返事に付けた出典は頭脳に見せない（ふだんの返事でも真似して書き始めるため）
            msgs = [{**m, "content": SOURCES_RE.sub("", m["content"])}
                    if m.get("role") == "assistant" and isinstance(m.get("content"), str) else m
                    for m in (req.get("messages") or [])]
            now = datetime.now()
            note = f"現在の日時: {now:%Y年%m月%d日}（{WEEKDAYS[now.weekday()]}）{now:%H:%M}"
            if search_mode != "off":
                note += "\n" + SEARCH_NOTE
            msgs = add_note(msgs, note)
            suffix = b""
            text = last_user_text(msgs)
            if search_mode != "off" and req.get("stream") and web_search.needs_search(text, search_mode):
                explicit = web_search.needs_search(text, "explicit")
                if search_filler:
                    self._start()
                    self._chunk(sse_text(search_filler))  # 調べている間に読み上げが始まる
                query = self._make_query(req, text)
                if query is None and explicit:
                    query = web_search.fallback_query(text)
                if query:
                    ctx = search_fn(query, search_pages)
                    log.info("検索「%s」→ %s", query, ", ".join(r.site for r in ctx.results) or "見つからず")
                    msgs = insert_before_last_user(msgs, web_search.build_context(ctx))
                    last_search.update(ctx=ctx, turns=0)
                    line = web_search.sources_line(ctx)
                    suffix = sse_text(line) if line else b""
                    return {**req, "messages": msgs}, suffix
            ctx = last_search["ctx"]
            if ctx is not None and last_search["turns"] < 2 and (now - ctx.when).total_seconds() < 600:
                last_search["turns"] += 1
                msgs = insert_before_last_user(msgs, web_search.build_context(ctx, followup=True))
            return {**req, "messages": msgs}, suffix

        # ---------------- チャット
        def _try_chat(self, body: bytes, injected: bool, stream: bool, suffix: bytes) -> bool:
            """チャットを 1 回試す。reasoning_effort を付けたせいで空っぽだったら False（まだ中身を返していない）。"""
            conn, res = self._open(body)
            try:
                if res.status != 200 or (not injected and not stream):
                    if self._started:  # 「ちょっと調べるね。」を送ったあとに失敗した
                        self._chunk(STOP_EVENT[1:])
                        self._end()
                        return True
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
                    self._end()
                    return True
                return self._stream(res, injected, suffix)
            finally:
                conn.close()

        def _stream(self, res, injected: bool, suffix: bytes = b"") -> bool:
            """SSE を 1 イベントずつ中継する。
            - injected のときは、中身のある最初のイベントまで溜めておく（空っぽなら False を返して聞き直す）
            - 繰り返し（無限ループ）を見つけたら、そこで返事を終わらせる（Ollama 側の生成も接続を切って止める）
            - suffix（出典など）は返事の最後、[DONE] の直前に足す"""
            pending: list[bytes] = []
            answered = not injected
            text = ""

            def flush():
                nonlocal pending
                self._start(res)
                if pending:
                    self._chunk(b"".join(pending))
                    pending = []

            while True:
                line = res.readline()
                if not line:
                    break
                s = line.strip()
                if s == b"data: [DONE]":
                    if not answered:
                        return False
                    flush()
                    self._chunk(suffix + line)
                    suffix = b""
                    continue
                if s.startswith(b"data:"):
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
                        self._chunk(b"\n" + suffix + STOP_EVENT[1:])
                        self._end()
                        return True
                pending.append(line)
                if answered and not s:  # イベントの区切り（空行）ごとに流す
                    flush()
            if not answered:
                return False
            flush()
            self._chunk(suffix)
            self._end()
            return True

        def _forward(self):
            self._started = False
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
                    suffix = b""
                    if isinstance(req, dict) and isinstance(req.get("messages"), list):
                        req, suffix = self._augment(req)
                        body = json.dumps(req, ensure_ascii=False).encode("utf-8")
                    name = str(req.get("model", ""))
                    stream = bool(req.get("stream"))
                    use_effort = not think and name not in no_effort_models
                    first = rewrite_chat_request(body, not use_effort, top_p, defaults)
                    injected = use_effort and b"reasoning_effort" in first and "reasoning_effort" not in req
                    if self._try_chat(first, injected, stream, suffix):
                        return
                    # 「考えるモードなし」の指定を外してやり直す。次からこのモデルには付けない
                    log.warning("%s は考えるモードの指定を付けると返事が空になるので、指定を外して聞き直します", name)
                    no_effort_models.add(name)
                    self._try_chat(rewrite_chat_request(body, True, top_p, defaults), False, stream, suffix)
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
                if self._started:
                    return
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
    p.add_argument("--search", choices=("auto", "explicit", "off"), default="off",
                   help="Web 検索: auto=「調べて」と最新情報が要る話題 / explicit=「調べて」と言われたときだけ / off")
    p.add_argument("--search-filler", default="ちょっと調べるね。", help="調べ始めるときに先に言う一言（空なら言わない）")
    p.add_argument("--search-pages", type=int, default=3, help="本文を読むページ数")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    server = ThreadingHTTPServer((a.host, a.port), make_handler(
        a.ollama, a.think, a.top_p, a.model, {"max_tokens": a.max_tokens, "presence_penalty": a.presence_penalty or None},
        search_mode=a.search, search_filler=a.search_filler, search_pages=a.search_pages))
    log.info("頭脳の中継: http://%s:%d  →  %s（考えるモード: %s / モデル: %s / 検索: %s）", a.host, a.port, a.ollama,
             "ON" if a.think else "OFF", a.model or "conf.yaml のまま", a.search)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    sys.exit(main())
