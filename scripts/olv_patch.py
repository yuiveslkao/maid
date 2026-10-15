r"""maid が Open-LLM-VTuber (v1.2.1) に当てる修正をまとめたもの。何度実行しても同じ結果になる。

本体は改造しない方針だが、設定で変えられない部分だけここで直す:

    prepare       起動のたびに実行する:
                    - 画面の初期設定 (maid-defaults.js) を書き、画面のマイク設定の不具合を直す
                    - conf.yaml を maid の形にそろえる（使わない設定を消し、音声認識を ReazonSpeech に。キャラの設定はそのまま）
    extract-asr   ReazonSpeech のアーカイブから必要なファイルだけを取り出す

Open-LLM-VTuber のライブラリ（ruamel.yaml）を使うので、その Python で動かす:

    runtime\Open-LLM-VTuber\.venv\Scripts\python.exe scripts\olv_patch.py prepare --olv runtime\Open-LLM-VTuber --ws-url ws://127.0.0.1:12394/client-ws
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tarfile
from pathlib import Path

# ------------------------------------------------------------------ 画面

FRONTEND_JS = """// maid が起動のたびに書き換えるファイル（scripts/olv_patch.py）
(function () {{
  try {{
    var ls = window.localStorage;
    ls.setItem("wsUrl", JSON.stringify({ws_url}));
    ls.setItem("baseUrl", JSON.stringify({base_url}));
    // マイクは止めない（毎回入れ直す。画面の設定を開いて閉じると古い値で上書きされる不具合があるため）
    ls.setItem("micOn", "true");
    ls.setItem("autoStopMic", "false");            // 話し終わっても・キャラが話していてもマイクを止めない
    ls.setItem("autoStartMicOn", "true");          // 割り込んだあともマイクを戻す
    ls.setItem("autoStartMicOnConvEnd", "true");   // 返事が終わったらマイクを戻す
    // 話し終わりの判定（maid.settings.json の silence_ms）。感度は画面で変えた値を残す
    var vad = {{}};
    try {{ vad = JSON.parse(ls.getItem("vadSettings") || "{{}}") || {{}}; }} catch (e) {{}}
    if (typeof vad.positiveSpeechThreshold !== "number") vad.positiveSpeechThreshold = 50;
    if (typeof vad.negativeSpeechThreshold !== "number") vad.negativeSpeechThreshold = 35;
    vad.redemptionFrames = {redemption_frames};
    ls.setItem("vadSettings", JSON.stringify(vad));
  }} catch (e) {{}}
}})();
"""

# 画面の本体 (assets/main-*.js) のマイク設定の初期値を「止めない」に直す。
# 初期値が「止める」のままだと、設定画面を閉じたときにその値で上書き保存されてしまう（v1.2.1 の不具合）
BUNDLE_FIXES = [
    ('useRef(!0),[st,it]=useLocalStorage("autoStopMic"', 'useRef(!1),[st,it]=useLocalStorage("autoStopMic"'),
    ('useRef(!1),[at,lt]=useLocalStorage("autoStartMicOnConvEnd"', 'useRef(!0),[at,lt]=useLocalStorage("autoStartMicOnConvEnd"'),
    ("ut=reactExports.useRef(!1),[,ct]=reactExports.useReducer", "ut=reactExports.useRef(!0),[,ct]=reactExports.useReducer"),
]
BUNDLE_VERSION = "3"  # 本体を書き換えたら上げる（Edge に古い本体を使い回させないため）
VAD_FRAME_MS = 32


def _write_if_changed(path: Path, text: str) -> bool:
    old = path.read_text(encoding="utf-8") if path.exists() else None
    if old == text:
        return False
    path.write_text(text, encoding="utf-8", newline="")
    return True


def patch_frontend(front: Path, ws_url: str, base_url: str, silence_ms: int) -> list[str]:
    """変更したファイル名の一覧を返す。"""
    index = front / "index.html"
    if not index.exists():
        raise FileNotFoundError(f"{index} がありません（setup.bat を実行してください）")
    changed = []
    frames = max(3, round(silence_ms / VAD_FRAME_MS))
    js = FRONTEND_JS.format(ws_url=json.dumps(ws_url), base_url=json.dumps(base_url), redemption_frames=frames)
    if _write_if_changed(front / "maid-defaults.js", js):
        changed.append("maid-defaults.js")

    html = index.read_text(encoding="utf-8")
    new_html = html
    if "maid-defaults.js" not in new_html:
        new_html = new_html.replace("<head>", '<head>\n    <script src="./maid-defaults.js"></script>', 1)

    bundles = sorted((front / "assets").glob("main-*.js"))
    if bundles:
        code = bundles[0].read_text(encoding="utf-8")
        fixed = code
        for old, new in BUNDLE_FIXES:
            fixed = fixed.replace(old, new)
        if _write_if_changed(bundles[0], fixed):
            changed.append(bundles[0].name)
        new_html = re.sub(r'(assets/main-[A-Za-z0-9_-]+\.js)(\?maid=\d+)?"', rf'\1?maid={BUNDLE_VERSION}"', new_html)

    if _write_if_changed(index, new_html):
        changed.append("index.html")
    return changed


# ------------------------------------------------------------------ conf.yaml

REAZON_NAME = "sherpa-onnx-zipformer-ja-reazonspeech-2024-08-01"
REAZON_FILES = ("encoder-epoch-99-avg-1.int8.onnx", "decoder-epoch-99-avg-1.int8.onnx",
                "joiner-epoch-99-avg-1.int8.onnx", "tokens.txt")

# 音声認識は ReazonSpeech（日本語専用, CPU）に固定する
REAZON_ASR = {
    "model_type": "transducer",
    "encoder": f"./models/{REAZON_NAME}/{REAZON_FILES[0]}",
    "decoder": f"./models/{REAZON_NAME}/{REAZON_FILES[1]}",
    "joiner": f"./models/{REAZON_NAME}/{REAZON_FILES[2]}",
    "tokens": f"./models/{REAZON_NAME}/tokens.txt",
    "use_itn": False,
}

# 利用者が書き換える部分（作り直すときに引き継ぐ）
CHARACTER_KEYS = ("conf_name", "conf_uid", "live2d_model_name", "avatar", "character_name", "human_name", "persona_prompt")
# maid で使う部分。これ以外の項目がある conf.yaml は、雛形から作り直す
MAID_SHAPE = {
    ("character_config", "agent_config", "agent_settings"): {"basic_memory_agent"},
    ("character_config", "agent_config", "llm_configs"): {"ollama_llm"},
    ("character_config", "asr_config"): {"asr_model", "sherpa_onnx_asr"},
    ("character_config", "tts_config"): {"tts_model", "openai_tts"},
}


def _yaml():
    from ruamel.yaml import YAML

    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096
    return yaml


def _get(data, path):
    for key in path:
        data = data.get(key) if hasattr(data, "get") else None
    return data


def is_maid_shape(data) -> bool:
    return "live_config" not in data and all(set(_get(data, path) or {}) <= keep for path, keep in MAID_SHAPE.items())


def patch_conf(conf: Path, template: Path) -> str:
    """conf.yaml を maid の形にそろえる。戻り値は "rebuilt"（作り直した）/ "asr"（音声認識だけ直した）/ ""（変更なし）。

    - 使わない設定（ほかの頭脳・音声認識・声など）が入っていたら、雛形から作り直してキャラの設定だけ引き継ぐ。
      元のファイルは conf.yaml.before-maid に残す。
    - 音声認識は ReazonSpeech に固定する。
    """
    yaml = _yaml()
    with conf.open(encoding="utf-8") as f:
        data = yaml.load(f)
    result = ""
    if not is_maid_shape(data):
        with template.open(encoding="utf-8") as f:
            fresh = yaml.load(f)
        for key in CHARACTER_KEYS:
            if key in data["character_config"]:
                fresh["character_config"][key] = data["character_config"][key]
        conf.with_name(conf.name + ".before-maid").write_bytes(conf.read_bytes())
        data = fresh
        result = "rebuilt"

    asr = data["character_config"]["asr_config"]
    sherpa = asr.setdefault("sherpa_onnx_asr", {})
    wrong = {k: v for k, v in REAZON_ASR.items() if sherpa.get(k) != v}
    if wrong or asr.get("asr_model") != "sherpa_onnx_asr" or "sense_voice" in sherpa:
        asr["asr_model"] = "sherpa_onnx_asr"
        sherpa.pop("sense_voice", None)
        sherpa.update(wrong)
        result = result or "asr"
    if result:
        with conf.open("w", encoding="utf-8", newline="\n") as f:
            yaml.dump(data, f)
    return result


def extract_reazon(archive: Path, dest: Path) -> None:
    """配布アーカイブ（約 700MB）から int8 版のモデルと tokens だけを取り出す（約 160MB）。"""
    dest.mkdir(parents=True, exist_ok=True)
    found = set()
    with tarfile.open(archive, "r:bz2") as t:
        for m in t:
            name = os.path.basename(m.name)
            if m.isfile() and name in REAZON_FILES:
                with t.extractfile(m) as src, (dest / name).open("wb") as out:
                    out.write(src.read())
                found.add(name)
    missing = set(REAZON_FILES) - found
    if missing:
        raise RuntimeError(f"アーカイブに必要なファイルがありません: {', '.join(sorted(missing))}")


# ------------------------------------------------------------------ コマンド

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Open-LLM-VTuber への maid の修正")
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("prepare")
    f.add_argument("--olv", required=True)
    f.add_argument("--ws-url", required=True)
    f.add_argument("--base-url", default="http://127.0.0.1:12393")
    f.add_argument("--silence-ms", type=int, default=900)
    f.add_argument("--template", default=str(Path(__file__).resolve().parent.parent / "config" / "open-llm-vtuber" / "conf.yaml"))
    e = sub.add_parser("extract-asr")
    e.add_argument("archive")
    e.add_argument("--olv", required=True)
    args = p.parse_args(argv)

    olv = Path(args.olv)
    if args.cmd == "prepare":
        changed = patch_frontend(olv / "frontend", args.ws_url, args.base_url, args.silence_ms)
        if changed:
            print("画面の設定を更新しました: " + ", ".join(changed))
        result = patch_conf(olv / "conf.yaml", Path(args.template))
        if result == "rebuilt":
            print("conf.yaml から使わない設定を消しました（キャラの設定はそのまま。元のファイルは conf.yaml.before-maid）")
        elif result:
            print("conf.yaml の音声認識を ReazonSpeech に直しました")
    elif args.cmd == "extract-asr":
        extract_reazon(Path(args.archive), olv / "models" / REAZON_NAME)
        print("音声認識モデルを展開しました")
    return 0


if __name__ == "__main__":
    sys.exit(main())
