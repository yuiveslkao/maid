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
        self.assertIn(f'main-nu7uwxNJ.js?maid={olv_patch.BUNDLE_VERSION}"', html)

        js = (self.front / "maid-defaults.js").read_text(encoding="utf-8")
        self.assertIn('JSON.stringify("ws://127.0.0.1:12394/client-ws")', js)
        self.assertIn("vad.redemptionFrames = 28;", js)

    def test_mic_off(self):
        olv_patch.patch_frontend(self.front, "ws://a", "http://b", 900, mic=False)
        js = (self.front / "maid-defaults.js").read_text(encoding="utf-8")
        for key in ("micOn", "autoStartMicOn", "autoStartMicOnConvEnd"):
            self.assertIn(f'ls.setItem("{key}", "false")', js)

    def test_ws_url_change_only_rewrites_defaults(self):
        olv_patch.patch_frontend(self.front, "ws://a/client-ws", "http://b", 900)
        # 設定が変わると、Edge に古い設定を使わせないよう index.html の ?v= も変わる
        self.assertEqual(olv_patch.patch_frontend(self.front, "ws://c/client-ws", "http://b", 900), ["maid-defaults.js", "index.html"])

    def make_vrm_libs(self):
        archives = {}
        for name, files in olv_patch.VRM_LIBS.items():
            path = self.front / f"{name}.tgz"
            with tarfile.open(path, "w:gz") as t:
                for src in files:
                    data = src.encode()
                    info = tarfile.TarInfo(src)
                    info.size = len(data)
                    t.addfile(info, io.BytesIO(data))
            archives[name] = path
        olv_patch.extract_vrm_libs(archives, self.front)

    def test_vrm(self):
        vrm = self.front / "my.vrm"
        vrm.write_bytes(b"glTF" + b"0" * 100)
        # ライブラリが無ければ 3D は諦めて、ほかの準備はする
        self.assertIn("maid-defaults.js", olv_patch.patch_frontend(self.front, "ws://a", "http://b", 900, vrm))
        self.assertNotIn("maid-vrm", (self.front / "index.html").read_text(encoding="utf-8"))

        self.make_vrm_libs()
        self.assertTrue(olv_patch.vrm_libs_ready(self.front))
        changed = olv_patch.patch_frontend(self.front, "ws://a", "http://b", 900, vrm, "full")
        self.assertEqual(sorted(changed), sorted(["model.vrm", "maid-vrm.js", "maid-defaults.js", "index.html"]))
        self.assertEqual(olv_patch.patch_frontend(self.front, "ws://a", "http://b", 900, vrm, "full"), [])
        html = (self.front / "index.html").read_text(encoding="utf-8")
        # import map は画面の本体（module）より前
        self.assertLess(html.index('type="importmap"'), html.index("assets/main-nu7uwxNJ.js"))
        self.assertEqual(html.count("<!-- maid-vrm -->"), 1)
        js = (self.front / "maid-defaults.js").read_text(encoding="utf-8")
        self.assertIn('"view": "full"', js)
        self.assertIn("./maid-vrm/model.vrm?v=", js)
        self.assertEqual((self.front / "maid-vrm" / "model.vrm").read_bytes(), vrm.read_bytes())

        # 使わなくしたら片付ける
        olv_patch.patch_frontend(self.front, "ws://a", "http://b", 900, None)
        self.assertNotIn("maid-vrm", (self.front / "index.html").read_text(encoding="utf-8"))
        self.assertIn("window.MAID_VRM = null;", (self.front / "maid-defaults.js").read_text(encoding="utf-8"))
        self.assertFalse((self.front / "maid-vrm" / "model.vrm").exists())


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
