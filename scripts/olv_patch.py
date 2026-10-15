r"""maid が Open-LLM-VTuber (v1.2.1) に当てる修正をまとめたもの。何度実行しても同じ結果になる。

本体は改造しない方針だが、設定で変えられない部分だけここで直す:

    prepare       起動のたびに実行する:
                    - 画面の初期設定 (maid-defaults.js) を書き、画面のマイク設定の不具合を直す
                    - conf.yaml を maid の形にそろえる（使わない設定を消し、音声認識を ReazonSpeech に。キャラの設定はそのまま）
                    - 3D モデル (VRM) を表示する設定（frontend/maid-vrm.js を画面に読み込ませる）
    extract-vrm-libs  3D 表示に使うライブラリ（three.js / three-vrm）を npm の配布物から取り出す
    extract-asr   ReazonSpeech のアーカイブから必要なファイルだけを取り出す

Open-LLM-VTuber のライブラリ（ruamel.yaml）を使うので、その Python で動かす:

    runtime\Open-LLM-VTuber\.venv\Scripts\python.exe scripts\olv_patch.py prepare --olv runtime\Open-LLM-VTuber --ws-url ws://127.0.0.1:12394/client-ws
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

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
// 3D モデル（maid.settings.json の vrm。null なら Live2D のまま）
window.MAID_VRM = {vrm};
"""

# 3D 表示（frontend/maid-vrm.js）が使うライブラリ。npm の配布物（.tgz）から必要なファイルだけを取り出す
VRM_DIR = "maid-vrm"
VRM_LIBS = {
    "three": {
        "package/build/three.module.js": "three.module.js",
        "package/build/three.core.js": "three.core.js",
        "package/examples/jsm/loaders/GLTFLoader.js": "addons/loaders/GLTFLoader.js",
        "package/examples/jsm/utils/BufferGeometryUtils.js": "addons/utils/BufferGeometryUtils.js",
        "package/examples/jsm/utils/SkeletonUtils.js": "addons/utils/SkeletonUtils.js",
        "package/LICENSE": "LICENSE-three.txt",
    },
    "three-vrm": {
        "package/lib/three-vrm.module.min.js": "three-vrm.module.min.js",
        "package/LICENSE": "LICENSE-three-vrm.txt",
    },
}
IMPORT_MAP = {"imports": {"three": f"./{VRM_DIR}/lib/three.module.js", "three/addons/": f"./{VRM_DIR}/lib/addons/",
                          "@pixiv/three-vrm": f"./{VRM_DIR}/lib/three-vrm.module.min.js"}}
VRM_BLOCK_RE = re.compile(r"\n?\s*<!-- maid-vrm -->.*?<!-- /maid-vrm -->", re.S)
DEFAULTS_TAG_RE = re.compile(r'<script src="\./maid-defaults\.js(\?v=\w+)?"></script>')

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


