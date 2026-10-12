"""Open-LLM-VTuber への修正 (scripts/olv_patch.py) のテスト。"""

import io
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import olv_patch  # noqa: E402

# v1.2.1 の画面の本体と同じ形の断片
BUNDLE = (
    'const x=1;[$,_e]=useLocalStorage("micOn",DEFAULT_VAD_STATE.micOn),tt=reactExports.useRef(!0),'
    '[st,it]=useLocalStorage("autoStopMic",DEFAULT_VAD_STATE.autoStopMic),'
    '[j,rt]=useLocalStorage("autoStartMicOn",DEFAULT_VAD_STATE.autoStartMicOn),ot=reactExports.useRef(!1),'
    '[at,lt]=useLocalStorage("autoStartMicOnConvEnd",DEFAULT_VAD_STATE.autoStartMicOnConvEnd),'
    "ut=reactExports.useRef(!1),[,ct]=reactExports.useReducer(Vt=>Vt+1,0);"
)
INDEX = """<!doctype html>
<html>
  <head>
    <meta charset="UTF-8" />
    <script type="module" crossorigin src="./assets/main-nu7uwxNJ.js"></script>
  </head>
</html>
"""


class FrontendTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.front = Path(self.tmp.name)
        (self.front / "assets").mkdir()
        (self.front / "index.html").write_text(INDEX, encoding="utf-8")
        (self.front / "assets" / "main-nu7uwxNJ.js").write_text(BUNDLE, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_patch_is_idempotent_and_fixes_mic_defaults(self):
        changed = olv_patch.patch_frontend(self.front, "ws://127.0.0.1:12394/client-ws", "http://127.0.0.1:12393", 900)
        self.assertEqual(sorted(changed), sorted(["index.html", "main-nu7uwxNJ.js", "maid-defaults.js"]))
        self.assertEqual(olv_patch.patch_frontend(self.front, "ws://127.0.0.1:12394/client-ws", "http://127.0.0.1:12393", 900), [])

        bundle = (self.front / "assets" / "main-nu7uwxNJ.js").read_text(encoding="utf-8")
        self.assertIn('tt=reactExports.useRef(!1),[st,it]=useLocalStorage("autoStopMic"', bundle)
        self.assertIn('ot=reactExports.useRef(!0),[at,lt]=useLocalStorage("autoStartMicOnConvEnd"', bundle)
        self.assertIn("ut=reactExports.useRef(!0),[,ct]", bundle)

        html = (self.front / "index.html").read_text(encoding="utf-8")
        self.assertEqual(html.count("maid-defaults.js"), 1)
        self.assertIn('main-nu7uwxNJ.js?maid=3"', html)

        js = (self.front / "maid-defaults.js").read_text(encoding="utf-8")
        self.assertIn('JSON.stringify("ws://127.0.0.1:12394/client-ws")', js)
        self.assertIn("vad.redemptionFrames = 28;", js)

    def test_ws_url_change_only_rewrites_defaults(self):
        olv_patch.patch_frontend(self.front, "ws://a/client-ws", "http://b", 900)
        self.assertEqual(olv_patch.patch_frontend(self.front, "ws://c/client-ws", "http://b", 900), ["maid-defaults.js"])


class AsrTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_conf_asr_keeps_everything_else(self):
        try:
            import ruamel.yaml  # noqa: F401
            import yaml
        except ImportError:
            self.skipTest("ruamel.yaml / pyyaml なし")
        conf = self.dir / "conf.yaml"
        conf.write_text((ROOT / "config" / "open-llm-vtuber" / "conf.yaml").read_text(encoding="utf-8"), encoding="utf-8")
        before = yaml.safe_load(conf.read_text(encoding="utf-8"))
        self.assertTrue(olv_patch.patch_conf_asr(conf, "reazonspeech"))
        self.assertFalse(olv_patch.patch_conf_asr(conf, "reazonspeech"))
        after = yaml.safe_load(conf.read_text(encoding="utf-8"))
        sherpa = after["character_config"]["asr_config"]["sherpa_onnx_asr"]
        self.assertEqual(sherpa["model_type"], "transducer")
        self.assertTrue(sherpa["encoder"].endswith("encoder-epoch-99-avg-1.int8.onnx"))
        # 音声認識以外は何も変わっていない
        for d in (before, after):
            d["character_config"]["asr_config"].pop("sherpa_onnx_asr")
        self.assertEqual(before, after)

    def test_sensevoice_language_patch(self):
        py = self.dir / "sherpa_onnx_asr.py"
        py.write_text("x = f(\n                model=self.sense_voice,\n                tokens=self.tokens,\n)\n", encoding="utf-8")
        self.assertTrue(olv_patch.patch_sensevoice_language(py, "ja"))
        self.assertFalse(olv_patch.patch_sensevoice_language(py, "ja"))
        self.assertTrue(olv_patch.patch_sensevoice_language(py, "auto"))
        text = py.read_text(encoding="utf-8")
        self.assertEqual(text.count("maid-asr-language"), 1)
        self.assertIn('language="auto",', text)
        compile(text, "x", "exec")

    def test_extract_reazon_keeps_only_needed_files(self):
        archive = self.dir / "m.tar.bz2"
        with tarfile.open(archive, "w:bz2") as t:
            for name in olv_patch.REAZON_FILES + ("encoder-epoch-99-avg-1.onnx", "test_wavs/1.wav"):
                data = name.encode()
                info = tarfile.TarInfo(f"{olv_patch.REAZON_NAME}/{name}")
                info.size = len(data)
                t.addfile(info, io.BytesIO(data))
        dest = self.dir / "out"
        olv_patch.extract_reazon(archive, dest)
        self.assertEqual(sorted(p.name for p in dest.iterdir()), sorted(olv_patch.REAZON_FILES))


if __name__ == "__main__":
    unittest.main()
