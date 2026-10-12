"""maid が Open-LLM-VTuber (v1.2.1) に当てる修正をまとめたもの。何度実行しても同じ結果になる。

本体は改造しない方針だが、設定で変えられない部分だけここで直す:

    frontend      画面の初期設定 (maid-defaults.js) を書き、画面のマイク設定の不具合を直す
    asr           conf.yaml の音声認識の部分だけを書き換え、SenseVoice の言語を固定する
    extract-asr   ReazonSpeech のアーカイブから必要なファイルだけを取り出す

Open-LLM-VTuber のライブラリ（ruamel.yaml）を使うので、その環境で動かす:

    uv run --project runtime/Open-LLM-VTuber python scripts/olv_patch.py asr --olv runtime/Open-LLM-VTuber --engine reazonspeech
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


# ------------------------------------------------------------------ 音声認識

REAZON_NAME = "sherpa-onnx-zipformer-ja-reazonspeech-2024-08-01"
REAZON_FILES = ("encoder-epoch-99-avg-1.int8.onnx", "decoder-epoch-99-avg-1.int8.onnx",
                "joiner-epoch-99-avg-1.int8.onnx", "tokens.txt")
SENSEVOICE_NAME = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"

ASR_ENGINES = {
    # 日本語専用（ReazonSpeech の日本語音声で学習）。CPU で速く、日本語の精度が高い
    "reazonspeech": {
        "model_type": "transducer",
        "encoder": f"./models/{REAZON_NAME}/{REAZON_FILES[0]}",
        "decoder": f"./models/{REAZON_NAME}/{REAZON_FILES[1]}",
        "joiner": f"./models/{REAZON_NAME}/{REAZON_FILES[2]}",
        "tokens": f"./models/{REAZON_NAME}/tokens.txt",
        "use_itn": False,
    },
    # 多言語（中国語中心）。Open-LLM-VTuber の初期設定
    "sensevoice": {
        "model_type": "sense_voice",
        "sense_voice": f"./models/{SENSEVOICE_NAME}/model.int8.onnx",
        "tokens": f"./models/{SENSEVOICE_NAME}/tokens.txt",
        "use_itn": True,
    },
}

SENSEVOICE_ANCHOR = "model=self.sense_voice,"
LANG_LINE = re.compile(r'language="[a-z]*",  # maid-asr-language')


def patch_conf_asr(conf: Path, engine: str) -> bool:
    """conf.yaml の音声認識の部分だけを書き換える（キャラ設定などやコメントには触らない）。"""
    from ruamel.yaml import YAML

    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096
    with conf.open(encoding="utf-8") as f:
        data = yaml.load(f)
    asr = data["character_config"]["asr_config"]
    sherpa = asr.setdefault("sherpa_onnx_asr", {})
    changed = False
    if asr.get("asr_model") != "sherpa_onnx_asr":
        asr["asr_model"] = "sherpa_onnx_asr"
        changed = True
    for key, value in ASR_ENGINES[engine].items():
        if sherpa.get(key) != value:
            sherpa[key] = value
            changed = True
    if changed:
        with conf.open("w", encoding="utf-8", newline="\n") as f:
            yaml.dump(data, f)
    return changed


def patch_sensevoice_language(asr_py: Path, language: str) -> bool:
    """v1.2.1 は SenseVoice の言語を設定できないので、言語指定の 1 行を足す（自動判定だと韓国語などに間違える）。"""
    text = asr_py.read_text(encoding="utf-8")
    line = f'language="{language}",  # maid-asr-language'
    if LANG_LINE.search(text):
        new = LANG_LINE.sub(line, text)
    else:
        i = text.find(SENSEVOICE_ANCHOR)
        if i < 0:
            raise ValueError("SenseVoice の呼び出しが見つかりません（Open-LLM-VTuber の版が違う？）")
        nl = "\r\n" if "\r\n" in text else "\n"
        end = i + len(SENSEVOICE_ANCHOR)
        new = text[:end] + f"{nl}                {line}" + text[end:]
    return _write_if_changed(asr_py, new)


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
    f = sub.add_parser("frontend")
    f.add_argument("--olv", required=True)
    f.add_argument("--ws-url", required=True)
    f.add_argument("--base-url", default="http://127.0.0.1:12393")
    f.add_argument("--silence-ms", type=int, default=900)
    a = sub.add_parser("asr")
    a.add_argument("--olv", required=True)
    a.add_argument("--engine", choices=sorted(ASR_ENGINES), required=True)
    a.add_argument("--language", default="ja", help="sensevoice のときの言語（ja / auto / en / zh / ko / yue）")
    e = sub.add_parser("extract-asr")
    e.add_argument("archive")
    e.add_argument("--olv", required=True)
    args = p.parse_args(argv)

    olv = Path(args.olv)
    if args.cmd == "frontend":
        changed = patch_frontend(olv / "frontend", args.ws_url, args.base_url, args.silence_ms)
        print("画面の設定: " + (", ".join(changed) + " を更新" if changed else "変更なし"))
    elif args.cmd == "asr":
        changed = patch_conf_asr(olv / "conf.yaml", args.engine)
        patch_sensevoice_language(olv / "src" / "open_llm_vtuber" / "asr" / "sherpa_onnx_asr.py", args.language)
        print(f"音声認識: {args.engine}" + ("（conf.yaml を書き換えました）" if changed else ""))
    elif args.cmd == "extract-asr":
        extract_reazon(Path(args.archive), olv / "models" / REAZON_NAME)
        print("音声認識モデルを展開しました")
    return 0


if __name__ == "__main__":
    sys.exit(main())
