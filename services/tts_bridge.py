"""AivisSpeech Engine を OpenAI 互換の TTS API (/v1/audio/speech) として見せる小さな中継サーバー。

maid のアプリ（app/chat.py の Speaker）は、返事を文ごとにここへ送って声にする。
声の名前（「話者名/スタイル名」）をスタイル ID に変換し、使う声のモデルを起動時に読み込んでおく。

標準ライブラリだけで動く。

    python services/tts_bridge.py --voice "コハク/あまあま" --speed 1.0

エンドポイント:
    POST /v1/audio/speech   {"input": "...", "voice": "<スタイルID | 話者名 | 話者名/スタイル名 | default>", "speed": 1.0}
    GET  /v1/voices         使える声（スタイル ID）の一覧
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

log = logging.getLogger("tts_bridge")


@dataclass
class Settings:
    aivis_url: str = "http://127.0.0.1:10101"
    default_voice: str = "default"
    speed: float = 1.0
    pre_silence: float = 0.0    # 頭の無音を削ると体感が速くなる
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
        query["prePhonemeLength"] = self.s.pre_silence
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
            if path != "/v1/voices":
                return self._error(404, "not found")
            try:
                self._json(200, {"voices": client.voices(refresh=True)})
            except (urllib.error.URLError, OSError) as e:
                self._error(502, f"AivisSpeech Engine ({client.s.aivis_url}) に接続できません: {e}")

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path.rstrip("/")
            if path != "/v1/audio/speech":
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
    p = argparse.ArgumentParser(description="声の中継（AivisSpeech → OpenAI 互換 TTS）")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=10102)
    p.add_argument("--aivis-url", default=Settings.aivis_url)
    p.add_argument("--voice", default=Settings.default_voice, help="リクエストの voice が default のときに使う声")
    p.add_argument("--speed", type=float, default=Settings.speed, help="話す速さ (1.0 = 標準)")
    a = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    client = AivisClient(Settings(aivis_url=a.aivis_url, default_voice=a.voice, speed=a.speed))
    server = ThreadingHTTPServer((a.host, a.port), make_handler(client))
    log.info("声の中継: http://%s:%d/v1  →  %s（声: %s）", a.host, a.port, a.aivis_url, a.voice)
    threading.Thread(target=warm_up, args=(client,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    sys.exit(main())
