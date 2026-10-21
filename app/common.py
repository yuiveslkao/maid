"""maid のアプリで使う、設定（maid.settings.json）・キャラ（character.yaml）・アイコン（data\\icons\\）の読み書き。"""

from __future__ import annotations

import base64
import json
import logging
import re
from pathlib import Path

log = logging.getLogger("common")

ROOT = Path(__file__).resolve().parent.parent
ICON_KINDS = ("character", "user")
ICON_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}
MAX_ICON_BYTES = 8_000_000
CHARACTER_KEYS = ("name", "human", "persona")
# 前に使っていた Open-LLM-VTuber の設定（キャラの設定をここから引っ越す）
OLD_OLV_CONF = ROOT / "runtime" / "Open-LLM-VTuber" / "conf.yaml"


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


def character_yaml(name: str, human: str, persona: str) -> str:
    body = "".join(f"  {line}\n" if line else "\n" for line in persona.strip("\n").splitlines())
    return ("# キャラの設定（name: キャラの名前 / human: あなたの名前 / persona: 性格・口調）\n\n"
            f"name: {json.dumps(name, ensure_ascii=False)}\n"
            f"human: {json.dumps(human, ensure_ascii=False)}\n\n"
            f"persona: |\n{body}")


def ensure_character(path: Path | None = None, default: Path | None = None, old_conf: Path | None = OLD_OLV_CONF) -> Path:
    """character.yaml が無ければ作る。前に Open-LLM-VTuber の conf.yaml に書いていたキャラがあれば、それを引き継ぐ。"""
    path = path or ROOT / "character.yaml"
    default = default or ROOT / "config" / "character.default.yaml"
    if path.exists():
        return path
    if old_conf and old_conf.exists():
        try:
            import yaml

            cc = (yaml.safe_load(old_conf.read_text(encoding="utf-8")) or {}).get("character_config") or {}
            if cc.get("persona_prompt"):
                path.write_text(character_yaml(str(cc.get("character_name") or "メイド"), str(cc.get("human_name") or "ご主人様"),
                                               str(cc["persona_prompt"])), encoding="utf-8")
                log.info("キャラの設定を %s から %s に引っ越しました", old_conf, path)
                return path
        except Exception as e:
            log.warning("前のキャラの設定を読めませんでした (%s): %s", old_conf, e)
    path.write_text(default.read_text(encoding="utf-8"), encoding="utf-8")
    return path


def load_character(path: Path | None = None) -> dict:
    """キャラの名前・相手の名前・性格（persona）。読めなければ簡単な既定値。"""
    out = {"name": "メイド", "human": "ご主人様", "persona": "あなたは親しみやすいアシスタントです。日本語の話し言葉で、短く答えてください。"}
    try:
        import yaml

        data = yaml.safe_load(ensure_character(path).read_text(encoding="utf-8")) or {}
        out.update({k: str(data[k]).strip() for k in CHARACTER_KEYS if data.get(k)})
    except Exception as e:
        log.warning("キャラの設定を読めませんでした: %s", e)
    return out


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
