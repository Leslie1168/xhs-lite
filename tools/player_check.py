# -*- coding: utf-8 -*-
import json, time, urllib.request, urllib.parse

B = "http://127.0.0.1:8770"


def get(path, tmo=120):
    t0 = time.time()
    with urllib.request.urlopen(B + path, timeout=tmo) as r:
        return r.status, r.read(), time.time() - t0


def post(path, tmo=180):
    t0 = time.time()
    req = urllib.request.Request(B + path, method="POST")
    with urllib.request.urlopen(req, timeout=tmo) as r:
        return r.status, r.read(), time.time() - t0


ok = True
def chk(name, cond, extra=""):
    global ok
    if not cond:
        ok = False
    print("  [%s] %-40s %s" % ("OK" if cond else "!!", name, extra))


print("=== 1 首页 ===")
c, b, dt = get("/")
h = b.decode("utf-8")
chk("GET / 200", c == 200, "%d B" % len(b))
for k in ("id=\"au\"", "id=\"auwall\"", "id=\"auq\"", "id=\"aubk\"",
          "id=\"lu\"", "function showAuthor", "function pollAuthor", "popstate"):
    chk("含 " + k, k in h)

print()
print("=== 2 feed（带标题+作者） ===")
c, b, dt = get("/api/feed?n=12&p=1")
jf = json.loads(b)
its = jf.get("items") or []
chk("返回条数", len(its) == 12, "%d" % len(its))
chk("每条的 title/uid/uname 齐全",
    all(x.get("title") is not None and x.get("uid") and x.get("uname") for x in its))
chk("黑名单 15583959 未出现", all(x.get("uid") != 15583959 for x in its))

print()
print("=== 3 作者主页（首屏→补齐） ===")
c, b, dt = get("/api/author/39784")
j = json.loads(b)
chk("首屏有返回", len(j.get("items") or []) > 0, "%d 条 %.2fs grow=%s" %
    (len(j.get("items") or []), dt, j.get("grow")))
for i in range(20):
    time.sleep(1.5)
    c, b, dt = get("/api/author/39784")
    j2 = json.loads(b)
    if not j2.get("grow"):
        break
its2 = j2.get("items") or []
chk("补齐到 831 条", len(its2) == 831, "%d 条" % len(its2))
chk("全部带 uid/uname", all(x.get("uid") and x.get("uname") for x in its2))

print()
print("=== 4 粉丝团严格过滤 ===")
import xhs2
xhs2.ensure_account()
allg = []
for pg in (1, 2, 3):
    r = xhs2.raw_post("v2/member/note-list",
                      {"note_type": 0, "user_id": 39784, "page": pg, "page_size": 500})
    allg += r["data"]["list"]
grp = [x["note_id"] for x in allg if (x.get("group_id") or 0) != 0]
gg = {x["note_id"] for x in allg if (x.get("group_id") or 0) == 0}
ids = {x["id"] for x in its2}
chk("与原始非团集合完全一致", ids == gg, "原始团 %d / 非团 %d" % (len(grp), len(gg)))
chk("混进团笔记为 0", len([g for g in grp if g in ids]) == 0)

print()
print("=== 5 作者内搜索 ===")
for q, exp in (("黑丝", 11), ("自慰", 17), ("韩国", 5), ("zzz不存在", 0)):
    c, b, dt = get("/api/author/39784?q=" + urllib.parse.quote(q))
    n = len(json.loads(b).get("items") or [])
    chk("q=%s" % q, n == exp, "%d 条（预期 %d）%.2fs" % (n, exp, dt))

print()
print("=== 6 anchor 对齐 ===")
anchor = its2[150]["id"]
c, b, dt = get("/api/author/39784?a=%d" % anchor)
f = (json.loads(b).get("items") or [{}])[0]
chk("首条 == anchor", f.get("id") == anchor, "%s" % f.get("id"))

print()
print("=== 7 搜索 fallback ===")
for q in ("黑丝", "美食"):
    c, b, dt = get("/api/search?q=" + urllib.parse.quote(q))
    jq = json.loads(b)
    n = len(jq.get("items") or [])
    chk("q=%s 有条目" % q, n > 0, "%d 条 unfiltered=%s %.2fs" % (n, jq.get("unfiltered"), dt))

print()
print("=== 8 HLS 边下边播 ===")
c, b, dt = get("/hls/63534/index.m3u8")
txt = b.decode()
chk("清单 200", c == 200, "%d B" % len(b))
chk("已剥掉 EXT-X-KEY", "#EXT-X-KEY" not in txt)
c2, b2, dt2 = get("/hls/63534/s/0.ts")
chk("分片首字节 0x47", b2[:1] == b"\x47", "%d B %.2fs" % (len(b2), dt2))

print()
print("=== 9 刷新 ===")
c, b, dt = post("/api/refresh")
chk("POST /api/refresh", c == 200, b.decode()[:60] + " %.2fs" % dt)

print()
print("=== 10 下载 ===")
c, b, dt = post("/api/dl/62950")
chk("POST /api/dl", c == 200, b.decode()[:40])

print()
print("总计:", "全部通过" if ok else "有失败项")
