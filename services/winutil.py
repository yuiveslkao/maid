"""Windows の小さな便利関数（Windows 以外では、いつも「使っている」扱いにする）。"""

from __future__ import annotations

import sys


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
