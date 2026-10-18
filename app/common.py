"""maid のアプリで使う、設定（maid.settings.json）とアイコン（data\\icons\\）の読み書き。"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ICON_KINDS = ("character", "user")
ICON_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}
MAX_ICON_BYTES = 8_000_000


class Settings:
    """maid.settings.json（書いていない項目は config\\maid.settings.default.json の値）。"""

    def __init__(self, path: Path | None = None, defaults: Path | None = None):
        self.path = path or ROOT / "maid.settings.json"
        self.defaults_path = defaults or ROOT / "config" / "maid.settings.default.json"

    def _read(self, path: Path) -> dict:
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return {}

    def all(self) -> dict:
        return {**self._read(self.defaults_path), **self._read(self.path)}

    def update(self, values: dict) -> None:
        """変えた項目だけを書き足す（ほかの項目や順番はそのまま）。"""
        data = self._read(self.path)
        data.update(values)
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class Icons:
    def __init__(self, folder: Path):
        self.dir = folder

    def find(self, kind: str) -> Path | None:
        for ext in ICON_TYPES.values():
            p = self.dir / f"{kind}.{ext}"
            if p.exists():
                return p
        return None

    def data_url(self, kind: str) -> str | None:
        p = self.find(kind)
        if not p:
            return None
        mime = next(m for m, e in ICON_TYPES.items() if e == p.suffix[1:])
        return f"data:{mime};base64," + base64.b64encode(p.read_bytes()).decode()

    def save(self, kind: str, data_url: str) -> str:
        """画面で選んだ画像（data URL）を保存する。戻り値は表示用の data URL。"""
        if kind not in ICON_KINDS:
            raise ValueError(kind)
        m = re.match(r"data:(image/[a-z]+);base64,(.*)", data_url, re.S)
        if not m or m.group(1) not in ICON_TYPES:
            raise ValueError("PNG / JPEG / WebP / GIF の画像を選んでください")
        data = base64.b64decode(m.group(2))
        if len(data) > MAX_ICON_BYTES:
            raise ValueError("画像が大きすぎます（8MB まで）")
        self.remove(kind)
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / f"{kind}.{ICON_TYPES[m.group(1)]}").write_bytes(data)
        return self.data_url(kind)

    def remove(self, kind: str) -> None:
        while (p := self.find(kind)) is not None:
            p.unlink()
