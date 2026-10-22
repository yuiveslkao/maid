"""声で話しかける部分。

マイクの音は画面（WebView2）が取る。ブラウザと同じ雑音の除去・エコーの除去・音量の自動調整がかかるので、
マイクの音をそのまま使うより聞き取りがよい。画面は 16kHz の音を 0.1 秒ごとに hear() に渡す。
ここで話しはじめと話し終わりを見つけ（Silero VAD）、話し終わるごとに文字にする（ReazonSpeech。どちらも sherpa-onnx で CPU）。
Discord で通話中は聞かない（services/winutil.py）。
"""

from __future__ import annotations

import base64
import logging
import queue
import re
import threading
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger("listen")

RATE = 16000
WINDOW = 512                 # Silero VAD が一度に見る長さ（16kHz で 32ms）
MIN_SECONDS = 0.3            # これより短い音は物音として捨てる
PRE_SECONDS = 0.25           # 話しはじめと判定される少し前の音も足す（言いはじめの子音を落とさないように）
KEEP_SECONDS = 25            # 〃 のために残しておく音の長さ（1 回の発言は 20 秒で区切る）
NOT_WORDS = re.compile(r"[\s、。！？!?…ー〜]+")
ASR_FILES = ("encoder-epoch-99-avg-1.int8.onnx", "decoder-epoch-99-avg-1.int8.onnx",
             "joiner-epoch-99-avg-1.int8.onnx", "tokens.txt")


class Recognizer:
    """音（16kHz の float32）を区切って文字にする。"""

    def __init__(self, asr_dir: Path, vad_model: Path, silence_ms: int = 900, threads: int = 2):
        import sherpa_onnx

        enc, dec, joi, tok = (str(Path(asr_dir) / f) for f in ASR_FILES)
        self.asr = sherpa_onnx.OfflineRecognizer.from_transducer(enc, dec, joi, tok, num_threads=threads,
                                                                 sample_rate=RATE, decoding_method="greedy_search")
        silero = sherpa_onnx.SileroVadModelConfig(model=str(vad_model), threshold=0.5, min_silence_duration=silence_ms / 1000,
                                                  min_speech_duration=0.25, window_size=WINDOW, max_speech_duration=20)
        self.vad = sherpa_onnx.VoiceActivityDetector(sherpa_onnx.VadModelConfig(silero_vad=silero, sample_rate=RATE), 30)
        self.reset()

    def reset(self) -> None:
        import numpy as np

        self.vad.reset()
        self.buf = np.zeros(0, dtype=np.float32)       # まだ VAD に渡していない音（窓の長さに満たない分）
        self.recent = np.zeros(0, dtype=np.float32)    # 最近の音
        self.recent_from = 0                           # recent の先頭が、reset からの何サンプル目か

    def speaking(self) -> bool:
        return self.vad.is_speech_detected()

    def feed(self, samples) -> list[str]:
        """音を渡す。話し終わった文があれば、その文字を返す。"""
        import numpy as np

        samples = np.asarray(samples, dtype=np.float32)
        self.recent = np.concatenate([self.recent, samples])
        if len(self.recent) > KEEP_SECONDS * RATE:
            drop = len(self.recent) - KEEP_SECONDS * RATE
            self.recent, self.recent_from = self.recent[drop:], self.recent_from + drop
        self.buf = np.concatenate([self.buf, samples])
        while len(self.buf) >= WINDOW:
            self.vad.accept_waveform(self.buf[:WINDOW])
            self.buf = self.buf[WINDOW:]
        out = []
        while not self.vad.empty():
            seg = self.vad.front
            samples, start = np.asarray(seg.samples, dtype=np.float32), seg.start
            self.vad.pop()
            if len(samples) < MIN_SECONDS * RATE:
                continue
            a, b = max(start - int(PRE_SECONDS * RATE), self.recent_from) - self.recent_from, start - self.recent_from
            if 0 <= a < b <= len(self.recent):
                samples = np.concatenate([self.recent[a:b], samples])
            text = self.decode(samples)
            if NOT_WORDS.sub("", text):
                out.append(text)
        return out

    def decode(self, samples) -> str:
        import numpy as np

        pad = np.zeros(RATE // 2, dtype=np.float32)  # 前後に無音を足さないと、言いはじめを聞き落とす
        stream = self.asr.create_stream()
        stream.accept_waveform(RATE, np.concatenate([pad, np.asarray(samples, dtype=np.float32), pad]))
        self.asr.decode_stream(stream)
        return stream.result.text.strip()


class Listener:
    """画面から届く音を聞き、話し終わるごとに on_text(文字) を呼ぶ。

    on_state には "loading"（準備中）/ "listening" / "speaking"（話している）/ "off" / "discord"（通話中で聞かない）/
    "error" を渡す。マイクを開く・閉じるのは画面（enabled を見て決める）。"""

    def __init__(self, asr_dir: Path, vad_model: Path, on_text: Callable[[str], None], on_state: Callable[[str], None],
                 silence_ms: int = 900, blocked: Callable[[], bool] = lambda: False, enabled: bool = True):
        self.asr_dir, self.vad_model, self.silence_ms = Path(asr_dir), Path(vad_model), silence_ms
        self.on_text, self.on_state, self.blocked = on_text, on_state, blocked
        self.enabled = enabled
        self.state = ""
        self.stop = threading.Event()
        self.audio: queue.Queue = queue.Queue(maxsize=100)  # 10 秒分。詰まったら古いものを捨てる

    def set_enabled(self, on: bool) -> None:
        self.enabled = bool(on)

    def hear(self, b64: str) -> None:
        """画面から届いた音（16kHz・16 ビットの PCM を base64 にしたもの）。"""
        if not self.enabled:
            return
        try:
            self.audio.put_nowait(b64)
        except queue.Full:
            pass

    def _set(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self.on_state(state)

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        import numpy as np

        self._set("loading")
        try:
            t = time.monotonic()
            rec = Recognizer(self.asr_dir, self.vad_model, self.silence_ms)
            log.info("音声認識の準備ができました（%.1f 秒）", time.monotonic() - t)
        except Exception as e:
            log.warning("音声認識を準備できませんでした: %s", e)
            self._set("error")
            return
        checked, blocked = 0.0, False
        while not self.stop.is_set():
            if time.monotonic() - checked >= 1:
                checked, blocked = time.monotonic(), self.blocked()
            if not self.enabled or blocked:
                self._set("discord" if self.enabled else "off")
                rec.reset()
                while not self.audio.empty():  # 通話中・切っている間の音は使わない
                    self.audio.get_nowait()
                self.stop.wait(0.3)
                continue
            try:
                b64 = self.audio.get(timeout=0.5)
            except queue.Empty:
                self._set("listening")
                continue
            samples = np.frombuffer(base64.b64decode(b64), dtype=np.int16).astype(np.float32) / 32768
            texts = rec.feed(samples)
            self._set("speaking" if rec.speaking() else "listening")
            for text in texts:
                log.info("聞こえた: %s", text)
                self.on_text(text)
