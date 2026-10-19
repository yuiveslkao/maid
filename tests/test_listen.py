"""声で話しかける部分（app/listen.py）のテスト。sherpa-onnx は偽物（numpy は要る）。"""

import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
try:
    import numpy as np
except ImportError:
    np = None


class FakeVad:
    """音が 0.1 より大きい所を「話している」とみなし、続けて 3 窓（512 サンプル）静かなら区切る。"""

    def __init__(self, config, buffer_seconds):
        self.segments, self.current, self.quiet = [], [], 0

    def accept_waveform(self, w):
        if abs(w).max() > 0.1:
            self.current.extend(w)
            self.quiet = 0
        elif self.current:
            self.quiet += 1
            if self.quiet >= 3:
                self.segments.append(types.SimpleNamespace(samples=list(self.current)))
                self.current, self.quiet = [], 0

    def is_speech_detected(self):
        return bool(self.current)

    def empty(self):
        return not self.segments

    @property
    def front(self):
        return self.segments[0]

    def pop(self):
        self.segments.pop(0)

    def reset(self):
        self.segments, self.current, self.quiet = [], [], 0


class FakeAsr:
    decoded: list = []

    def create_stream(self):
        return types.SimpleNamespace(accept_waveform=lambda rate, s: setattr(self, "last", s), result=None)

    def decode_stream(self, stream):
        FakeAsr.decoded.append(len(self.last))
        loud = int((abs(self.last) > 0.1).sum())
        stream.result = types.SimpleNamespace(text="。" if loud < 8000 else f" こんにちは{loud} ")


def fake_sherpa():
    m = types.ModuleType("sherpa_onnx")
    m.OfflineRecognizer = types.SimpleNamespace(from_transducer=lambda *a, **k: FakeAsr())
    m.SileroVadModelConfig = lambda **k: k
    m.VadModelConfig = lambda **k: k
    m.VoiceActivityDetector = FakeVad
    return m


@unittest.skipIf(np is None, "numpy なし")
class RecognizerTest(unittest.TestCase):
    def setUp(self):
        with mock.patch.dict(sys.modules, {"sherpa_onnx": fake_sherpa()}):
            import listen

            self.listen = listen
            self.rec = listen.Recognizer(Path("asr"), Path("vad.onnx"))
        FakeAsr.decoded = []

    def feed_in_blocks(self, audio, block=1600):
        out = []
        for k in range(0, len(audio), block):
            out += self.rec.feed(audio[k:k + block])
        return out

    def test_sentences_and_noise(self):
        quiet = np.zeros(4000, dtype=np.float32)
        speech = np.full(16000, 0.5, dtype=np.float32)
        click = np.full(1024, 0.5, dtype=np.float32)      # 0.3 秒より短い物音は捨てる
        mumble = np.full(6000, 0.5, dtype=np.float32)     # 文字にならなかった（句読点だけ）は捨てる
        audio = np.concatenate([quiet, speech, quiet, click, quiet, mumble, quiet, speech, quiet])
        out = self.feed_in_blocks(audio)
        self.assertEqual(out, ["こんにちは16000", "こんにちは16000"])
        # 前後に 0.5 秒ずつ無音を足してから文字にする（言いはじめを聞き落とさないように）
        self.assertGreaterEqual(FakeAsr.decoded[0], 16000 + 16000)

    def test_speaking_flag(self):
        self.rec.feed(np.full(1024, 0.5, dtype=np.float32))
        self.assertTrue(self.rec.speaking())
        self.rec.reset()
        self.assertFalse(self.rec.speaking())
