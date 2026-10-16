"""記憶（services/memory.py）と、頭脳の中継への組み込みのテスト。頭脳は偽物。"""

import json
import random
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "services"))
import llm_proxy  # noqa: E402
import memory as mem  # noqa: E402


class FakeBrain:
    """記憶係として呼ばれたときの偽の頭脳。呼ばれた内容を覚えておく。"""

    def __init__(self):
        self.calls = []

    def __call__(self, messages, max_tokens):
        system, user = messages[0]["content"], messages[-1]["content"]
        self.calls.append((system, user))
        if system == mem.EPISODE_PROMPT:
            return "相手は熱があると言っていた。キャラは心配した。"
        if system == mem.FACTS_PROMPT:
            facts = []
            if "熱があるんだ" in user:
                facts.append("状況: 熱がある")
            if "治った" in user:
                facts.append("済み: 熱がある")
            if "ゲームが好き" in user:
                facts.append("プロフィール: ゲームが好き")
            if "また今度ね" in user:
                facts.append("約束: 今度オムライスを作る")
            if "オムライス作ったよ" in user:
                facts.append("済み: オムライスを作る")
            if "ハンバーグは作るの大変" in user:
                facts.append("プロフィール: 手間のかかることは面倒くさがる")
            if "引っ越し" in user:
                facts.append("近況：引っ越しの準備中")  # 全角のコロンでも読める
            return "\n".join(f"- {x}" for x in facts) or "なし"
        # プロフィールの書き直し: 今の行に【足すこと】を足す
        old, _, change = user.partition("【足すこと】")
        add, _, _ = change.partition("【忘れること】")
        lines = [ln for ln in old.splitlines() if ln.startswith("- ")]
        return "\n".join(lines + [ln for ln in add.splitlines() if ln.startswith("- ")])


def fake_embed(texts):
    # 「熱」が入っていれば似ている、くらいの偽物
    return [[1.0, 0.0] if "熱" in t else [0.0, 1.0] for t in texts]


class MemoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.brain = FakeBrain()
        self.m = mem.Memory(Path(self.tmp.name), self.brain, fake_embed)
        self.t0 = datetime(2026, 10, 15, 20, 0)

    def tearDown(self):
        self.m.db.close()
        self.tmp.cleanup()

    def now_text(self):
        return self.m.read_note(self.m.now_md)

    def test_consolidates_after_idle_and_updates_state(self):
        self.m.record("熱があるんだ", "だいじょうぶ？", when=self.t0)
        self.m.record("", "おくすり飲んだ？", proactive=True, when=self.t0 + timedelta(minutes=5))
        self.assertFalse(self.m.maybe_consolidate(self.t0 + timedelta(minutes=20)))  # まだ 30 分たっていない
        self.assertTrue(self.m.maybe_consolidate(self.t0 + timedelta(minutes=40)))
        self.assertEqual(self.m.pending(), [])
        self.assertEqual(self.now_text(), "- [状況] 熱がある（2026-10-15 20:05）")
        self.assertEqual(len(self.m.episodes()), 1)
        # 会話の記録には、キャラから話しかけた分も「キャラ:」として入る
        talk = [u for s, u in self.brain.calls if s == mem.EPISODE_PROMPT][0]
        self.assertEqual(talk, "相手: 熱があるんだ\nキャラ: だいじょうぶ？\nキャラ: おくすり飲んだ？")
        # 治ったと聞いたら消える
        self.m.record("熱は治ったよ", "よかった！", when=self.t0 + timedelta(hours=5))
        self.m.consolidate()
        self.assertEqual(self.now_text(), "")

    def test_kinds_go_to_the_right_place(self):
        self.m.record("ゲームが好きなんだ", "そうなんだ！", when=self.t0)
        self.m.record("オムライスはまた今度ね", "うん！", when=self.t0)
        self.m.record("ハンバーグは作るの大変なんだよね", "そっかー", when=self.t0)
        self.m.record("最近引っ越しの準備で忙しい", "がんばって！", when=self.t0)
        self.m.consolidate()
        self.assertEqual(self.m.read_note(self.m.profile_md), "- ゲームが好き\n- 手間のかかることは面倒くさがる")
        self.assertEqual(self.now_text(), "- [約束] 今度オムライスを作る（2026-10-15 20:00）\n- [近況] 引っ越しの準備中（2026-10-15 20:00）")
        # 約束が果たされたら消える
        self.m.record("オムライス作ったよ", "おいしい！", when=self.t0 + timedelta(days=1))
        self.m.consolidate()
        self.assertEqual(self.now_text(), "- [近況] 引っ越しの準備中（2026-10-15 20:00）")

    def test_now_expires(self):
        self.m.write_now([("状況", "夜ごはんはアジフライ", self.t0), ("約束", "今度オムライス", self.t0),
                          ("近況", "引っ越しの準備中", self.t0), ("メモ", "手で書いた行", None)])
        texts = lambda days: [t for _, t, _ in self.m.now_items(self.t0 + timedelta(days=days))]  # noqa: E731
        self.assertEqual(texts(0.5), ["夜ごはんはアジフライ", "今度オムライス", "引っ越しの準備中", "手で書いた行"])
        self.assertEqual(texts(2), ["今度オムライス", "引っ越しの準備中", "手で書いた行"])
        self.assertEqual(texts(10), ["引っ越しの準備中", "手で書いた行"])
        self.assertEqual(texts(40), ["手で書いた行"])

    def test_character_wishes_are_not_kept(self):
        self.m.record("何が食べたい？", "ハンバーグがいいな", when=self.t0)
        self.m.consolidate()
        self.assertEqual(self.m.read_note(self.m.profile_md), "")
        self.assertEqual(self.now_text(), "")

    def test_bad_rewrite_is_ignored(self):
        self.m.profile_md.write_text("- a\n- b\n- c\n- d\n", encoding="utf-8")
        self.m.llm = lambda messages, n: "プロフィール: e" if messages[0]["content"] == mem.FACTS_PROMPT else "わかりました！"
        self.m.update_notes([mem.Turn(self.t0, "やあ", "うん")])
        self.assertEqual(self.m.read_note(self.m.profile_md), "- a\n- b\n- c\n- d")

    def test_remember_now(self):
        self.m.record("ゲームが好きって覚えておいて", "うん、覚えた！", when=self.t0)
        for _ in range(50):
            if self.m.profile_md.exists():
                break
            time.sleep(0.02)
        self.assertIn("ゲームが好き", self.m.read_note(self.m.profile_md))

    def test_related_and_context(self):
        now = datetime.now()
        self.m.add_episode(now - timedelta(days=1, hours=1), now - timedelta(days=1), "相手は熱があると言っていた。")
        self.m.add_episode(now, now, "相手とゲームの話をした。")
        self.assertEqual([t for _, t in self.m.related("熱は下がった？")], ["相手は熱があると言っていた。"])
        self.m.profile_md.write_text("- ゲームが好き\n", encoding="utf-8")
        self.m.write_now([("状況", "夜ごはんはアジフライ", now - timedelta(hours=2))])
        ctx = self.m.context("熱は下がった？")
        self.assertIn("相手について:\n- ゲームが好き", ctx)
        self.assertIn("相手の今のこと:\n- [状況] 夜ごはんはアジフライ（2 時間前）", ctx)
        self.assertIn("- 昨日: 相手は熱があると言っていた。", ctx)
        self.assertEqual(mem.Memory(Path(self.tmp.name) / "empty", self.brain).context("やあ"), "")

    def test_chat_does_not_wait_for_consolidation(self):
        gate = threading.Event()

        def slow(messages, n):
            gate.wait(5)  # 頭脳がまとめに時間をかけている
            return ""

        self.m.llm = slow
        self.m.add_episode(self.t0, self.t0, "相手は熱があると言っていた。")
        self.m.record("やあ", "うん", when=self.t0)
        worker = threading.Thread(target=self.m.consolidate)
        worker.start()
        time.sleep(0.1)
        self.assertTrue(self.m.busy())
        started = time.perf_counter()
        self.m.context("熱は？")
        self.m.record("ねえ", "なあに")
        self.assertLess(time.perf_counter() - started, 1.0)  # 会話の側は待たされない
        gate.set()
        worker.join()
        self.assertFalse(self.m.busy())

    def test_topic(self):
        self.assertTrue(self.m.topic(random.Random(0)))  # 何も覚えていなくても話題は出る
        self.m.add_episode(self.t0, self.t0, "相手はテスト勉強をしていた。")
        self.m.write_now([("約束", "今度オムライスを作る", datetime.now())])
        topics = {self.m.topic(random.Random(i)) for i in range(40)}
        self.assertTrue(any("テスト勉強" in t for t in topics))
        self.assertTrue(any("今度オムライスを作る" in t for t in topics))


