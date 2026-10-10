"""AivisSpeech Engine を OpenAI 互換の TTS API (/v1/audio/speech) として見せる小さな中継サーバー。

Open-LLM-VTuber v1.2.x は AivisSpeech に標準対応していないが、OpenAI 互換 TTS
(`tts_model: 'openai_tts'`) には対応しているので、その形に変換する。
Open-LLM-VTuber 本体を改造しないので、本体のアップデートに影響されない。

標準ライブラリだけで動く（pip install 不要）。

    python bridge/aivis_openai_bridge.py
    python bridge/aivis_openai_bridge.py --voice "話者名/ノーマル" --speed 1.1

エンドポイント:
    POST /v1/audio/speech   {"input": "...", "voice": "<スタイルID | 話者名 | 話者名/スタイル名 | default>", "speed": 1.0}
    GET  /v1/voices         使えるボイス（スタイル ID）の一覧
    GET  /health            AivisSpeech Engine につながるか
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("aivis_bridge")


@dataclass
class Settings:
    aivis_url: str = "http://127.0.0.1:10101"
    default_voice: str = "default"
    speed: float = 1.0
    pitch: float = 0.0
    intonation: float = 1.0
    tempo_dynamics: float = 1.0
    pre_silence: float = 0.0
    post_silence: float = 0.05
    timeout: float = 30.0


class AivisClient:
    def __init__(self, settings: Settings):
        self.s = settings
        self._voices: list[dict] | None = None
        self._lock = threading.Lock()

    def _request(self, method: str, path: str, params: dict | None = None, body: bytes | None = None) -> bytes:
        url = self.s.aivis_url.rstrip("/") + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"Content-Type": "application/json"} if body is not None else {}
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=self.s.timeout) as res:
            return res.read()

    def voices(self, refresh: bool = False) -> list[dict]:
        """[{"id": 888753760, "speaker": "まい", "style": "ノーマル"}, ...]"""
        # 通信中はロックを持たない（AivisSpeech が遅いときに他のリクエストまで巻き込まないため）
        with self._lock:
            cached = self._voices
        if cached is not None and not refresh:
            return cached
        speakers = json.loads(self._request("GET", "/speakers"))
        voices = [
            {"id": st["id"], "speaker": sp["name"], "style": st["name"]}
            for sp in speakers
            for st in sp.get("styles", [])
        ]
        with self._lock:
            self._voices = voices
        return voices

    def resolve(self, voice: str | int | None) -> int:
        voice = str(voice if voice not in (None, "") else self.s.default_voice).strip()
        if voice in ("", "default"):
            voice = self.s.default_voice
        if voice.lstrip("-").isdigit():
            return int(voice)
        for refresh in (False, True):  # モデルを追加した直後でも見つかるように 1 回だけ取り直す
            voices = self.voices(refresh=refresh)
            if not voices:
                break
            if voice in ("", "default"):
                return voices[0]["id"]
            speaker, _, style = voice.partition("/")
            for v in voices:
                if v["speaker"] == speaker and (not style or v["style"] == style):
                    return v["id"]
            if style and not refresh:
                continue
            for v in voices:  # スタイル名が違うだけなら、その話者の最初のスタイルで話す
                if v["speaker"] == speaker:
                    log.warning("'%s' にスタイル '%s' が無いので '%s' を使います（/v1/voices で一覧を確認）", speaker, style, v["style"])
                    return v["id"]
        raise LookupError(f"voice '{voice}' が AivisSpeech に見つかりません（/v1/voices で一覧を確認）")

    def synthesize(self, text: str, voice: str | int | None = None, speed: float | None = None) -> bytes:
        style_id = self.resolve(voice)
        query = json.loads(self._request("POST", "/audio_query", {"text": text, "speaker": style_id}))
        query["speedScale"] = self.s.speed * (speed or 1.0)
        query["pitchScale"] = self.s.pitch
        query["intonationScale"] = self.s.intonation
        if "tempoDynamicsScale" in query:  # AivisSpeech 独自パラメータ
            query["tempoDynamicsScale"] = self.s.tempo_dynamics
        query["prePhonemeLength"] = self.s.pre_silence  # 頭の無音を削ると体感が速くなる
        query["postPhonemeLength"] = self.s.post_silence
        body = json.dumps(query, ensure_ascii=False).encode("utf-8")
        return self._request("POST", "/synthesis", {"speaker": style_id}, body)


def make_handler(client: AivisClient):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # 標準の stderr 出力を logging に流す
            log.debug("%s - %s", self.address_string(), fmt % args)

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, obj) -> None:
            self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _error(self, status: int, message: str) -> None:
            self._json(status, {"error": {"message": message}})

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path.rstrip("/")
            try:
                if path in ("/v1/voices", "/voices"):
                    self._json(200, {"voices": client.voices(refresh=True)})
                elif path == "/health":
                    self._json(200, {"ok": True, "voices": len(client.voices(refresh=True))})
                else:
                    self._error(404, "not found")
            except (urllib.error.URLError, OSError) as e:
                self._error(502, f"AivisSpeech Engine ({client.s.aivis_url}) に接続できません: {e}")

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path.rstrip("/")
            if path not in ("/v1/audio/speech", "/audio/speech"):
                return self._error(404, "not found")
            try:
                length = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                return self._error(400, "invalid JSON")

            text = str(req.get("input") or "").strip()
            if not text:
                return self._error(400, "input が空です")
            fmt = req.get("response_format") or "wav"
            if fmt != "wav":
                return self._error(400, f"response_format '{fmt}' は未対応です（wav のみ）")
            try:
                speed = float(req["speed"]) if req.get("speed") is not None else None
            except (TypeError, ValueError):
                return self._error(400, "speed は数値で指定してください")

            started = time.perf_counter()
            try:
                wav = client.synthesize(text, req.get("voice"), speed)
            except LookupError as e:
                return self._error(400, str(e))
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:500]
                return self._error(502, f"AivisSpeech Engine がエラーを返しました ({e.code}): {detail}")
            except (urllib.error.URLError, OSError) as e:
                return self._error(502, f"AivisSpeech Engine ({client.s.aivis_url}) に接続できません: {e}")
            log.info("合成 %4.0f ms  %s", (time.perf_counter() - started) * 1000, text[:40])
            self._send(200, wav, "audio/wav")

    return Handler


def warm_up(client: AivisClient) -> None:
    """初回リクエストはモデル読み込みで遅いので、起動時に 1 回合成しておく。"""
    for attempt in range(30):
        try:
            started = time.perf_counter()
            client.synthesize("あ")
            log.info("ウォームアップ完了 (%.1f 秒)", time.perf_counter() - started)
            return
        except LookupError as e:
            log.error("%s", e)
            return
        except (urllib.error.URLError, OSError):
            if attempt == 0:
                log.info("AivisSpeech Engine (%s) の起動を待っています...", client.s.aivis_url)
            time.sleep(2)
    log.warning("AivisSpeech Engine に接続できないままです。起動しているか確認してください。")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="AivisSpeech → OpenAI 互換 TTS ブリッジ")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=10102)
    p.add_argument("--aivis-url", default=Settings.aivis_url)
    p.add_argument("--voice", default=Settings.default_voice, help="リクエストに voice が無い/default のときに使うボイス")
    p.add_argument("--speed", type=float, default=Settings.speed, help="話速 (1.0 = 標準)")
    p.add_argument("--pitch", type=float, default=Settings.pitch, help="音高 (0.0 = 標準)")
    p.add_argument("--intonation", type=float, default=Settings.intonation, help="感情表現の強さ (1.0 = 標準)")
    p.add_argument("--tempo-dynamics", type=float, default=Settings.tempo_dynamics, help="テンポの緩急 (1.0 = 標準)")
    p.add_argument("--pre-silence", type=float, default=Settings.pre_silence, help="音声の前の無音 (秒)")
    p.add_argument("--post-silence", type=float, default=Settings.post_silence, help="音声の後の無音 (秒)")
    p.add_argument("--no-warmup", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    client = AivisClient(Settings(
        aivis_url=a.aivis_url, default_voice=a.voice, speed=a.speed, pitch=a.pitch, intonation=a.intonation,
        tempo_dynamics=a.tempo_dynamics, pre_silence=a.pre_silence, post_silence=a.post_silence,
    ))
    server = ThreadingHTTPServer((a.host, a.port), make_handler(client))
    log.info("ブリッジ起動: http://%s:%d/v1  →  %s", a.host, a.port, a.aivis_url)
    if not a.no_warmup:
        threading.Thread(target=warm_up, args=(client,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    sys.exit(main())
