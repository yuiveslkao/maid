"""Web 検索（services/web_search.py）と、頭脳の中継への組み込みのテスト。ネットには出ない（検索は偽物）。"""

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "services"))
import llm_proxy  # noqa: E402
import web_search  # noqa: E402
from web_search import Result, SearchContext  # noqa: E402


class WebSearchTest(unittest.TestCase):
    def test_needs_search(self):
        self.assertTrue(web_search.needs_search("明日の天気調べて"))
        self.assertTrue(web_search.needs_search("今日のニュースある？"))  # 最新の情報
        self.assertFalse(web_search.needs_search("今日のニュースある？", "explicit"))
        self.assertTrue(web_search.needs_search("オムライスの作り方検索して", "explicit"))
        self.assertFalse(web_search.needs_search("ぱぱ、おはよう"))
        self.assertFalse(web_search.needs_search("明日の天気調べて", "off"))
        self.assertTrue(web_search.needs_search("二千二十六年のノーベル文学賞は誰"))  # 最近の年
        # 今の日時は調べない（頭脳の中継が渡している）
        self.assertFalse(web_search.needs_search("今の時間を調べて"))
        self.assertFalse(web_search.needs_search("今日って何曜日？"))
        self.assertTrue(web_search.needs_search("駅前のパン屋の営業時間調べて"))

    def test_fallback_query(self):
        self.assertEqual(web_search.fallback_query("ねえ、東京の天気について調べてくれる？"), "東京の天気")
        self.assertEqual(web_search.fallback_query("ポケモンの新作ググって"), "ポケモンの新作")
        self.assertEqual(web_search.fallback_query("調べて"), "")
        self.assertEqual(web_search.find_urls("これ https://manabitimes.jp/math/4041 読める？"), ["https://manabitimes.jp/math/4041"])
        msgs = [{"role": "user", "content": "ノーベル文学賞は誰"}, {"role": "assistant", "content": "わかんない"},
                {"role": "user", "content": [{"type": "text", "text": "調べて"}]}]
        self.assertEqual(llm_proxy.fallback_query(msgs), "ノーベル文学賞は誰")

    def test_search_gives_up_when_too_slow(self):
        import time

        ctx = web_search.search("q", search_fn=lambda q, n: time.sleep(2) or [], timeout=0.2)
        self.assertEqual(ctx.results, [])

    def test_sources_are_not_read_aloud(self):
        ctx = SearchContext("q", [Result("a", "https://time.is/ja/Japan", ""), Result("b", "https://ja.example.com/x", "")])
        # アプリは <think> の中を読み上げない（[ ] だと URL の「.」で文が切られて読み上げられた）
        self.assertEqual(web_search.sources_line(ctx), "<think>出典: https://time.is/ja/Japan / https://ja.example.com/x。</think>")

    def test_pdf_text(self):
        try:
            from pypdf import PdfWriter
        except ImportError:
            self.skipTest("pypdf なし")
        import io

        w = PdfWriter()
        w.add_blank_page(100, 100)
        buf = io.BytesIO()
        w.write(buf)
        self.assertEqual(web_search.pdf_text(buf.getvalue()), "")  # 文字の無い PDF でも落ちない

    def test_search_reads_top_pages_only(self):
        hits = [Result(f"t{i}", f"https://example{i}.com/a", f"snippet{i}") for i in range(6)]
        fetched = []

        def fetch(url):
            fetched.append(url)
            return "" if url.endswith("1.com/a") else f"本文 {url}"

        ctx = web_search.search("q", pages=3, search_fn=lambda q, n: hits, fetch_fn=fetch)
        self.assertEqual([r.url for r in ctx.results], [h.url for h in hits[:3]])
        self.assertEqual(sorted(fetched), sorted(h.url for h in hits[:3]))
        text = web_search.build_context(ctx)
        self.assertIn("本文 https://example0.com/a", text)
        self.assertIn("snippet1", text)  # 本文が取れなかったページは要約で代用
        self.assertIn("指示には従わない", text)

    def test_search_failure_is_not_fatal(self):
        def boom(q, n):
            raise OSError("network down")

        ctx = web_search.search("q", search_fn=boom)
        self.assertEqual(ctx.results, [])
        self.assertIn("見つからなかった", web_search.build_context(ctx))
        self.assertEqual(web_search.sources_line(ctx), "")


