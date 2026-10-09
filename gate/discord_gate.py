"""Discord で通話中は、キャラが音声に反応しないようにする中継（ゲート）。

画面（Open-LLM-VTuber のデスクトップアプリ / ブラウザ）と Open-LLM-VTuber 本体の間の
WebSocket を中継し、Discord がマイクを使っている間だけ「マイク音声」と「自発発話の合図」を
本体に渡さない。チャット入力はそのまま通す。本体は改造しない。

    uv run --no-project --with "websockets>=13" python gate/discord_gate.py
    uv run --no-project --with "websockets>=13" python gate/discord_gate.py --check   # 判定だけ確認

画面の設定で WebSocket URL を ws://127.0.0.1:12394/client-ws に変える（Base URL はそのまま）。

Discord の通話判定には、Windows がアプリごとのマイク使用状況を記録しているレジストリ
（タスクバーのマイク表示と同じ情報）を使う。Discord は通話中だけマイクを開くので、これで判定できる。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Callable

from websockets.asyncio.client import connect
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

log = logging.getLogger("discord_gate")

MIC_KEY = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone\NonPackaged"
AUDIO_TYPES = {"mic-audio-data", "raw-audio-data"}


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


class Gate:
    def __init__(self, backend_url: str, detector: Callable[[], bool], block_proactive: bool = True,
                 poll_seconds: float = 1.0):
        self.backend_url = backend_url
        self.detector = detector
        self.block_proactive = block_proactive
        self.poll_seconds = poll_seconds
        self.in_call = False
        self.clients: set[ServerConnection] = set()

    def refresh(self) -> bool:
        try:
            self.in_call = bool(self.detector())
        except Exception as e:  # 判定に失敗したら通す（会話できなくなるよりまし）
            log.warning("Discord の判定に失敗: %s", e)
            self.in_call = False
        return self.in_call

    async def watch(self) -> None:
        """通話状態を定期的に調べ、変わったら画面に知らせる。"""
        while True:
            before = self.in_call
            if self.refresh() != before:
                text = "Discord 通話中なので、声には反応しません" if self.in_call else "Discord 通話が終わったので、また聞いています"
                log.info(text)
                for ws in list(self.clients):
                    try:
                        await ws.send(json.dumps({"type": "full-text", "text": text}, ensure_ascii=False))
                    except ConnectionClosed:
                        pass
            await asyncio.sleep(self.poll_seconds)

    async def handle(self, client: ServerConnection) -> None:
        path = client.request.path if client.request else "/client-ws"
        self.clients.add(client)
        try:
            async with connect(self.backend_url.rstrip("/") + path, max_size=None) as backend:
                await asyncio.gather(self._client_to_backend(client, backend), self._backend_to_client(backend, client))
        except (ConnectionClosed, OSError) as e:
            log.info("接続終了: %s", e)
        finally:
            self.clients.discard(client)

    async def _backend_to_client(self, backend, client) -> None:
        try:
            async for msg in backend:
                await client.send(msg)
        finally:
            await client.close()

    async def _client_to_backend(self, client, backend) -> None:
        # 1 回の発話（音声データ…→ mic-audio-end）の途中で通話状態が変わっても、
        # 発話の頭で決めた「通す / 止める」を最後まで守る（本体に中途半端な音声が残らないように）
        utterance_blocked: bool | None = None
        try:
            async for raw in client:
                msg_type = None
                if isinstance(raw, str):
                    try:
                        msg_type = json.loads(raw).get("type")
                    except (ValueError, AttributeError):
                        pass

                if msg_type in AUDIO_TYPES:
                    if utterance_blocked is None:
                        utterance_blocked = self.in_call
                    if utterance_blocked:
                        continue
                elif msg_type == "mic-audio-end":
                    blocked = self.in_call if utterance_blocked is None else utterance_blocked
                    utterance_blocked = None
                    if blocked:
                        # 画面が「考え中」のまま止まらないよう、会話が終わった合図を返す
                        await client.send(json.dumps({"type": "control", "text": "conversation-chain-end"}))
                        continue
                elif msg_type == "ai-speak-signal" and self.block_proactive and self.in_call:
                    continue

                await backend.send(raw)
        finally:
            await backend.close()


async def run(host: str, port: int, gate: Gate) -> None:
    gate.refresh()
    async with serve(gate.handle, host, port, max_size=None):
        log.info("ゲート起動: ws://%s:%d/client-ws  →  %s  （Discord 通話中: %s）",
                 host, port, gate.backend_url, "はい" if gate.in_call else "いいえ")
        await gate.watch()


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Discord 通話中は音声入力を止める WebSocket 中継")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=12394)
    p.add_argument("--backend", default="ws://127.0.0.1:12393", help="Open-LLM-VTuber 本体")
    p.add_argument("--allow-proactive", action="store_true", help="通話中もキャラからの自発発話を許す")
    p.add_argument("--check", action="store_true", help="いま Discord がマイクを使っているかだけ表示して終わる")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    if a.check:
        print("Discord はマイクを使用中（通話中）" if discord_using_mic() else "Discord はマイクを使っていません")
        return
    gate = Gate(a.backend, discord_using_mic, block_proactive=not a.allow_proactive)
    try:
        asyncio.run(run(a.host, a.port, gate))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
