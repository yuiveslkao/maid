"""maid のアプリ。画面は WebView2（Windows に標準で入っている Edge の部品）で表示する。

    python app/maid_app.py launcher ...   起動の画面（どれで起動するか・使うもの・アイコンを選ぶ）
    python app/maid_app.py chat ...       チャットだけの版（app/chat.py。Open-LLM-VTuber を使わない）
    python app/maid_app.py full ...       フル版: タスクトレイ・デスクトップのキャラ・チャットのウィンドウ

フル版:
- キャラのウィンドウ: 枠なし・背景透明。ほかのウィンドウの後ろ（デスクトップの上）に置き、クリックは後ろへすり抜ける。
  キャラの声は Discord ゲートから写しを受け取って、このウィンドウで鳴らす（口の動きのため）。
- チャットのウィンドウ: 今は Open-LLM-VTuber の画面。閉じてもアプリは終わらない（タスクトレイから開き直せる）。
- タスクトレイ: チャットを開く / キャラを出す・隠す / いつも手前に / 終了。終了すると maid 全体が止まる（start.bat が後片付けする）。

起動の画面の終わり方（終了コード）: 0 = やめる / 10 = チャットだけ / 11 = フル
"""

from __future__ import annotations

import argparse
import ctypes
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
from chat import ChatSession, Speaker, load_character, proactive_loop  # noqa: E402
from common import ROOT, Icons, Settings  # noqa: E402
from winutil import discord_using_mic, seconds_since_input  # noqa: E402

log = logging.getLogger("maid_app")
UI = Path(__file__).resolve().parent / "ui"
EXIT_CHAT, EXIT_FULL = 10, 11

# Win32
GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x20      # クリックをすり抜ける（WS_EX_LAYERED と一緒に使う）
WS_EX_TOOLWINDOW = 0x80       # タスクバーに出さない
WS_EX_LAYERED = 0x80000
WS_EX_APPWINDOW = 0x40000
WS_EX_NOACTIVATE = 0x8000000
LWA_ALPHA = 0x2
HWND_TOPMOST, HWND_NOTOPMOST, HWND_BOTTOM = -1, -2, 1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x1, 0x2, 0x10, 0x40
SPI_GETWORKAREA = 0x30


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def work_area() -> RECT:
    """タスクバーを除いた、メインの画面の範囲（実際のピクセル）。"""
    r = RECT()
    ctypes.windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(r), 0)
    return r