def _short_hash(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()[:10]


def vrm_libs_ready(front: Path) -> bool:
    lib = front / VRM_DIR / "lib"
    return all((lib / dest).exists() for files in VRM_LIBS.values() for dest in files.values())


def extract_vrm_libs(archives: dict[str, Path], front: Path) -> None:
    """npm の配布物から 3D 表示に要るファイルだけを frontend/maid-vrm/lib に取り出す。"""
    lib = front / VRM_DIR / "lib"
    for name, files in VRM_LIBS.items():
        with tarfile.open(archives[name], "r:gz") as t:
            for src, dest in files.items():
                out = lib / dest
                out.parent.mkdir(parents=True, exist_ok=True)
                with t.extractfile(src) as f:
                    out.write_bytes(f.read())


def _setup_vrm(front: Path, vrm: Path | None, view: str, changed: list[str]) -> dict | None:
    """3D モデルと表示用のファイルを frontend/maid-vrm に置き、画面に渡す設定を返す。使わないなら片付けて None。"""
    folder = front / VRM_DIR
    model = folder / "model.vrm"
    if vrm is None:
        if model.exists():
            model.unlink()
            changed.append("model.vrm（削除）")
        return None
    if not vrm.is_file():
        raise FileNotFoundError(f"3D モデルが見つかりません: {vrm}（maid.settings.json の vrm を確認してください）")
    if not vrm_libs_ready(front):
        raise FileNotFoundError("3D 表示用のライブラリがありません（start.bat がダウンロードします）")
    folder.mkdir(parents=True, exist_ok=True)
    st = vrm.stat()
    if not model.exists() or model.stat().st_size != st.st_size or int(model.stat().st_mtime) != int(st.st_mtime):
        shutil.copy2(vrm, model)  # 更新日時ごとコピーするので、次からは同じならコピーしない
        changed.append("model.vrm")
    js = (ROOT / "frontend" / "maid-vrm.js").read_text(encoding="utf-8")
    if _write_if_changed(folder / "maid-vrm.js", js):
        changed.append("maid-vrm.js")
    # ?v= はファイルが変わったときだけ変わる（Edge に古いファイルを使い回させず、同じなら読み込みを速くする）
    return {"url": f"./{VRM_DIR}/model.vrm?v={st.st_size}-{int(st.st_mtime)}", "view": view,
            "script": f"./{VRM_DIR}/maid-vrm.js?v={_short_hash(js.encode())}"}


def patch_frontend(front: Path, ws_url: str, base_url: str, silence_ms: int,
                   vrm: Path | None = None, vrm_view: str = "bust") -> list[str]:
    """変更したファイル名の一覧を返す。"""
    index = front / "index.html"
    if not index.exists():
        raise FileNotFoundError(f"{index} がありません（setup.bat を実行してください）")
    changed: list[str] = []
    try:
        vrm_cfg = _setup_vrm(front, vrm, vrm_view, changed)
    except FileNotFoundError as e:  # 3D が出せなくても、ほかの準備は続ける（Live2D のまま）
        print(f"3D モデルを使えません: {e}", file=sys.stderr)
        vrm_cfg = None
    frames = max(3, round(silence_ms / VAD_FRAME_MS))
    js = FRONTEND_JS.format(ws_url=json.dumps(ws_url), base_url=json.dumps(base_url), redemption_frames=frames,
                            vrm=json.dumps(vrm_cfg and {"url": vrm_cfg["url"], "view": vrm_cfg["view"]}))
    if _write_if_changed(front / "maid-defaults.js", js):
        changed.append("maid-defaults.js")

    html = index.read_text(encoding="utf-8")
    defaults_tag = f'<script src="./maid-defaults.js?v={_short_hash(js.encode())}"></script>'
    if DEFAULTS_TAG_RE.search(html):
        new_html = DEFAULTS_TAG_RE.sub(defaults_tag, html, count=1)
    else:
        new_html = html.replace("<head>", f"<head>\n    {defaults_tag}", 1)
    # 3D 表示の読み込み。import map は画面の本体（module）より前に置く必要がある
    new_html = VRM_BLOCK_RE.sub("", new_html)
    if vrm_cfg:
        block = ("\n    <!-- maid-vrm -->\n"
                 f'    <script type="importmap">{json.dumps(IMPORT_MAP)}</script>\n'
                 f'    <script type="module" src="{vrm_cfg["script"]}"></script>\n'
                 "    <!-- /maid-vrm -->")
        new_html = new_html.replace(defaults_tag, defaults_tag + block, 1)

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
    f.add_argument("--vrm", default="", help="3D モデル (.vrm) のパス。空なら Live2D のまま")
    f.add_argument("--vrm-view", choices=("bust", "full"), default="bust", help="bust = 胸から上 / full = 全身")
    f.add_argument("--template", default=str(Path(__file__).resolve().parent.parent / "config" / "open-llm-vtuber" / "conf.yaml"))
    v = sub.add_parser("extract-vrm-libs")
    v.add_argument("three")
    v.add_argument("three_vrm")
    v.add_argument("--olv", required=True)
    e = sub.add_parser("extract-asr")
    e.add_argument("archive")
    e.add_argument("--olv", required=True)
    args = p.parse_args(argv)

    olv = Path(args.olv)
    if args.cmd == "prepare":
        changed = patch_frontend(olv / "frontend", args.ws_url, args.base_url, args.silence_ms,
                                 Path(args.vrm) if args.vrm else None, args.vrm_view)
        if changed:
            print("画面の設定を更新しました: " + ", ".join(changed))
        result = patch_conf(olv / "conf.yaml", Path(args.template))
        if result == "rebuilt":
            print("conf.yaml から使わない設定を消しました（キャラの設定はそのまま。元のファイルは conf.yaml.before-maid）")
        elif result:
            print("conf.yaml の音声認識を ReazonSpeech に直しました")
    elif args.cmd == "extract-vrm-libs":
        extract_vrm_libs({"three": Path(args.three), "three-vrm": Path(args.three_vrm)}, olv / "frontend")
        print("3D 表示用のライブラリを展開しました")
    elif args.cmd == "extract-asr":
        extract_reazon(Path(args.archive), olv / "models" / REAZON_NAME)
        print("音声認識モデルを展開しました")
    return 0


if __name__ == "__main__":
    sys.exit(main())
