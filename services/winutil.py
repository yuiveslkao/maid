"""Windows の小さな便利関数（Windows 以外では、PC を使っていて Discord の通話はしていない扱いにする）。"""

from __future__ import annotations

import sys

MIC_KEY = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone\NonPackaged"


def seconds_since_input() -> float:
    """キーボードかマウスを最後に触ってからの秒数。Windows 以外は 0。"""
    if sys.platform != "win32":
        return 0.0
    import ctypes

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    return ((ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0


def discord_using_mic() -> bool:
    """Discord（PTB / Canary 含む）がいまマイクを使っていれば True。Windows 以外は常に False。"""
    if sys.platform != "win32":
        return False
    import winreg

    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, MIC_KEY)
    except OSError:
        return False
    with root:
        i = 0
        while True:
            try:
                name = winreg.EnumKey(root, i)
            except OSError:
                return False
            i += 1
            # 例: C:#Users#me#AppData#Local#Discord#app-1.0.9200#Discord.exe
            if not name.lower().split("#")[-1].startswith("discord"):
                continue
            try:
                with winreg.OpenKey(root, name) as k:
                    stop, _ = winreg.QueryValueEx(k, "LastUsedTimeStop")
                    start, _ = winreg.QueryValueEx(k, "LastUsedTimeStart")
            except OSError:
                continue
            if stop == 0 and start != 0:  # 使用開始していて、まだ終わっていない
                return True