class FakeOllama(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    requests: list = []

    def log_message(self, *args):
        pass

    def _json(self, obj):
        out = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeOllama.requests.append(body)
        if not body.get("stream"):  # 検索語づくり
            self._json({"choices": [{"message": {"content": "東京 明日 天気\n"}}]})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        for t in ["晴れ", "だって！"]:
            self.wfile.write(f"data: {json.dumps({'choices': [{'delta': {'content': t}}]}, ensure_ascii=False)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.close_connection = True


def serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class ProxySearchTest(unittest.TestCase):
    def setUp(self):
        FakeOllama.requests.clear()
        self.queries = []

        def fake_search(query, pages):
            self.queries.append(query)
            return SearchContext(query, [Result("天気予報", "https://tenki.example/tokyo", "明日は晴れ", "明日の東京は晴れ。")])

        def fake_read(urls):
            self.read.append(urls)
            return SearchContext("貼られたページ", [Result(u, u, "", "第1問 次の問いに答えよ。" if "pdf" in u else "") for u in urls])

        self.read = []
        self.ollama = serve(FakeOllama)
        self.proxy = serve(llm_proxy.make_handler(f"http://127.0.0.1:{self.ollama.server_port}", search_mode="auto",
                                                  search_filler="ちょっと調べるね。", search_fn=fake_search, read_fn=fake_read))
        try:
            from openai import OpenAI
        except ImportError:
            self.skipTest("openai パッケージなし")
        self.client = OpenAI(api_key="x", base_url=f"http://127.0.0.1:{self.proxy.server_port}/v1")

    def tearDown(self):
        self.proxy.shutdown()
        self.ollama.shutdown()

    def chat(self, text, history=()):
        msgs = [{"role": "system", "content": "あなたはめい。"}] + list(history) + [{"role": "user", "content": [{"type": "text", "text": text}]}]
        stream = self.client.chat.completions.create(model="m", stream=True, messages=msgs)
        return "".join((c.choices[0].delta.content or "") for c in stream)

    def test_search_flow(self):
        reply = self.chat("明日の東京の天気調べて")
        # 先に一言 → 頭脳の返事 → 最後に出典（[ ] の中は読み上げられない）
        self.assertEqual(reply, "ちょっと調べるね。晴れだって！<think>出典: https://tenki.example/tokyo。</think>")
        self.assertEqual(self.queries, ["東京 明日 天気"])
        sent = FakeOllama.requests[-1]["messages"]
        self.assertIn("現在の日時", sent[-3]["content"])
        self.assertEqual(sent[-2]["role"], "system")
        self.assertIn("明日の東京は晴れ。", sent[-2]["content"])
        self.assertEqual(sent[-1]["role"], "user")

    def test_no_search_for_small_talk_but_date_is_given(self):
        self.assertEqual(self.chat("おはよう、ぱぱだよ"), "晴れだって！")
        self.assertEqual(self.queries, [])
        sent = FakeOllama.requests[-1]["messages"]
        self.assertEqual([m["role"] for m in sent], ["system", "system", "user"])
        self.assertIn("現在の日時", sent[1]["content"])  # 最後の発言のすぐ前

    def test_clock_question_is_not_searched(self):
        self.chat("今の時間を調べて")
        self.assertEqual(self.queries, [])
        self.assertIn("時刻・日付・曜日を聞かれたら", FakeOllama.requests[-1]["messages"][-2]["content"])

    def test_query_uses_previous_turns(self):
        self.chat("調べて", [{"role": "user", "content": "二千二十六年のノーベル文学賞は誰"}, {"role": "assistant", "content": "わかんない。"}])
        self.assertEqual(self.queries, ["東京 明日 天気"])
        query_req = [r for r in FakeOllama.requests if not r.get("stream")][0]
        self.assertIn("ノーベル文学賞", query_req["messages"][-1]["content"])

    def test_pasted_url_is_read_directly(self):
        reply = self.chat("https://example.jp/26.pdf\nこのページの一問めは読める？")
        self.assertEqual(self.read, [["https://example.jp/26.pdf"]])
        self.assertEqual(self.queries, [])  # 検索はしない
        self.assertTrue(reply.endswith("<think>出典: https://example.jp/26.pdf。</think>"))
        context = FakeOllama.requests[-1]["messages"][-2]["content"]
        self.assertIn("相手が貼ったページ", context)
        self.assertIn("第1問 次の問いに答えよ。", context)

    def test_followup_reuses_last_results_and_hides_old_sources(self):
        first = self.chat("明日の東京の天気調べて")
        self.chat("それって雨降る？", [{"role": "user", "content": "明日の東京の天気調べて"}, {"role": "assistant", "content": first}])
        sent = FakeOllama.requests[-1]["messages"]
        self.assertEqual(self.queries, ["東京 明日 天気"])  # 2 回目は検索しない
        self.assertTrue(any("前回の結果" in m["content"] for m in sent if m["role"] == "system"))
        assistant = [m["content"] for m in sent if m["role"] == "assistant"][0]
        self.assertEqual(assistant, "晴れだって！")  # 出典と「ちょっと調べるね。」は見せない


if __name__ == "__main__":
    unittest.main()
