"""手工 APK 重打包：把编译好的 classes.dex 塞进 aapt2 生成的 base.apk。

用法：
    python pack.py            # 默认读同目录 build/
    python pack.py <build目录>

两个必踩的坑都在这里处理：
1. resources.arsc 必须 STORED 且 4 字节对齐，否则 Android 11+ 直接拒装：
   Failure [-124: Targeting R+ requires the resources.arsc to be stored uncompressed]
2. 其余条目沿用 base.apk 里原本的压缩方式，不要一刀切重压。
"""
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "build")

base = os.path.join(BUILD, "base.apk")
dex = os.path.join(BUILD, "dex", "classes.dex")
out = os.path.join(BUILD, "unsigned.apk")

if os.path.exists(out):
    os.remove(out)

with zipfile.ZipFile(base) as zin:
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for it in zin.infolist():
            data = zin.read(it.filename)
            zi = zipfile.ZipInfo(it.filename, date_time=it.date_time)
            stored = it.filename == "resources.arsc" or it.compress_type == zipfile.ZIP_STORED
            zi.compress_type = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
            zi.external_attr = it.external_attr
            zout.writestr(zi, data)
        zi = zipfile.ZipInfo("classes.dex", date_time=(2026, 1, 1, 0, 0, 0))
        zi.compress_type = zipfile.ZIP_DEFLATED
        zout.writestr(zi, open(dex, "rb").read())

print("unsigned.apk", os.path.getsize(out))

with zipfile.ZipFile(out) as z:
    names = z.namelist()
    print("entries", len(names))
    for n in names[:40]:
        print("  ", n)
    print("has dex", "classes.dex" in names)
    print("has manifest", "AndroidManifest.xml" in names)
    print("has icon", any("ic_launcher" in n for n in names))
