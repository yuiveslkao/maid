"""チャットだけの版（app/chat.py・app/common.py）のテスト。頭脳の中継と声の中継は偽物。"""

import base64
import json
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import chat  # noqa: E402
from common import Icons, Settings  # noqa: E402


class FakeServers(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    chats: list = []
    speeches: list = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/v1/audio/speech":
            FakeServers.speeches.append(body["input"])
            out = b"RIFF" + body["input"].encode()
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        FakeServers.chats.append(body)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        for t in ["ちょっと調べるね。", "晴れだって！", "<think>出典: https://tenki.example/a。</think>"]:
            self.wfile.write(f"data: {json.dumps({'choices': [{'delta': {'content': t}}]}, ensure_ascii=False)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.close_connection = True


class ChatTest(unittest.TestCase):
    def setUp(self):
        FakeServers.chats.clear()
        FakeServers.speeches.clear()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeServers)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.srv.shutdown()
        self.tmp.cleanup()

    def test_load_character(self):
        conf = self.dir / "conf.yaml"
        conf.write_text("character_config:\n  character_name: 'めい'\n  human_name: 'ぱぱ'\n  persona_prompt: |\n    あなたはめい。\n",
                        encoding="utf-8")
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("pyyaml なし")
        self.assertEqual(chat.load_character(conf), {"name": "めい", "human": "ぱぱ", "persona": "あなたはめい。\n"})
        self.assertEqual(chat.load_character(self.dir / "none.yaml")["name"], "メイド")

    def test_send_streams_and_keeps_history(self):
        session = chat.ChatSession(self.url, {"name": "めい", "human": "ぱぱ", "persona": "あなたはめい。"}, self.dir / "chat.json")
        pieces = []
        reply = session.send("天気は？", pieces.append)
        self.assertEqual(reply, "ちょっと調べるね。晴れだって！<think>出典: https://tenki.example/a。</think>")
        self.assertEqual("".join(pieces), reply)
        sent = FakeServers.chats[-1]["messages"]
        self.assertEqual(sent[0], {"role": "system", "content": "あなたはめい。"})
        self.assertEqual(sent[-1], {"role": "user", "content": "天気は？"})
        # 次に開いたときも続きから
        again = chat.ChatSession(self.url, session.character, self.dir / "chat.json")
        self.assertEqual([m["role"] for m in again.history], ["user", "assistant"])

    def test_proactive_is_not_kept_in_history(self):
        session = chat.ChatSession(self.url, {"name": "めい", "human": "ぱぱ", "persona": "p"}, self.dir / "chat.json")
        session.send("", lambda t: None, proactive=True)
        self.assertEqual(FakeServers.chats[-1]["messages"][-1]["content"], chat.PROACTIVE_MARK)
        self.assertEqual(session.history, [])  # 話しかけた内容は頭脳の中継が覚えていて、次の返事のときに足す

    def test_proxy_down(self):
        session = chat.ChatSession("http://127.0.0.1:1", {"name": "め", "human": "ぱ", "persona": "p"})
        self.assertIn("頭脳につながりませんでした", session.send("やあ", lambda t: None))

    def test_proactive_loop(self):
        session = chat.ChatSession(self.url, {"name": "め", "human": "ぱ", "persona": "p"})
        calls, stop = [], threading.Event()
        session.last_activity -= 120
        t = threading.Thread(target=chat.proactive_loop, args=(session, 1, lambda: calls.append(1), lambda: 10, stop),
                             kwargs={"poll": 0.02})
        t.start()
        time.sleep(0.1)
        stop.set()
        t.join()
        self.assertEqual(calls, [1])  # 1 回話しかけたら、また 1 分待つ

    def test_speaker_reads_sentences_without_sources(self):
        played = []
        sp = chat.Speaker(self.url, played.append)
        for piece in ["ちょっと調べるね。", "晴れ", "だって！", "<think>出典: https://x.example/。</think>"]:
            sp.feed(piece)
        sp.feed("", final=True)
        for _ in range(100):
            if len(played) >= 2:
                break
            time.sleep(0.02)
        self.assertEqual(FakeServers.speeches, ["ちょっと調べるね。", "晴れだって！"])
        self.assertEqual([base64.b64decode(p)[4:].decode() for p in played], ["ちょっと調べるね。", "晴れだって！"])
        self.assertEqual(chat.spoken("うん（にこっ）[joy] *えへへ*"), "うん")


class CommonTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_settings_update_keeps_other_keys(self):
        (self.dir / "default.json").write_text('{"mode": "chat", "search": "auto", "memory": true}', encoding="utf-8")
        (self.dir / "user.json").write_text('{"model": "x", "search": "off"}', encoding="utf-8")
        s = Settings(self.dir / "user.json", self.dir / "default.json")
        self.assertEqual(s.all(), {"mode": "chat", "search": "off", "memory": True, "model": "x"})
        s.update({"mode": "full"})
        self.assertEqual(json.loads((self.dir / "user.json").read_text(encoding="utf-8")), {"model": "x", "search": "off", "mode": "full"})

    def test_icons(self):
        icons = Icons(self.dir / "icons")
        png = "data:image/png;base64," + base64.b64encode(b"\x89PNG fake").decode()
        self.assertEqual(icons.save("character", png), png)
        jpg = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8 fake").decode()
        icons.save("character", jpg)  # 差し替えると前のは消える
        self.assertEqual(sorted(p.name for p in (self.dir / "icons").iterdir()), ["character.jpg"])
        self.assertIsNone(icons.data_url("user"))
        with self.assertRaises(ValueError):
            icons.save("character", "data:text/plain;base64,aGk=")
        icons.remove("character")
        self.assertIsNone(icons.find("character"))


if __name__ == "__main__":
    unittest.main()
