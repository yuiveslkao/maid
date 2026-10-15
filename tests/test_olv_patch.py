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


TEMPLATE = ROOT / "config" / "open-llm-vtuber" / "conf.yaml"


class ConfTest(unittest.TestCase):
    def setUp(self):
        try:
            import ruamel.yaml  # noqa: F401
            import yaml
        except ImportError:
            self.skipTest("ruamel.yaml / pyyaml なし")
        self.yaml = yaml
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.conf = self.dir / "conf.yaml"

    def tearDown(self):
        self.tmp.cleanup()

    def load(self):
        return self.yaml.safe_load(self.conf.read_text(encoding="utf-8"))

    def test_template_is_already_maid_shape(self):
        self.conf.write_text(TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
        self.assertEqual(olv_patch.patch_conf(self.conf, TEMPLATE), "")

    def test_old_conf_is_rebuilt_keeping_character(self):
        old = self.yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
        cc = old["character_config"]
        cc["character_name"] = "めい"
        cc["persona_prompt"] = "あなたはめい。\n- 短く話す。\n"
        cc["agent_config"]["llm_configs"]["claude_llm"] = {"model": "x"}
        cc["tts_config"]["edge_tts"] = {"voice": "y"}
        cc["asr_config"]["sherpa_onnx_asr"] = {"model_type": "sense_voice", "sense_voice": "./a.onnx", "tokens": "./t.txt"}
        old["live_config"] = {"bilibili_live": {"room_ids": [1]}}
        self.conf.write_text(self.yaml.safe_dump(old, allow_unicode=True), encoding="utf-8")

        self.assertEqual(olv_patch.patch_conf(self.conf, TEMPLATE), "rebuilt")
        self.assertEqual(olv_patch.patch_conf(self.conf, TEMPLATE), "")
        new = self.load()
        expected = self.yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
        expected["character_config"]["character_name"] = "めい"
        expected["character_config"]["persona_prompt"] = "あなたはめい。\n- 短く話す。\n"
        self.assertEqual(new, expected)
        self.assertTrue((self.dir / "conf.yaml.before-maid").exists())

    def test_asr_is_fixed_in_place(self):
        text = TEMPLATE.read_text(encoding="utf-8").replace("model_type: 'transducer'", "model_type: 'sense_voice'")
        self.conf.write_text(text.replace("character_name: 'メイド'", "character_name: 'めい'"), encoding="utf-8")
        self.assertEqual(olv_patch.patch_conf(self.conf, TEMPLATE), "asr")
        conf = self.load()
        self.assertEqual(conf["character_config"]["asr_config"]["sherpa_onnx_asr"]["model_type"], "transducer")
        self.assertEqual(conf["character_config"]["character_name"], "めい")
        self.assertFalse((self.dir / "conf.yaml.before-maid").exists())


class ReazonTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

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
