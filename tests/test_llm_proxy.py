"""頭脳の中継のテスト。偽の Ollama を立てて、設定が足されるか・ストリーミングが流れるかを確かめる。"""

import json
import sys
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bridge"))
import llm_proxy  # noqa: E402


class FakeOllama(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    requests: list = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeOllama.requests.append((self.path, body))
        if self.path == "/v1/chat/completions":
            # 本物と同じく、少しずつ SSE で返す（長さ不明・接続を閉じて終わり）
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            for t in ["うん", "、", "いいよ"]:
                self.wfile.write(f"data: {json.dumps({'choices': [{'delta': {'content': t}}]})}\n\n".encode())
                self.wfile.flush()
                time.sleep(0.05)
            self.wfile.write(b"data: [DONE]\n\n")
            self.close_connection = True
        else:
            out = json.dumps({"ok": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    def do_GET(self):
        out = json.dumps({"models": [{"name": "qwen3:8b"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class ProxyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ollama = serve(FakeOllama)
        cls.proxy = serve(llm_proxy.make_handler(f"http://127.0.0.1:{cls.ollama.server_port}", think=False, top_p=0.8))
        cls.base = f"http://127.0.0.1:{cls.proxy.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.proxy.shutdown()
        cls.ollama.shutdown()

    def setUp(self):
        FakeOllama.requests.clear()

    def test_rewrite_rules(self):
        r = json.loads(llm_proxy.rewrite_chat_request(b'{"model":"m","top_p":0.5}', think=False, top_p=0.8))
        self.assertEqual((r["reasoning_effort"], r["top_p"]), ("none", 0.5))
        r = json.loads(llm_proxy.rewrite_chat_request(b'{"reasoning_effort":"high"}', think=False, top_p=None))
        self.assertEqual(r, {"reasoning_effort": "high"})
        r = json.loads(llm_proxy.rewrite_chat_request(b'{"model":"m"}', think=True, top_p=None))
        self.assertNotIn("reasoning_effort", r)
        self.assertEqual(llm_proxy.rewrite_chat_request(b"not json", False, 0.8), b"not json")

    def test_streaming_with_openai_client(self):
        """Open-LLM-VTuber と同じ openai クライアントでストリーミングが届くか。"""
        try:
            from openai import OpenAI
        except ImportError:
            self.skipTest("openai パッケージなし")
        client = OpenAI(api_key="x", base_url=self.base + "/v1")
        stream = client.chat.completions.create(model="qwen3:8b", stream=True, temperature=0.8,
                                                messages=[{"role": "user", "content": "やあ"}])
        text = "".join((c.choices[0].delta.content or "") for c in stream)
        self.assertEqual(text, "うん、いいよ")
        path, body = FakeOllama.requests[0]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual((body["reasoning_effort"], body["top_p"], body["temperature"]), ("none", 0.8, 0.8))

    def test_other_paths_pass_through(self):
        req = urllib.request.Request(self.base + "/api/chat", data=json.dumps({"model": "m", "keep_alive": -1}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req) as res:
            self.assertEqual(json.loads(res.read()), {"ok": True})
        self.assertEqual(FakeOllama.requests[0], ("/api/chat", {"model": "m", "keep_alive": -1}))
        with urllib.request.urlopen(self.base + "/api/tags") as res:
            self.assertEqual(json.loads(res.read())["models"][0]["name"], "qwen3:8b")

    def test_model_override(self):
        proxy = serve(llm_proxy.make_handler(f"http://127.0.0.1:{self.ollama.server_port}", think=False, top_p=None, model="other:9b"))
        try:
            for path in ("/api/chat", "/v1/chat/completions"):
                req = urllib.request.Request(f"http://127.0.0.1:{proxy.server_port}{path}", data=json.dumps({"model": "qwen3:8b"}).encode(),
                                             headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req) as res:
                    res.read()
            self.assertEqual([b["model"] for _, b in FakeOllama.requests], ["other:9b", "other:9b"])
        finally:
            proxy.shutdown()

    def test_upstream_down_gives_502(self):
        proxy = serve(llm_proxy.make_handler("http://127.0.0.1:1", think=False, top_p=None))
        try:
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(f"http://127.0.0.1:{proxy.server_port}/api/tags")
            self.assertEqual(cm.exception.code, 502)
        finally:
            proxy.shutdown()


if __name__ == "__main__":
    unittest.main()
