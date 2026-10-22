"""会話（app/chat.py・app/common.py・app/screen.py）のテスト。頭脳の中継と声の中継は偽物。"""

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
import common  # noqa: E402
import screen  # noqa: E402
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

    def test_images_are_sent_but_not_kept(self):
        session = chat.ChatSession(self.url, {"name": "め", "human": "ぱ", "persona": "p"}, self.dir / "chat.json")
        session.send("これ何？", lambda t: None, images=["data:image/jpeg;base64,AAAA"])
        last = FakeServers.chats[-1]["messages"][-1]["content"]
        self.assertEqual(last[0], {"type": "text", "text": "これ何？"})
        self.assertEqual(last[1], {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}})
        self.assertEqual(session.history[0], {"role": "user", "content": "これ何？", "images": 1})
        self.assertNotIn("AAAA", (self.dir / "chat.json").read_text(encoding="utf-8"))
        # 次の発言では、画像を見せたことだけを伝える
        session.send("ありがとう", lambda t: None)
        self.assertEqual(FakeServers.chats[-1]["messages"][1], {"role": "user", "content": "これ何？\n（このとき画像を 1 枚見せた）"})

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

    def test_proactive_loop_waits_during_discord_call(self):
        session = chat.ChatSession(self.url, {"name": "め", "human": "ぱ", "persona": "p"})
        calls, stop = [], threading.Event()
        session.last_activity -= 120
        t = threading.Thread(target=chat.proactive_loop, args=(session, 1, lambda: calls.append(1), lambda: 10, stop),
                             kwargs={"poll": 0.02, "blocked": lambda: True})
        t.start()
        time.sleep(0.1)
        stop.set()
        t.join()
        self.assertEqual(calls, [])

    def test_speaker_reads_sentences_without_sources(self):
        played = []
        sp = chat.Speaker(self.url, lambda b64, part: played.append((b64, part)))
        for piece in ["ちょっと調べるね。", "晴れ", "だって！", "？", "<think>出典: https://x.example/。</think>"]:
            sp.feed(piece)
        sp.feed("", final=True)
        sp.finish()
        for _ in range(100):
            if played and played[-1] == (None, None):
                break
            time.sleep(0.02)
        self.assertEqual(FakeServers.speeches, ["ちょっと調べるね。", "晴れだって！？"])
        # 文ごとに、声と画面に出す文が組になって届く。出典は声なしで、最後に返事の終わり
        self.assertEqual([(base64.b64decode(b)[4:].decode() if b else None, part) for b, part in played],
                         [("ちょっと調べるね。", "ちょっと調べるね。"), ("晴れだって！？", "晴れだって！？"),
                          (None, "<think>出典: https://x.example/。</think>"), (None, None)])
        self.assertEqual(chat.spoken("うん（にこっ）[joy] *えへへ*"), "うん")

    def test_speaker_reset_drops_the_rest(self):
        played, gate = [], threading.Event()
        sp = chat.Speaker(self.url, lambda b64, part: played.append(part))
        sp.synthesize = lambda text: (gate.wait(2), b"RIFF")[1]
        sp.feed("いち。にい。さん。")
        time.sleep(0.05)
        sp.reset()  # 割り込まれた
        sp.finish()
        gate.set()
        for _ in range(100):
            if None in played:
                break
            time.sleep(0.02)
        self.assertEqual(played, [None])


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

    def test_character_moves_from_old_conf(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("pyyaml なし")
        old = self.dir / "conf.yaml"
        old.write_text("character_config:\n  character_name: 'めい'\n  human_name: 'ぱぱ'\n  persona_prompt: |\n"
                       "    あなたはめい。\n\n    - 短く話す: とても大事\n", encoding="utf-8")
        path = self.dir / "character.yaml"
        common.ensure_character(path, old_conf=old)
        self.assertEqual(yaml.safe_load(path.read_text(encoding="utf-8")),
                         {"name": "めい", "human": "ぱぱ", "persona": "あなたはめい。\n\n- 短く話す: とても大事\n"})
        self.assertEqual(common.load_character(path)["persona"], "あなたはめい。\n\n- 短く話す: とても大事")
        # 既にあれば触らない
        path.write_text("name: め\n", encoding="utf-8")
        common.ensure_character(path, old_conf=old)
        self.assertEqual(common.load_character(path)["name"], "め")

    def test_character_default(self):
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("pyyaml なし")
        path = self.dir / "character.yaml"
        common.ensure_character(path, old_conf=self.dir / "none.yaml")
        c = common.load_character(path)
        self.assertEqual((c["name"], c["human"]), ("メイド", "ご主人様"))
        self.assertIn("話し方のルール", c["persona"])

    def test_wants_look(self):
        for t in ("画面見て", "これわかる？", "この文読んで", "今の見える？"):
            self.assertTrue(screen.wants_look(t), t)
        for t in ("おはよう", "明日の天気調べて", "見てたアニメの話"):
            self.assertFalse(screen.wants_look(t), t)

    def test_shrink(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow なし")
        import base64
        import io

        buf = io.BytesIO()
        Image.new("RGBA", (3000, 1500), (255, 0, 0, 128)).save(buf, "PNG")
        out = screen.shrink("data:image/png;base64," + base64.b64encode(buf.getvalue()).decode())
        self.assertTrue(out.startswith("data:image/jpeg;base64,"))
        img = Image.open(io.BytesIO(base64.b64decode(out.split(",", 1)[1])))
        self.assertEqual(img.size, (1280, 640))

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
