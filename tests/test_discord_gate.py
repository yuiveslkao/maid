"""ゲートのテスト。偽の Open-LLM-VTuber 本体を立てて、通話中に何が止まるかを確かめる。

websockets が必要: uv run --no-project --with "websockets>=13" python -m unittest discover -s tests
"""

import asyncio
import json
import sys
import unittest
from pathlib import Path

try:
    from websockets.asyncio.client import connect
    from websockets.asyncio.server import serve
except ImportError:  # pragma: no cover
    connect = None

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gate"))


@unittest.skipIf(connect is None, "websockets なし")
class GateTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import discord_gate

        self.received = []
        self.paths = []

        async def backend(ws):
            self.paths.append(ws.request.path)
            await ws.send(json.dumps({"type": "full-text", "text": "Connection established"}))
            async for msg in ws:
                self.received.append(json.loads(msg)["type"])

        self.backend = await serve(backend, "127.0.0.1", 0)
        port = self.backend.sockets[0].getsockname()[1]
        self.call = False
        self.gate = discord_gate.Gate(f"ws://127.0.0.1:{port}", lambda: self.call, poll_seconds=0.02)
        self.gate_server = await serve(self.gate.handle, "127.0.0.1", 0)
        self.gate_port = self.gate_server.sockets[0].getsockname()[1]
        self.watcher = asyncio.create_task(self.gate.watch())

    async def asyncTearDown(self):
        self.watcher.cancel()
        self.gate_server.close()
        self.backend.close()

    async def set_call(self, value):
        self.call = value
        await asyncio.sleep(0.1)

    async def send(self, ws, *types):
        for t in types:
            await ws.send(json.dumps({"type": t, "audio": [0.1]}))
        await asyncio.sleep(0.1)

    async def test_passes_everything_when_not_in_call(self):
        async with connect(f"ws://127.0.0.1:{self.gate_port}/client-ws?client_uid=x") as ws:
            self.assertEqual(json.loads(await ws.recv())["text"], "Connection established")
            await self.send(ws, "mic-audio-data", "mic-audio-end", "text-input", "ai-speak-signal")
        self.assertEqual(self.received, ["mic-audio-data", "mic-audio-end", "text-input", "ai-speak-signal"])
        self.assertEqual(self.paths, ["/client-ws?client_uid=x"])

    async def test_blocks_voice_and_proactive_in_call_but_not_chat(self):
        async with connect(f"ws://127.0.0.1:{self.gate_port}/client-ws") as ws:
            await ws.recv()
            await self.set_call(True)
            notice = json.loads(await ws.recv())
            self.assertIn("Discord 通話中", notice["text"])
            await self.send(ws, "mic-audio-data", "mic-audio-data", "mic-audio-end")
            # 画面が「考え中」で止まらないよう、会話終了の合図が返る
            self.assertEqual(json.loads(await ws.recv()), {"type": "control", "text": "conversation-chain-end"})
            await self.send(ws, "ai-speak-signal", "text-input", "interrupt-signal")
        self.assertEqual(self.received, ["text-input", "interrupt-signal"])

    async def test_utterance_is_not_split_when_call_state_changes(self):
        async with connect(f"ws://127.0.0.1:{self.gate_port}/client-ws") as ws:
            await ws.recv()
            await self.send(ws, "mic-audio-data")
            await self.set_call(True)   # 発話の途中で通話開始 → この発話は最後まで通す
            await self.send(ws, "mic-audio-data", "mic-audio-end")
            await self.send(ws, "mic-audio-data")
            await self.set_call(False)  # 通話中に始まった発話は最後まで止める
            await self.send(ws, "mic-audio-data", "mic-audio-end")
            await self.send(ws, "mic-audio-data", "mic-audio-end")  # 通話後は通常どおり
        self.assertEqual(self.received, ["mic-audio-data"] * 2 + ["mic-audio-end"] + ["mic-audio-data", "mic-audio-end"])


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
        import discord_gate
        from unittest import mock

        with mock.patch.object(sys, "platform", "win32"), mock.patch.dict(sys.modules, {"winreg": FakeWinreg(keys)}):
            return discord_gate.discord_using_mic()

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
        import discord_gate

        if sys.platform != "win32":
            self.assertFalse(discord_gate.discord_using_mic())


if __name__ == "__main__":
    unittest.main()
