"""services/winutil.py のテスト（Discord の通話判定。レジストリは偽物）。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "services"))


class FakeWinreg:
    """レジストリの CapabilityAccessManager を真似る。keys = {サブキー名: (開始, 終了)}"""
    HKEY_CURRENT_USER = object()

    def __init__(self, keys):
        self.keys = keys

    class _Key:
        def __init__(self, name):
            self.name = name

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    def OpenKey(self, parent, name):
        return self._Key(name)

    def EnumKey(self, key, i):
        names = list(self.keys)
        if i >= len(names):
            raise OSError
        return names[i]

    def QueryValueEx(self, key, value):
        start, stop = self.keys[key.name]
        return (start if value == "LastUsedTimeStart" else stop), 11


class DetectorTest(unittest.TestCase):
    def detect(self, keys):
        import winutil
        from unittest import mock

        with mock.patch.object(sys, "platform", "win32"), mock.patch.dict(sys.modules, {"winreg": FakeWinreg(keys)}):
            return winutil.discord_using_mic()

    def test_registry_detection(self):
        old = "C:#Users#me#AppData#Local#Discord#app-1.0.9100#Discord.exe"
        new = "C:#Users#me#AppData#Local#Discord#app-1.0.9200#Discord.exe"
        chrome = "C:#Program Files#Google#Chrome#Application#chrome.exe"
        self.assertFalse(self.detect({}))
        self.assertFalse(self.detect({old: (5, 6), chrome: (7, 0)}))  # 他のアプリが使用中でも反応しない
        self.assertTrue(self.detect({old: (5, 6), new: (8, 0)}))     # 新しい版の Discord が使用中
        self.assertTrue(self.detect({"C:#x#DiscordPTB#app-1#DiscordPTB.exe": (1, 0)}))
        self.assertFalse(self.detect({new: (0, 0)}))                 # 一度も使っていない

    def test_non_windows_returns_false(self):
        import winutil

        if sys.platform != "win32":
            self.assertFalse(winutil.discord_using_mic())

