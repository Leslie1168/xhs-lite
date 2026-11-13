import re
import sys

p = r"D:\tools\work\xhs-app\assets\index.html"
h = open(p, encoding="utf-8").read()
blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", h, re.S)
js = "\n".join(blocks)
open(r"D:\tools\work\xhs-app\_c.js", "w", encoding="utf-8").write(js)
print("blocks", len(blocks), "bytes", len(js))

need = [
    "window.__cb", "function call(", "function buildFeed", "function doSearch",
    "function doAuthor", "function doMy", "function thumbOf", "function dlStart",
    "function startHls", "histPost", "__BOOT_ERR__", "Native.req",
]
for k in need:
    print(("OK  " if k in js else "MISS"), k)
print("BASES 残留:", "BASES" in js)
print("App is not defined 之类:", "fetch(API" in js)
