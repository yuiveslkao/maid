"""Open-LLM-VTuber の conf.yaml の「音声認識」の部分だけを書き換える。

キャラ設定などほかの部分やコメントには触らない（ruamel.yaml で読み書きするので体裁が保たれる）。
Open-LLM-VTuber のライブラリ（ruamel.yaml）を使うので、その環境で動かす:

    uv run --project runtime/Open-LLM-VTuber python scripts/olv_asr.py runtime/Open-LLM-VTuber/conf.yaml --engine reazonspeech
"""

from __future__ import annotations

import argparse
import sys

from ruamel.yaml import YAML

REAZON_DIR = "./models/sherpa-onnx-zipformer-ja-reazonspeech-2024-08-01"
SENSEVOICE_DIR = "./models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"

ENGINES = {
    # 日本語専用（ReazonSpeech の 35,000 時間の日本語音声で学習）。CPU で速く、日本語の精度が高い
    "reazonspeech": {
        "model_type": "transducer",
        "encoder": f"{REAZON_DIR}/encoder-epoch-99-avg-1.int8.onnx",
        "decoder": f"{REAZON_DIR}/decoder-epoch-99-avg-1.int8.onnx",
        "joiner": f"{REAZON_DIR}/joiner-epoch-99-avg-1.int8.onnx",
        "tokens": f"{REAZON_DIR}/tokens.txt",
        "use_itn": False,
    },
    # 多言語（中国語中心）。Open-LLM-VTuber の初期設定
    "sensevoice": {
        "model_type": "sense_voice",
        "sense_voice": f"{SENSEVOICE_DIR}/model.int8.onnx",
        "tokens": f"{SENSEVOICE_DIR}/tokens.txt",
        "use_itn": True,
    },
}


def apply(conf_path: str, engine: str) -> bool:
    """書き換えが必要なら書き換えて True を返す。"""
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096
    with open(conf_path, encoding="utf-8") as f:
        data = yaml.load(f)

    asr = data["character_config"]["asr_config"]
    sherpa = asr.setdefault("sherpa_onnx_asr", {})
    changed = False
    if asr.get("asr_model") != "sherpa_onnx_asr":
        asr["asr_model"] = "sherpa_onnx_asr"
        changed = True
    for key, value in ENGINES[engine].items():
        if sherpa.get(key) != value:
            sherpa[key] = value
            changed = True
    if changed:
        with open(conf_path, "w", encoding="utf-8", newline="\n") as f:
            yaml.dump(data, f)
    return changed


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("conf")
    p.add_argument("--engine", choices=sorted(ENGINES), required=True)
    a = p.parse_args(argv)
    changed = apply(a.conf, a.engine)
    print(f"音声認識: {a.engine}" + ("（conf.yaml を書き換えました）" if changed else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
