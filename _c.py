"""前端 JS 自检：把 index.html 里的内联 <script> 抽出来，做完整性检查。

用法：
    python _c.py                     # 检查仓库里的 assets/index.html
    python _c.py path/to/index.html  # 检查指定的 index.html

产物：同目录下的 _c.js（抽出的 JS，可直接喂给 node --check）
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
p = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "assets", "index.html")

h = open(p, encoding="utf-8").read()
blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", h, re.S)
js = "\n".join(blocks)

out = os.path.join(HERE, "_c.js")
open(out, "w", encoding="utf-8").write(js)
print("blocks", len(blocks), "bytes", len(js), "->", out)

need = [
    "window.__cb", "function call(", "function buildFeed", "function doSearch",
    "function doAuthor", "function doMy", "function thumbOf", "function dlStart",
    "function startHls", "histPost", "__BOOT_ERR__", "Native.req",
]
for k in need:
    print(("OK  " if k in js else "MISS"), k)
print("BASES 残留:", "BASES" in js)
print("App is not defined 之类:", "fetch(API" in js)
