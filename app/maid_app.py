"""maid のアプリ。画面は WebView2（Windows に標準で入っている Edge の部品）で表示する。

    python app/maid_app.py launcher ...   起動の画面（使うもの・アイコンを選ぶ）
    python app/maid_app.py run ...        会話のウィンドウ（中身は app/chat.py・app/listen.py・app/screen.py）
    python app/maid_app.py character      character.yaml が無ければ作る（前の設定があれば引き継ぐ）

起動の画面の終わり方（終了コード）: 0 = やめる / 10 = 起動する
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "services"))
from chat import ChatSession, Speaker, proactive_loop  # noqa: E402
from common import Icons, Settings, ensure_character, load_character, ROOT  # noqa: E402
from winutil import discord_using_mic, seconds_since_input  # noqa: E402

log = logging.getLogger("maid_app")
UI = Path(__file__).resolve().parent / "ui"
EXIT_START = 10
LAUNCH_KEYS = ("voice_reply", "mic", "search", "memory", "proactive_minutes")


def run_launcher(a) -> int:
    import webview

    settings, icons = Settings(), Icons(Path(a.data) / "icons")
    character = load_character()
    result = {"code": 0}
    holder: dict = {}

    class Api:
        def load(self):
            s = settings.all()
            return {"voice_reply": bool(s.get("voice_reply")), "mic": bool(s.get("mic", True)),
                    "search": s.get("search", "auto"), "memory": s.get("memory", True) is not False,
                    "proactive_minutes": float(s.get("proactive_minutes") or 0),
                    "name": character["name"], "icons": {k: icons.data_url(k) for k in ("character", "user")}}

        def save_icon(self, kind, data_url):
            return icons.save(kind, data_url)

        def remove_icon(self, kind):
            icons.remove(kind)

        def launch(self, choice):
            settings.update({k: choice[k] for k in LAUNCH_KEYS if k in choice})
            result["code"] = EXIT_START
            holder["window"].destroy()

        def quit(self):
            holder["window"].destroy()

    holder["window"] = webview.create_window(f"{character['name']} を起動", url=str(UI / "launcher.html"), js_api=Api(),
                                             width=580, height=720)
    webview.start(private_mode=False, storage_path=a.storage)
    return result["code"]


def run(a) -> int:
    os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = "--autoplay-policy=no-user-gesture-required"  # キャラから話しかけた声も鳴らす
    import screen
    import webview

    character = load_character()
    icons = Icons(Path(a.data) / "icons")
    session = ChatSession(a.proxy, character, Path(a.data) / "chat.json")
    state = {"voice": bool(a.voice and a.tts), "reply": 0}
    holder: dict = {}
    stop = threading.Event()

    def js(fn: str, *args) -> None:
        window = holder.get("window")
        if window:
            window.evaluate_js(f"window.maid && maid.{fn}({','.join(json.dumps(x, ensure_ascii=False) for x in args)})")

    # 声で返事するときは、文を声と一緒に画面へ渡す（画面は声を鳴らしはじめたときに文を出す）
    speaker = Speaker(a.tts, lambda b64, part: js("say", b64, part)) if a.tts else None

    def reply(text: str, proactive: bool = False, images: list[str] | None = None) -> None:
        # 声で割り込まれたら、前の返事の続きは画面にも声にも出さない
        state["reply"] += 1
        rid = state["reply"]
        voiced = bool(speaker and state["voice"])  # 返事の途中で切り替えても、この返事は最初の出し方のまま
        if speaker:
            speaker.reset()
        if proactive:
            js("proactive")

        def chunk(piece: str) -> None:
            if rid != state["reply"]:
                return
            if voiced:
                speaker.feed(piece)
            else:
                js("chunk", piece)

        session.send(text, chunk, proactive=proactive, images=images)
        if rid != state["reply"]:
            return
        if voiced:
            speaker.feed("", final=True)
            speaker.finish()
        else:
            js("done")

    def capture() -> str:
        """画面を撮る。自分のウィンドウが写らないように、撮る間だけ隠す。"""
        window = holder["window"]
        window.hide()
        time.sleep(0.4)
        try:
            return screen.capture()
        finally:
            window.show()

    def heard(text: str) -> None:
        """声で話しかけられた。返事の途中なら止めて、聞こえた文を送る。「画面見て」なら画面も見せる。"""
        state["reply"] += 1  # 前の返事の続きを、この時点から出さない
        session.cancel.set()
        if speaker:
            speaker.reset()
        images = []
        if screen.wants_look(text):
            try:
                images = [capture()]
            except Exception as e:
                log.warning("画面を撮れませんでした: %s", e)
        js("heard", text, images)
        threading.Thread(target=reply, args=(text, False, images), daemon=True).start()

    listener = None
    if a.asr_dir and a.vad:
        from listen import Listener

        listener = Listener(Path(a.asr_dir), Path(a.vad), heard, lambda st: js("micState", st),
                            silence_ms=a.silence_ms, blocked=discord_using_mic, enabled=a.mic)

    class Api:
        def init(self):
            return {"name": character["name"], "human": character["human"],
                    "icons": {k: icons.data_url(k) for k in ("character", "user")},
                    "history": session.history, "voice": state["voice"], "voiceAvailable": bool(a.tts),
                    "mic": bool(listener and listener.enabled), "micAvailable": bool(listener),
                    "micState": listener.state if listener else ""}

        def send(self, text, images=None):
            threading.Thread(target=reply, args=(str(text), False, list(images or [])), daemon=True).start()

        def capture(self):
            return capture()

        def shrink(self, data_url):
            return screen.shrink(data_url)

        def stop(self):
            session.cancel.set()
            if speaker:
                speaker.reset()

        def set_voice(self, on):
            state["voice"] = bool(on and a.tts)

        def set_mic(self, on):
            if listener:
                listener.set_enabled(on)
                return listener.state

        def hear(self, b64):
            if listener:
                listener.hear(b64)

        def mic_error(self, message):
            log.warning("マイクを使えませんでした: %s", message)

        def open_url(self, url):
            if str(url).startswith(("http://", "https://")):
                webbrowser.open(url)

        def clear(self):
            session.clear()

    threading.Thread(target=proactive_loop, args=(session, a.proactive_minutes, lambda: reply("", proactive=True),
                                                  seconds_since_input, stop),
                     kwargs={"blocked": discord_using_mic}, daemon=True).start()
    holder["window"] = webview.create_window(character["name"], url=str(UI / "chat.html"), js_api=Api(), width=760, height=860)
    if listener:
        listener.start()
    webview.start(private_mode=False, storage_path=a.storage)
    stop.set()
    if listener:
        listener.stop.set()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="maid のアプリ")
    sub = p.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--storage", default=None, help="画面の設定やマイクの許可を保存するフォルダ")
    common.add_argument("--data", default=str(ROOT / "data"), help="アイコンや会話を置くフォルダ")

    sub.add_parser("launcher", parents=[common])
    sub.add_parser("character")
    c = sub.add_parser("run", parents=[common])
    c.add_argument("--proxy", default="http://127.0.0.1:11434", help="頭脳の中継")
    c.add_argument("--tts", default="", help="声の中継（空なら声で返事しない）")
    c.add_argument("--voice", action="store_true", help="最初から声で返事する")
    c.add_argument("--asr-dir", default="", help="音声認識のモデル（ReazonSpeech）のフォルダ。空なら声で話しかけられない")
    c.add_argument("--vad", default="", help="話しはじめと話し終わりを見つけるモデル（silero_vad.onnx）")
    c.add_argument("--silence-ms", type=int, default=900, help="無音がこの長さ続いたら話し終わり")
    c.add_argument("--mic", action="store_true", help="最初からマイクで聞く")
    c.add_argument("--proactive-minutes", type=float, default=0, help="会話がこれだけ無かったらキャラから話しかける（0 で話しかけない）")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    if a.cmd == "character":
        print(ensure_character())
        return 0
    return {"launcher": run_launcher, "run": run}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
