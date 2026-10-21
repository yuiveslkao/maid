"""画面を 1 枚撮る・貼られた画像を小さくする（頭脳に見せる画像。Pillow を使う）。"""

from __future__ import annotations

import base64
import io
import re

MAX_SIDE = 1280      # 長い辺をここまで縮める（頭脳はこれ以上大きくてもよく見えず、遅くなるだけ）
QUALITY = 85
# 声で「画面見て」「これわかる？」のように言われたら、画面を撮って一緒に見せる
LOOK_RE = re.compile(r"(画面|がめん|これ|この|それ|ここ|今の|いまの).{0,12}(見て|みて|見える|みえる|わかる|分かる|読んで|よんで)")


def to_data_url(img) -> str:
    img = img.convert("RGB")
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=QUALITY)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def capture() -> str:
    """メインの画面（モニター）を撮る。"""
    from PIL import ImageGrab

    return to_data_url(ImageGrab.grab())


def shrink(data_url: str) -> str:
    """貼られた・落とされた画像を、頭脳に見せる大きさの JPEG にする。"""
    from PIL import Image

    m = re.match(r"data:image/[a-z+.-]+;base64,(.*)", data_url, re.S)
    if not m:
        raise ValueError("画像ではありません")
    return to_data_url(Image.open(io.BytesIO(base64.b64decode(m.group(1)))))


def wants_look(text: str) -> bool:
    return bool(LOOK_RE.search(text))
