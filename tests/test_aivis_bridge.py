"""ブリッジのテスト。偽の AivisSpeech Engine を立てて、変換が正しいかを確かめる。

    python -m unittest discover -s tests
"""

import json
import sys
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bridge"))
import aivis_openai_bridge as bridge  # noqa: E402

SPEAKERS = [
    {"name": "まい", "styles": [{"id": 100, "name": "ノーマル"}, {"id": 101, "name": "あまあま"}]},
    {"name": "花音", "styles": [{"id": 200, "name": "ノーマル"}]},
]
FAKE_WAV = b"RIFF....WAVEfake"


class FakeAivis(BaseHTTPRequestHandler):
    synth_calls: list = []

    def log_message(self, *args):
        pass

    def _send(self, body: bytes, ctype: str):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/speakers"):
            self._send(json.dumps(SPEAKERS).encode(), "application/json")

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if u.path == "/audio_query":
            query = {"speedScale": 1.0, "pitchScale": 0.0, "intonationScale": 1.0, "tempoDynamicsScale": 1.0,
                     "prePhonemeLength": 0.1, "postPhonemeLength": 0.1, "kana": q["text"][0]}
            self._send(json.dumps(query).encode(), "application/json")
        elif u.path == "/synthesis":
            FakeAivis.synth_calls.append((int(q["speaker"][0]), json.loads(body)))
            self._send(FAKE_WAV, "audio/wav")


def serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class BridgeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.aivis = serve(FakeAivis)
        settings = bridge.Settings(aivis_url=f"http://127.0.0.1:{cls.aivis.server_port}", speed=1.2, pre_silence=0.0)
        cls.bridge = serve(bridge.make_handler(bridge.AivisClient(settings)))
        cls.base = f"http://127.0.0.1:{cls.bridge.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.bridge.shutdown()
        cls.aivis.shutdown()

    def setUp(self):
        FakeAivis.synth_calls.clear()

    def speech(self, payload):
        req = urllib.request.Request(self.base + "/v1/audio/speech", data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req) as res:
            return res.status, res.headers["Content-Type"], res.read()

    def test_default_voice_uses_first_style(self):
        status, ctype, body = self.speech({"model": "x", "voice": "default", "input": "こんにちは", "response_format": "wav"})
        self.assertEqual((status, ctype, body), (200, "audio/wav", FAKE_WAV))
        speaker, query = FakeAivis.synth_calls[0]
        self.assertEqual(speaker, 100)
        self.assertEqual(query["kana"], "こんにちは")
        self.assertAlmostEqual(query["speedScale"], 1.2)
        self.assertEqual(query["prePhonemeLength"], 0.0)

    def test_voice_by_id_and_name(self):
        self.speech({"voice": "200", "input": "a"})
        self.speech({"voice": "まい/あまあま", "input": "a"})
        self.speech({"voice": "花音", "input": "a"})
        self.assertEqual([c[0] for c in FakeAivis.synth_calls], [200, 101, 200])

    def test_unknown_style_falls_back_to_speaker(self):
        self.speech({"voice": "花音/あまあま", "input": "a"})
        self.assertEqual(FakeAivis.synth_calls[0][0], 200)

    def test_request_speed_multiplies(self):
        self.speech({"voice": "100", "input": "a", "speed": 1.5})
        self.assertAlmostEqual(FakeAivis.synth_calls[0][1]["speedScale"], 1.8)

    def test_errors(self):
        for payload, code in [({"voice": "いない人", "input": "a"}, 400),
                              ({"voice": "100", "input": ""}, 400),
                              ({"voice": "100", "input": "a", "response_format": "mp3"}, 400)]:
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.speech(payload)
            self.assertEqual(cm.exception.code, code)

    def test_voices_listing(self):
        with urllib.request.urlopen(self.base + "/v1/voices") as res:
            voices = json.loads(res.read())["voices"]
        self.assertEqual(voices[1], {"id": 101, "speaker": "まい", "style": "あまあま"})

    def test_works_with_openai_client(self):
        """Open-LLM-VTuber の openai_tts と同じ呼び方で動くか（openai が入っていれば）。"""
        try:
            from openai import OpenAI
        except ImportError:
            self.skipTest("openai パッケージなし")
        client = OpenAI(api_key="not-needed", base_url=self.base + "/v1")
        with client.audio.speech.with_streaming_response.create(
            model="aivisspeech", voice="default", input="テスト", response_format="wav", speed=1.0
        ) as res:
            self.assertEqual(res.read(), FAKE_WAV)


if __name__ == "__main__":
    unittest.main()