class Character:
    """デスクトップのキャラのウィンドウ（Win32 の細かい設定はここで行う）。"""

    def __init__(self, window, width: int, height: int, click_through: bool, on_top: bool):
        self.window = window
        self.width, self.height = width, height
        self.click_through = click_through
        self.on_top = on_top
        self.visible = True
        self.hwnd = 0

    def setup(self) -> None:
        user32 = ctypes.windll.user32
        self.hwnd = self.window.native.Handle.ToInt32()
        style = user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
        style = (style | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE) & ~WS_EX_APPWINDOW
        if self.click_through:
            style |= WS_EX_LAYERED | WS_EX_TRANSPARENT
        user32.SetWindowLongW(self.hwnd, GWL_EXSTYLE, style)
        if self.click_through:
            user32.SetLayeredWindowAttributes(self.hwnd, 0, 255, LWA_ALPHA)  # 見た目は変えずに、クリックだけすり抜ける
        # 画面の右下（タスクバーの上）に置く。大きさは画面の拡大率に合わせる
        scale = user32.GetDpiForWindow(self.hwnd) / 96 if hasattr(user32, "GetDpiForWindow") else 1.0
        w, h = int(self.width * scale), int(self.height * scale)
        area = work_area()
        user32.SetWindowPos(self.hwnd, 0, area.right - w, area.bottom - h, w, h, SWP_NOACTIVATE | SWP_SHOWWINDOW)
        self.apply_z()

    def apply_z(self) -> None:
        if self.hwnd and self.visible:
            after = HWND_TOPMOST if self.on_top else HWND_BOTTOM
            ctypes.windll.user32.SetWindowPos(self.hwnd, after, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    def keep_behind(self, stop: threading.Event) -> None:
        """ほかのウィンドウの後ろに居続ける（何かの拍子に前に出たら戻す）。"""
        while not stop.wait(1.0):
            if not self.on_top:
                self.apply_z()

    def set_on_top(self, value: bool) -> None:
        self.on_top = value
        if not value and self.hwnd:  # いつも手前、を外す
            ctypes.windll.user32.SetWindowPos(self.hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        self.apply_z()

    def toggle(self) -> None:
        self.visible = not self.visible
        (self.window.show if self.visible else self.window.hide)()
        if self.visible:
            self.apply_z()


def tray_image():
    """タスクトレイのアイコン（ピンクの丸）。"""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=(255, 140, 180, 255), outline=(255, 255, 255, 255), width=3)
    d.ellipse((20, 24, 28, 32), fill=(80, 40, 60, 255))
    d.ellipse((36, 24, 44, 32), fill=(80, 40, 60, 255))
    d.arc((22, 30, 42, 46), 20, 160, fill=(80, 40, 60, 255), width=3)
    return img


def run_full(a) -> int:
    # キャラのウィンドウは一度もクリックされないので、声を自動で鳴らせるようにする
    os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = "--autoplay-policy=no-user-gesture-required"
    import pystray
    import webview

    stop = threading.Event()
    chat = webview.create_window("maid", a.chat_url, width=1100, height=820)

    def on_closing():
        chat.hide()  # 閉じてもアプリは続ける（タスクトレイから開き直す）
        return False

    chat.events.closing += on_closing

    character = None
    if a.character_url:
        win = webview.create_window("maid-character", a.character_url, width=a.width, height=a.height,
                                    frameless=True, transparent=True, focus=False, resizable=False,
                                    on_top=a.on_top, easy_drag=False)
        character = Character(win, a.width, a.height, not a.no_click_through, a.on_top)

        def on_shown():
            try:
                character.setup()
            except Exception:
                log.exception("キャラのウィンドウの設定に失敗しました")
            threading.Thread(target=character.keep_behind, args=(stop,), daemon=True).start()

        win.events.shown += on_shown

    def quit_app(icon, _item=None):
        log.info("終了します")
        stop.set()
        icon.stop()
        for w in list(webview.windows):
            w.destroy()

    def open_chat(_icon=None, _item=None):
        chat.show()
        chat.restore()

    items = [pystray.MenuItem("チャットを開く", open_chat, default=True)]
    if character:
        items += [
            pystray.MenuItem("キャラを出す / 隠す", lambda icon, item: character.toggle()),
            pystray.MenuItem("いつも手前に出す", lambda icon, item: character.set_on_top(not character.on_top),
                             checked=lambda item: character.on_top),
        ]
    items += [pystray.Menu.SEPARATOR, pystray.MenuItem("終了", quit_app)]
    icon = pystray.Icon("maid", tray_image(), "maid", pystray.Menu(*items))
    threading.Thread(target=icon.run, daemon=True).start()

    webview.start(private_mode=False, storage_path=a.storage)
    stop.set()
    icon.stop()
    # 少し待ってから終わる（トレイのアイコンを消す時間）
    time.sleep(0.3)
    return 0


def run_launcher(a) -> int:
    import webview

    settings, icons = Settings(), Icons(Path(a.data) / "icons")
    character = load_character(Path(a.conf) if a.conf else None)
    result = {"code": 0}
    holder: dict = {}

    class Api:
        def load(self):
            s = settings.all()
            return {"mode": s.get("mode", "chat"), "voice_reply": bool(s.get("voice_reply")), "mic": bool(s.get("mic", True)),
                    "search": s.get("search", "auto"), "memory": s.get("memory", True) is not False,
                    "proactive_minutes": float(s.get("proactive_minutes") or 0), "vrm": s.get("vrm", ""),
                    "name": character["name"], "icons": {k: icons.data_url(k) for k in ("character", "user")}}

        def save_icon(self, kind, data_url):
            return icons.save(kind, data_url)

        def remove_icon(self, kind):
            icons.remove(kind)

        def pick_vrm(self):
            dialog = getattr(getattr(webview, "FileDialog", None), "OPEN", None) or webview.OPEN_DIALOG
            picked = holder["window"].create_file_dialog(dialog, file_types=("VRM (*.vrm)",))
            if not picked:
                return ""
            path = Path(picked[0])
            try:  # maid フォルダの中なら、maid フォルダからの場所で書く
                return path.resolve().relative_to(ROOT).as_posix()
            except ValueError:
                return path.as_posix()

        def launch(self, choice):
            keys = ("mode", "voice_reply", "mic", "search", "memory", "proactive_minutes", "vrm")
            settings.update({k: choice[k] for k in keys if k in choice})
            result["code"] = EXIT_FULL if choice.get("mode") == "full" else EXIT_CHAT
            holder["window"].destroy()

        def quit(self):
            holder["window"].destroy()

    holder["window"] = webview.create_window(f"{character['name']} を起動", url=str(UI / "launcher.html"), js_api=Api(),
                                             width=580, height=820)
    webview.start(private_mode=False, storage_path=a.storage)
    return result["code"]


def run_chat(a) -> int:
    os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = "--autoplay-policy=no-user-gesture-required"  # キャラから話しかけた声も鳴らす
    import webview

    character = load_character(Path(a.conf) if a.conf else None)
    icons = Icons(Path(a.data) / "icons")
    session = ChatSession(a.proxy, character, Path(a.data) / "chat.json")
    state = {"voice": bool(a.voice and a.tts), "reply": 0}
    holder: dict = {}
    stop = threading.Event()

    def js(fn: str, *args) -> None:
        window = holder.get("window")
        if window:
            window.evaluate_js(f"window.maid && maid.{fn}({','.join(json.dumps(x, ensure_ascii=False) for x in args)})")

    speaker = Speaker(a.tts, lambda b64: js("audio", b64)) if a.tts else None

    def reply(text: str, proactive: bool = False) -> None:
        # 声で割り込まれたら、前の返事の続きは画面にも声にも出さない
        state["reply"] += 1
        rid = state["reply"]
        if speaker:
            speaker.reset()
        if proactive:
            js("proactive")

        def chunk(piece: str) -> None:
            if rid != state["reply"]:
                return
            js("chunk", piece)
            if speaker and state["voice"]:
                speaker.feed(piece)

        session.send(text, chunk, proactive=proactive)
        if rid != state["reply"]:
            return
        if speaker and state["voice"]:
            speaker.feed("", final=True)
        js("done")

    def heard(text: str) -> None:
        """声で話しかけられた。返事の途中なら止めて、聞こえた文を送る。"""
        session.cancel.set()
        if speaker:
            speaker.reset()
        js("heard", text)
        threading.Thread(target=reply, args=(text,), daemon=True).start()

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

        def send(self, text):
            threading.Thread(target=reply, args=(str(text),), daemon=True).start()

        def stop(self):
            session.cancel.set()
            if speaker:
                speaker.reset()

        def set_voice(self, on):
            state["voice"] = bool(on and a.tts)
            if speaker and not state["voice"]:
                speaker.reset()

        def set_mic(self, on):
            if listener:
                listener.set_enabled(on)
                return listener.state

        def open_url(self, url):
            if str(url).startswith(("http://", "https://")):
                webbrowser.open(url)

        def clear(self):
            session.clear()

    threading.Thread(target=proactive_loop, args=(session, a.proactive_minutes, lambda: reply("", proactive=True),
                                                  seconds_since_input, stop), daemon=True).start()
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
    common.add_argument("--conf", default=str(ROOT / "runtime" / "Open-LLM-VTuber" / "conf.yaml"), help="キャラの設定")

    sub.add_parser("launcher", parents=[common])
    c = sub.add_parser("chat", parents=[common])
    c.add_argument("--proxy", default="http://127.0.0.1:11434", help="頭脳の中継")
    c.add_argument("--tts", default="", help="声の中継（空なら声で返事しない）")
    c.add_argument("--voice", action="store_true", help="最初から声で返事する")
    c.add_argument("--asr-dir", default="", help="音声認識のモデル（ReazonSpeech）のフォルダ。空なら声で話しかけられない")
    c.add_argument("--vad", default="", help="話しはじめと話し終わりを見つけるモデル（silero_vad.onnx）")
    c.add_argument("--silence-ms", type=int, default=900, help="無音がこの長さ続いたら話し終わり")
    c.add_argument("--mic", action="store_true", help="最初からマイクで聞く")
    c.add_argument("--proactive-minutes", type=float, default=0, help="会話がこれだけ無かったらキャラから話しかける（0 で話しかけない）")
    f = sub.add_parser("full", parents=[common])
    f.add_argument("--chat-url", required=True)
    f.add_argument("--character-url", default="", help="空ならキャラのウィンドウを出さない")
    f.add_argument("--width", type=int, default=420, help="キャラのウィンドウの幅")
    f.add_argument("--height", type=int, default=720, help="キャラのウィンドウの高さ")
    f.add_argument("--on-top", action="store_true", help="キャラをいつも手前に出す")
    f.add_argument("--no-click-through", action="store_true", help="キャラのウィンドウでクリックをすり抜けさせない")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    return {"launcher": run_launcher, "chat": run_chat, "full": run_full}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
