"""maid のアプリ: タスクトレイのアイコン・デスクトップのキャラ・チャットのウィンドウ。

画面は WebView2（Windows に標準で入っている Edge の部品）で表示する。今は中身を Open-LLM-VTuber の画面に頼っている
（チャットのウィンドウ = Open-LLM-VTuber の画面。キャラのウィンドウ = frontend/maid-vrm/character.html）。

- キャラのウィンドウ: 枠なし・背景透明。ほかのウィンドウの後ろ（デスクトップの上）に置き、クリックは後ろへすり抜ける。
  キャラの声は Discord ゲートから写しを受け取って、このウィンドウで鳴らす（口の動きのため）。
- チャットのウィンドウ: 閉じてもアプリは終わらない（タスクトレイから開き直せる）。
- タスクトレイ: チャットを開く / キャラを出す・隠す / いつも手前に / 終了。終了すると maid 全体が止まる（start.bat が後片付けする）。

    python app/maid_app.py --chat-url http://127.0.0.1:12393/ --character-url http://127.0.0.1:12393/maid-vrm/character.html
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import os
import sys
import threading
import time

log = logging.getLogger("maid_app")

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


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="maid のアプリ")
    p.add_argument("--chat-url", required=True)
    p.add_argument("--character-url", default="", help="空ならキャラのウィンドウを出さない")
    p.add_argument("--storage", default=None, help="画面の設定やマイクの許可を保存するフォルダ")
    p.add_argument("--width", type=int, default=420, help="キャラのウィンドウの幅")
    p.add_argument("--height", type=int, default=720, help="キャラのウィンドウの高さ")
    p.add_argument("--on-top", action="store_true", help="キャラをいつも手前に出す")
    p.add_argument("--no-click-through", action="store_true", help="キャラのウィンドウでクリックをすり抜けさせない")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

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


if __name__ == "__main__":
    sys.exit(main())
