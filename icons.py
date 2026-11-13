import sys
import os
from PIL import Image

SRC = sys.argv[1] if len(sys.argv) > 1 else "icon_src.png"
DST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "res")

im = Image.open(SRC).convert("RGBA")
print("src", im.size)

SIZES = {"mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192}
for k, v in SIZES.items():
    d = os.path.join(DST, "mipmap-" + k)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "ic_launcher.png")
    im.resize((v, v), Image.LANCZOS).save(p, "PNG", optimize=True)
    print(k, v, os.path.getsize(p))

# 圆形图标（部分启动器会要 round）
for k, v in SIZES.items():
    d = os.path.join(DST, "mipmap-" + k)
    p = os.path.join(d, "ic_launcher_round.png")
    im.resize((v, v), Image.LANCZOS).save(p, "PNG", optimize=True)
print("round done")