class FakeOllama(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    requests: list = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeOllama.requests.append(body)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(f"data: {json.dumps({'choices': [{'delta': {'content': 'ねえねえ、テストどうだった？'}}]}, ensure_ascii=False)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.close_connection = True


def serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class ProxyMemoryTest(unittest.TestCase):
    def setUp(self):
        try:
            from openai import OpenAI
        except ImportError:
            self.skipTest("openai パッケージなし")
        FakeOllama.requests.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.m = mem.Memory(Path(self.tmp.name), FakeBrain(), fake_embed)
        self.m.profile_md.write_text("- テスト勉強をしている\n", encoding="utf-8")
        self.ollama = serve(FakeOllama)
        self.proxy = serve(llm_proxy.make_handler(f"http://127.0.0.1:{self.ollama.server_port}", memory=self.m))
        self.base = f"http://127.0.0.1:{self.proxy.server_port}"
        self.client = OpenAI(api_key="x", base_url=self.base + "/v1")

    def tearDown(self):
        self.proxy.shutdown()
        self.ollama.shutdown()
        self.m.db.close()
        self.tmp.cleanup()

    def chat(self, text, history=()):
        msgs = [{"role": "system", "content": "あなたはめい。"}] + list(history) + [{"role": "user", "content": text}]
        before = len(self.m.pending())
        stream = self.client.chat.completions.create(model="m", stream=True, messages=msgs)
        reply = "".join((c.choices[0].delta.content or "") for c in stream)
        for _ in range(50):  # 記録は返事を送り終えてから書かれる
            if len(self.m.pending()) > before:
                break
            time.sleep(0.02)
        return reply

    def test_records_and_gives_memory(self):
        self.chat("ただいま")
        self.assertIn("テスト勉強をしている", FakeOllama.requests[-1]["messages"][0]["content"])
        self.assertEqual([(t.user, t.assistant) for t in self.m.pending()], [("ただいま", "ねえねえ、テストどうだった？")])

    def test_proactive(self):
        # Open-LLM-VTuber の「話しかけて」は、話題つきの日本語の指示に置き換わる
        self.chat("Please say something that would be engaging and appropriate for the current context.")
        sent = FakeOllama.requests[-1]["messages"][-1]["content"]
        self.assertIn("あなたから話しかける番", sent)
        self.assertTrue(self.m.pending()[-1].proactive)
        # 相手が返事をしたら、キャラが話しかけた内容を履歴に足す（本体は履歴に残さないため）
        self.chat("まあまあだったよ")
        msgs = FakeOllama.requests[-1]["messages"]
        said = [i for i, m in enumerate(msgs) if m == {"role": "assistant", "content": "ねえねえ、テストどうだった？"}]
        self.assertEqual(len(said), 1)
        self.assertEqual(msgs[-1], {"role": "user", "content": "まあまあだったよ"})
        self.assertTrue(all(m["role"] == "system" for m in msgs[said[0] + 1:-1]))

    def test_consolidate_endpoint(self):
        import urllib.request

        self.chat("熱があるんだ")
        req = urllib.request.Request(self.base + "/maid/consolidate", data=b"{}", method="POST")
        with urllib.request.urlopen(req) as res:
            self.assertTrue(json.loads(res.read())["consolidated"])
        self.assertEqual(self.m.pending(), [])
        self.assertEqual(len(self.m.episodes()), 1)


if __name__ == "__main__":
    unittest.main()
