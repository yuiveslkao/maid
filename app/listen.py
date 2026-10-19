"""チャットだけの版の、声で話しかける部分。

マイクの音を自分で取り（sounddevice）、話しはじめと話し終わりを見つけ（Silero VAD）、
話し終わるごとに文字にする（ReazonSpeech。フル版と同じモデル。どちらも sherpa-onnx で CPU）。
Discord で通話中は聞かない（フル版の Discord ゲートと同じ判定）。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger("listen")

RATE = 16000
WINDOW = 512                 # Silero VAD が一度に見る長さ（16kHz で 32ms）
MIN_SECONDS = 0.3            # これより短い音は物音として捨てる
NOT_WORDS = re.compile(r"[\s、。！？!?…ー〜]+")
ASR_FILES = ("encoder-epoch-99-avg-1.int8.onnx", "decoder-epoch-99-avg-1.int8.onnx",
             "joiner-epoch-99-avg-1.int8.onnx", "tokens.txt")


class Recognizer:
    """音（16kHz の float32）を区切って文字にする。マイクからではなく feed() に渡した音を扱う（テストしやすいように）。"""

    def __init__(self, asr_dir: Path, vad_model: Path, silence_ms: int = 900, threads: int = 2):
        import sherpa_onnx

        enc, dec, joi, tok = (str(Path(asr_dir) / f) for f in ASR_FILES)
        self.asr = sherpa_onnx.OfflineRecognizer.from_transducer(enc, dec, joi, tok, num_threads=threads,
                                                                 sample_rate=RATE, decoding_method="greedy_search")
        silero = sherpa_onnx.SileroVadModelConfig(model=str(vad_model), threshold=0.5, min_silence_duration=silence_ms / 1000,
                                                  min_speech_duration=0.25, window_size=WINDOW, max_speech_duration=20)
        self.vad = sherpa_onnx.VoiceActivityDetector(sherpa_onnx.VadModelConfig(silero_vad=silero, sample_rate=RATE), 30)
        self.buf = None

    def reset(self) -> None:
        self.vad.reset()
        self.buf = None

    def speaking(self) -> bool:
        return self.vad.is_speech_detected()

    def feed(self, samples) -> list[str]:
        """音を渡す。話し終わった文があれば、その文字を返す。"""
        import numpy as np

        self.buf = samples if self.buf is None else np.concatenate([self.buf, samples])
        while len(self.buf) >= WINDOW:
            self.vad.accept_waveform(self.buf[:WINDOW])
            self.buf = self.buf[WINDOW:]
        out = []
        while not self.vad.empty():
            seg = self.vad.front.samples
            self.vad.pop()
            if len(seg) < MIN_SECONDS * RATE:
                continue
            text = self.decode(seg)
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
    """マイクを聞きつづけ、話し終わるごとに on_text(文字) を呼ぶ。

    on_state には "loading"（準備中）/ "listening" / "speaking"（話している）/ "off" / "discord"（通話中で聞かない）/
    "error" を渡す。マイクを切っている間はマイクを閉じる（Windows のマイク使用中の表示も消える）。"""

    def __init__(self, asr_dir: Path, vad_model: Path, on_text: Callable[[str], None], on_state: Callable[[str], None],
                 silence_ms: int = 900, blocked: Callable[[], bool] = lambda: False, enabled: bool = True):
        self.asr_dir, self.vad_model, self.silence_ms = Path(asr_dir), Path(vad_model), silence_ms
        self.on_text, self.on_state, self.blocked = on_text, on_state, blocked
        self.enabled = enabled
        self.state = ""
        self.stop = threading.Event()

    def set_enabled(self, on: bool) -> None:
        self.enabled = bool(on)

    def _set(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self.on_state(state)

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        self._set("loading")
        try:
            t = time.monotonic()
            rec = Recognizer(self.asr_dir, self.vad_model, self.silence_ms)
            log.info("音声認識の準備ができました（%.1f 秒）", time.monotonic() - t)
        except Exception as e:
            log.warning("音声認識を準備できませんでした: %s", e)
            self._set("error")
            return
        while not self.stop.is_set():
            if not self.enabled:
                self._set("off")
                self.stop.wait(0.3)
                continue
            if self.blocked():
                self._set("discord")
                self.stop.wait(1)
                continue
            try:
                self._listen(rec)
            except Exception as e:
                log.warning("マイクを使えませんでした（3 秒後にやり直します）: %s", e)
                self._set("error")
                self.stop.wait(3)

    def _listen(self, rec: Recognizer) -> None:
        """マイクを開き、切られる・通話が始まるまで聞く。"""
        import numpy as np
        import sounddevice as sd

        rate = RATE
        try:
            sd.check_input_settings(samplerate=RATE, channels=1, dtype="float32")
        except Exception:
            rate = int(sd.query_devices(kind="input")["default_samplerate"])  # 16kHz で開けないマイクは、取ってから変換する
        block = int(rate * 0.1)
        rec.reset()
        checked = time.monotonic()
        with sd.InputStream(samplerate=rate, channels=1, dtype="float32", blocksize=block) as stream:
            self._set("listening")
            while not self.stop.is_set() and self.enabled:
                if time.monotonic() - checked >= 1:
                    checked = time.monotonic()
                    if self.blocked():
                        return
                data, _ = stream.read(block)
                samples = data[:, 0]
                if rate != RATE:
                    n = int(len(samples) * RATE / rate)
                    samples = np.interp(np.linspace(0, len(samples) - 1, n), np.arange(len(samples)), samples).astype(np.float32)
                texts = rec.feed(samples)
                self._set("speaking" if rec.speaking() else "listening")
                for text in texts:
                    log.info("聞こえた: %s", text)
                    self.on_text(text)
