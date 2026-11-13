#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
player.py — 小黄书「只看片」本地播放器

一个页面，没有标题、没有作者、没有广告、没有文字，只有封面墙。
点封面 → 播放。右下角 → 下载。

启动:
    python player.py            # 默认 http://127.0.0.1:8770
    python player.py 8080       # 换端口
"""
import os, sys, io, re, json, time, gzip, ssl, socket, threading, queue, hashlib
import http.client
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urljoin, unquote
from subprocess import Popen, PIPE, DEVNULL
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import xhs2
import xhs_fast
import xhs_batch
import xhs_dl
from Crypto.Cipher import AES

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ROOT, "cache")
THUMBS = os.path.join(CACHE, "thumbs")
VIDEOS = os.path.join(CACHE, "videos")
DOWNLOADS = os.path.join(ROOT, "downloads")
BLACKLIST_FILE = os.path.join(ROOT, "blacklist.json")


def load_blacklist():
    """读 blacklist.json，只认数字 user_id 键；name:xxx 这种纯名字键忽略"""
    try:
        raw = json.load(open(BLACKLIST_FILE, encoding="utf-8"))
    except Exception:
        return set()
    ids = set()
    for k in (raw or {}):
        k = str(k)
        if k.isdigit():
            ids.add(int(k))
    return ids


BL = load_blacklist()
for d in (CACHE, THUMBS, VIDEOS, DOWNLOADS):
    os.makedirs(d, exist_ok=True)

FFMPEG = os.environ.get("FFMPEG") or "ffmpeg"
KEY_ECB = b"525202f9149e061d"
UA_WEB = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

SEGS = os.path.join(CACHE, "segs")
os.makedirs(SEGS, exist_ok=True)
HLSJS = os.path.join(CACHE, "hls.min.js")

_lk = threading.Lock()
JOBS = {}        # note_id -> {"state": running|ready|error, "msg": ...}
FEED = {"items": [], "ts": 0}
VIEW_CACHE = {}  # note_id -> data dict
HLS = {}         # note_id -> {"segs":[url], "key":bytes|None, "iv":bytes|None, "ts":time}
SEARCH = {"q": "", "items": [], "ts": 0}
_search_lk = threading.Lock()
# 搜索结果缓存：(q, pn) -> {"ts":.., "items":[..], "grow":bool, "raw":[...], "pool":[...]}
SCACHE = {}
SCACHE_TTL = 900.0
SCACHE_MAX = 80
SCHED = os.path.join(ROOT, "search_cache.json")


def _load_scache():
    try:
        d = json.load(open(SCHED, encoding="utf-8"))
    except Exception:
        return
    now = time.time()
    for k, v in (d or {}).items():
        if not isinstance(v, dict):
            continue
        if now - v.get("ts", 0) > 86400:      # 超过一天别拿来骗人
            continue
        v["grow"] = False
        SCACHE[k] = v


def _save_scache():
    try:
        with _search_lk:
            snap = dict(list(SCACHE.items())[:SCACHE_MAX])
        tmp = SCHED + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False)
        os.replace(tmp, SCHED)
    except Exception as e:
        print("  [scache] 保存失败:", repr(e)[:160], file=sys.stderr)

SRC_KEYWORDS = [
    "反差", "偷拍", "熟女", "少妇", "学生", "自慰", "裸舞", "酒店", "浴室", "人妻",
    "素人", "主播", "网红", "萝莉", "巨乳", "黑丝", "调教", "福利", "私拍", "泄密",
    "偷情", "出轨", "按摩", "洗浴", "夜店", "制服", "丝袜", "女同", "探花", "外围",
]


# ------------------------------------------------------------------ 网络
def ecb_dec(b):
    try:
        if b[:2] == b"\xff\xd8":
            return b
        d = AES.new(KEY_ECB, AES.MODE_ECB).decrypt(b[len(b) // 16 * 16:])
        return d or b
    except Exception:
        return b


_DNS = {}
_DNS_LK = threading.Lock()


def _ips_of(host):
    """DNS 结果缓存 10 分钟，别每张封面都去解析一次"""
    with _DNS_LK:
        v = _DNS.get(host)
        if v and time.time() - v[0] < 600:
            return v[1]
    try:
        ips = list(dict.fromkeys([i[4][0] for i in socket.getaddrinfo(host, 443, socket.AF_INET)]))
    except Exception:
        return None
    with _DNS_LK:
        _DNS[host] = (time.time(), ips)
    return ips


def shrink_jpeg(data, w=360, q=80):
    """卡片只有 150px 宽，没必要把 700KB 原图怼过去"""
    try:
        from io import BytesIO
        from PIL import Image
        im = Image.open(BytesIO(data))
        if im.width <= w:
            return data
        h = max(1, int(im.height * w / im.width))
        im = im.convert("RGB").resize((w, h), Image.LANCZOS)
        out = BytesIO()
        im.save(out, "JPEG", quality=q, optimize=True)
        r = out.getvalue()
        return r if len(r) < len(data) else data
    except Exception:
        return data


def fetch_image(url):
    """封面：走快域 + ECB 解密"""
    url = xhs_fast.rewrite(url)
    host = url.split("/")[2]
    ips = _ips_of(host)
    if not ips:
        return None
    try:
        raw = xhs_fast.plain_fetch(url, host, ips, tries=2)
    except Exception:
        return None
    if raw[:2] == b"\xff\xd8":
        return raw
    try:
        d = AES.new(KEY_ECB, AES.MODE_ECB).decrypt(raw)
        if d[:2] == b"\xff\xd8":
            return d
    except Exception:
        pass
    return raw


def note_detail(nid):
    nid = int(nid)
    with _lk:
        if nid in VIEW_CACHE:
            return VIEW_CACHE[nid]
    try:
        d = xhs2.raw_post("v2/note/view", {"note_id": nid}).get("data") or {}
    except Exception:
        d = {}
    if d:
        with _lk:
            VIEW_CACHE[nid] = d
    return d


def hud(note_id, state, msg=""):
    with _lk:
        JOBS[note_id] = {"state": state, "msg": msg, "ts": time.time()}


def prep(note_id):
    """后台把 note 转成 mp4 缓存"""
    note_id = int(note_id)
    with _lk:
        j = JOBS.get(note_id)
        if j and (j["state"] == "running" or j["state"] == "ready"):
            return
        JOBS[note_id] = {"state": "running", "msg": "准备", "ts": time.time()}

    out = os.path.join(VIDEOS, "%d.mp4" % note_id)

    def run():
        try:
            if os.path.exists(out) and os.path.getsize(out) > 4096:
                hud(note_id, "ready")
                return
            d = note_detail(note_id)
            media = d.get("note_media_url") or ""
            if not any(x in media for x in ("/static/m3u8/", "/static/m4u8/", "/static/52job/")):
                hud(note_id, "error", "locked")
                return
            hud(note_id, "running", "下载")
            h = hls_info(note_id)
            segs = h["segs"]
            ts = os.path.join(CACHE, "_pv_%d.ts" % note_id)
            with open(ts, "wb") as f:
                with ThreadPoolExecutor(max_workers=8) as ex:
                    for data in ex.map(lambda i: hls_seg(note_id, i), range(len(segs))):
                        f.write(data)
            hud(note_id, "running", "转码")
            tmp = out + ".part"
            p = Popen([FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                       "-i", ts, "-c", "copy", "-movflags", "+faststart",
                       "-f", "mp4", tmp],
                      stdout=DEVNULL, stderr=PIPE)
            _, err = p.communicate()
            try:
                os.remove(ts)
            except Exception:
                pass
            if os.path.exists(tmp) and os.path.getsize(tmp) > 4096:
                os.replace(tmp, out)
                hud(note_id, "ready")
            else:
                hud(note_id, "error", "转码失败 " + (err or b"").decode("utf-8", "replace")[:70])
        except Exception as e:
            hud(note_id, "error", str(e)[:80])

    threading.Thread(target=run, daemon=True).start()


def do_download(note_id):
    note_id = int(note_id)
    key = "dl:%d" % note_id
    with _lk:
        j = JOBS.get(key)
        if j and j["state"] == "running":
            return
        JOBS[key] = {"state": "running", "msg": "下载", "ts": time.time()}

    def run():
        try:
            src = os.path.join(VIDEOS, "%d.mp4" % note_id)
            if not os.path.exists(src):
                prep(note_id)
                for _ in range(600):
                    time.sleep(0.5)
                    if os.path.exists(src):
                        break
                    with _lk:
                        jj = JOBS.get(note_id)
                    if jj and jj["state"] == "error":
                        hud(key, "error", jj["msg"])
                        return
            if not os.path.exists(src):
                hud(key, "error", "超时")
                return
            d = note_detail(note_id)
            title = (d.get("note_title") or str(note_id)).strip()
            dst = os.path.join(DOWNLOADS, "%d_%s.mp4" % (note_id, xhs_batch.safe(title)))
            import shutil
            shutil.copyfile(src, dst)
            hud(key, "ready", os.path.basename(dst))
        except Exception as e:
            hud(key, "error", str(e)[:80])

    threading.Thread(target=run, daemon=True).start()


def hls_info(note_id):
    """解析 m3u8，拿到分片列表与密钥；缓存 30 分钟"""
    note_id = int(note_id)
    with _lk:
        h = HLS.get(note_id)
    if h and time.time() - h["ts"] < 1800:
        return h
    d = note_detail(note_id)
    media = d.get("note_media_url") or ""
    if not media:
        raise RuntimeError("no-media")
    media = xhs_fast.rewrite(media)
    if not media.endswith(".m3u8"):
        raise RuntimeError("locked")
    txt = xhs_dl.fetch(media).decode("utf-8", "replace")
    base = media.rsplit("/", 1)[0] + "/"
    segs = [urljoin(base, l.strip()) for l in txt.splitlines()
            if l.strip() and not l.startswith("#")]
    if not segs:
        raise RuntimeError("empty")
    m = re.search(r'#EXT-X-KEY:.*?URI="([^"]+)"', txt)
    keyuri = m.group(1) if m else None
    m = re.search(r"IV=0x([0-9a-fA-F]+)", txt)
    iv = bytes.fromhex(m.group(1)) if m else b"\x00" * 16
    key = xhs_dl.get_key(keyuri, base, media) if keyuri else None
    h = {"segs": segs, "key": key, "iv": iv, "base": base, "ts": time.time()}
    with _lk:
        HLS[note_id] = h
    return h


WARMING = set()
_WARM_NOTES = set()
_warm_lk = threading.Lock()
_WARM_SEM = threading.Semaphore(3)   # 预读独立桶，不抢前台的连接

_INFLIGHT = {}
_IF_LK = threading.Lock()
WARM_AHEAD = 7                       # 往后预读几片（约 70 秒）


def _warm_ahead(note_id, i):
    """从第 i 片往后预读若干片。同一部片同时只跑一个预读线程。"""
    nid = int(note_id)
    with _warm_lk:
        if nid in _WARM_NOTES:
            return
        _WARM_NOTES.add(nid)

    def run():
        try:
            h = hls_info(nid)
            total = len(h["segs"])
            for j in range(i + 1, min(total, i + 1 + WARM_AHEAD)):
                fp = os.path.join(SEGS, str(nid), "%d.ts" % j)
                if os.path.exists(fp) and os.path.getsize(fp) > 188:
                    continue
                with _WARM_SEM:
                    try:
                        hls_seg(nid, j)
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            with _warm_lk:
                _WARM_NOTES.discard(nid)

    threading.Thread(target=run, daemon=True).start()


# ── 下一部预取 ───────────────────────────────────────────────────────────
# 播 A 的时候后台把队列里的 B 的前几片拉下来，滑过去就是秒开。
# 只预取 1 部、只取前 PRE_AHEAD 片，用完即弃；换目标时旧线程自动退出。
PRE = {"nid": 0, "done": 0}
_PRE_LK = threading.Lock()
_PRE_SEM = threading.Semaphore(2)        # 预取独立桶，别跟前台抢连接
PRE_AHEAD = 10                            # 预取前 10 片（约 100 秒）


def prefetch(note_id):
    """把 note_id 的前 PRE_AHEAD 片拉到本地。返回是否真的起了活。"""
    try:
        nid = int(note_id)
    except Exception:
        return False
    if nid <= 0:
        return False
    with _PRE_LK:
        if PRE["nid"] == nid:
            return False
        PRE["nid"] = nid
        PRE["done"] = 0

    def run():
        got = 0
        try:
            h = hls_info(nid)
            total = len(h["segs"])
            for j in range(min(PRE_AHEAD, total)):
                with _PRE_LK:
                    if PRE["nid"] != nid:      # 目标换了，立刻放弃
                        return
                fp = os.path.join(SEGS, str(nid), "%d.ts" % j)
                if os.path.exists(fp) and os.path.getsize(fp) > 188:
                    got += 1
                    with _PRE_LK:
                        if PRE["nid"] == nid:
                            PRE["done"] = got
                    continue
                try:
                    with _PRE_SEM:
                        hls_seg(nid, j, warm=False)
                    got += 1
                except Exception:
                    pass
                with _PRE_LK:
                    if PRE["nid"] == nid:
                        PRE["done"] = got
        except Exception:
            pass

    threading.Thread(target=run, daemon=True).start()
    return True


def prefetch_status():
    with _PRE_LK:
        nid, done = PRE["nid"], PRE["done"]
    total = 0
    try:
        fp = os.path.join(SEGS, str(nid))
        if nid and os.path.isdir(fp):
            total = len([f for f in os.listdir(fp) if f.endswith(".ts")])
    except Exception:
        pass
    return {"note_id": nid, "done": done, "cached": total}


def _read_seg(fp):
    with open(fp, "rb") as f:
        return f.read()


def hls_seg(note_id, i, warm=True):
    """取第 i 片，解密后落盘缓存。同一片同一时刻只允许一个线程去源站拉。"""
    note_id = int(note_id)
    fp = os.path.join(SEGS, str(note_id), "%d.ts" % i)
    if os.path.exists(fp) and os.path.getsize(fp) > 188:
        return _read_seg(fp)

    key = (note_id, i)
    with _IF_LK:
        ev = _INFLIGHT.get(key)
        owner = ev is None
        if owner:
            ev = threading.Event()
            _INFLIGHT[key] = ev

    # 别人正在拉这一片 → 等它落地，不再重复打源站
    if not owner:
        ev.wait(timeout=45)
        if os.path.exists(fp) and os.path.getsize(fp) > 188:
            return _read_seg(fp)
        raise RuntimeError("inflight")

    try:
        h = hls_info(note_id)
        if i < 0 or i >= len(h["segs"]):
            raise RuntimeError("range")
        raw = xhs_dl.fetch(xhs_fast.rewrite(h["segs"][i]))
        data = xhs_dl.decrypt_seg(raw, h["key"], h["iv"]) if h["key"] else raw
        try:
            os.makedirs(os.path.dirname(fp), exist_ok=True)
            tmpf = fp + ".p"
            with open(tmpf, "wb") as f:
                f.write(data)
            os.replace(tmpf, fp)
        except Exception:
            pass
        if warm:
            _warm_ahead(note_id, i)
        return data
    finally:
        with _IF_LK:
            _INFLIGHT.pop(key, None)
        ev.set()


def _mk_item(o):
    """列表项 → 前端 item。字段不全就返回 None"""
    nid = o.get("note_id")
    if not nid or not o.get("note_cover"):
        return None
    return {"id": nid,
            "cover": o.get("note_cover"),
            "size": o.get("note_cover_size") or "",
            "dur": o.get("hd_duration") or 0,
            "title": (o.get("note_title") or "").strip(),
            "uid": o.get("user_id"),
            "uname": (o.get("user_name") or "").strip()}


def _flat(o, out):
    """递归抠出 data 里所有笔记对象"""
    if isinstance(o, dict):
        it = _mk_item(o)
        if it:
            out.append(it)
        for v in o.values():
            _flat(v, out)
    elif isinstance(o, list):
        for v in o:
            _flat(v, out)


_NET_SEM = threading.Semaphore(4)   # 服务端按 token 串行，本地限并发，避免请求堆积拖到几十秒


def _api(path, params, nolimit=False):
    try:
        if nolimit:            # 首屏搜索不吃限流，别被后台补齐挤在后面
            r = xhs2.raw_post(path, params)
        else:
            with _NET_SEM:
                r = xhs2.raw_post(path, params)
    except Exception:
        return []
    if r.get("result") != 1:
        return []
    out = []
    _flat(r.get("data"), out)
    return out


def _search_fetch(q, pn):
    """并发拿搜索原始结果。返回 (direct, raw, pool)

    实测：v2/search/note-list 的 note_type=0 与 note_type=2 返回同一批（视频优先），
    note_type=1（图文）是另一套结果，拿来挖正常作者。
    之前只打 nt=1 导致所有视频关键词（如「替父还债」）搜出 0 条——nt=1 对该词本身就没命中。
    服务端按 token 串行处理（单条 warm 约 0.5s），请求数直接决定耗时，能少发一个是一个。
    """
    box = {}
    direct, raw, pool = [], [], []
    seen, pool_seen = set(), set()

    def work(nt, pg, nolimit=True):
        try:
            box[(nt, pg)] = _api("v2/search/note-list",
                                 {"q": q, "note_sort": 0, "note_type": nt,
                                  "category_id": 0, "page": pg}, nolimit=nolimit)
        except Exception:
            box[(nt, pg)] = []

    def run(jobs, nolimit=True):
        # 千万别开线程：每个新线程都拿不到 _tls 里的 keep-alive 连接，
        # 要重新做一次 TLS 握手（约 1.5 秒），比省下的并发值钱得多。
        for nt, pg in jobs:
            work(nt, pg, nolimit)
        for nt, pg in jobs:
            for it in box.get((nt, pg), []):
                if it["id"] in seen:
                    continue
                seen.add(it["id"])
                raw.append(it)
                if it["uid"] in BL:      # 刷屏号：不进结果也不进池
                    continue
                direct.append(it)
                if it["uid"] and it["uid"] not in pool_seen:
                    pool_seen.add(it["uid"])
                    pool.append(it["uid"])

    # 服务端按账号串行，一个请求 ≈ 0.5s：先发 1 个，没货再逐级补
    run([(2, pn)])
    if not direct:
        run([(2, pn + 1)])
    if not direct:
        run([(1, pn)], nolimit=False)

    # 图文页挖到的作者不够，再花一次请求问 user-list
    if len(pool) < 4:
        try:
            r = xhs2.raw_post("v2/search/user-list", {"q": q, "page": 1})
            if r.get("result") == 1:
                for u in ((r.get("data") or {}).get("list") or []):
                    uid = u.get("user_id")
                    if uid and uid not in pool_seen and uid not in BL:
                        pool_seen.add(uid)
                        pool.append(uid)
        except Exception:
            pass

    return direct, raw, pool


def _search_grow(key, q):
    """后台补结果：拉第 2 页视频，1 个请求见效"""
    time.sleep(2.0)      # 先让前台那次搜索落地，别抢账号
    with _search_lk:
        ent = SCACHE.get(key)
        if not ent:
            return
        pool = list(ent.get("pool") or [])
        base = list(ent.get("items") or [])
        pn = int(ent.get("pn") or 1)
        seen = set(x["id"] for x in base)

    derived = []
    dlk = threading.Lock()

    def push(rows):
        with dlk:
            for x in rows:
                if x["id"] in seen:
                    continue
                seen.add(x["id"])
                derived.append(x)

    # ① 第二页视频：1 个请求，拿到就立刻写回
    try:
        rows = _api("v2/search/note-list",
                    {"q": q, "note_sort": 0, "note_type": 2,
                     "category_id": 0, "page": pn + 2})
        ok = [x for x in rows if x["uid"] not in BL]
        push(ok)
        with dlk:
            for x in ok:
                if x["uid"] and x["uid"] not in pool:
                    pool.append(x["uid"])
        if derived:
            with _search_lk:
                e2 = SCACHE.get(key)
                if e2 is not None:
                    e2["items"] = (e2["items"] + derived)[:60]
    except Exception:
        pass

    # ② 作者主页反查已去掉：服务端按账号串行，反查会跟用户的下一次搜索抢队，
    #    换来的那点结果不值得让搜索从 1 秒变 6 秒。第 2 页 + 缓存足够用。

    with _search_lk:
        ent = SCACHE.get(key)
        if ent is not None:
            final = (list(ent.get("items") or []) + derived)[:60]
            if not final:
                final = (ent.get("raw") or [])[:60]
            ent["items"] = final
            ent["grow"] = False
            ent["ts"] = time.time()
    _save_scache()


def search_items(q, pn=1, nobl=False, force=False):
    """关键词搜索 —— 返回 (items, grow)

    grow=True 表示直出结果已给，派生结果还在后台补，前端应轮询。
    第一段只要 4 个请求（约 1.5~2s），派生部分后台补，命中缓存直接 0s。
    """
    global BL
    key = "%s\x00%d" % (q, pn)

    if not nobl:
        with _search_lk:
            ent = SCACHE.get(key)
            if ent and not force:
                age = time.time() - ent["ts"]
                if ent.get("grow") and age > 60:      # 后台线程死了也别永远转圈
                    ent["grow"] = False
                if age < SCACHE_TTL or ent.get("grow"):
                    return list(ent["items"]), bool(ent.get("grow"))

    BL = load_blacklist()
    direct, raw, pool = _search_fetch(q, pn)

    if nobl:
        return raw[:60], False

    items = direct[:60]
    with _search_lk:
        SCACHE[key] = {"ts": time.time(), "items": items,
                       "grow": bool(pool), "raw": raw, "pool": pool, "pn": pn}
        if len(SCACHE) > SCACHE_MAX:
            for k, _ in sorted(SCACHE.items(), key=lambda kv: kv[1]["ts"])[:20]:
                SCACHE.pop(k, None)

    if pool:
        threading.Thread(target=_search_grow, args=(key, q), daemon=True).start()

    return items, bool(pool)


# ------------------------------------------------------------------ 作者主页
AUTH = {}            # uid -> {"name","head","total","items","ts"}
_auth_lk = threading.Lock()
AUTH_MAX_PAGES = 12  # 每个作者最多翻 12 页 × 500 = 6000 条，防爆
AUTH_TTL = 900.0


def _pick_note(o, out, seen):
    """作者主页用：只要非粉丝团、有封面的"""
    nid = o.get("note_id")
    if not nid or not o.get("note_cover"):
        return
    if (o.get("group_id") or 0) != 0:        # 粉丝团一律隐藏
        return
    if nid in seen:
        return
    seen.add(nid)
    out.append({"id": nid,
                "cover": o.get("note_cover"),
                "size": o.get("note_cover_size") or "",
                "dur": o.get("hd_duration") or 0,
                "title": (o.get("note_title") or "").strip(),
                "uid": o.get("user_id"),
                "uname": (o.get("user_name") or "").strip()})


def author_meta(uid):
    """轻量：只要名字/头像/总数，1 条就够"""
    uid = int(uid)
    try:
        r = xhs2.raw_post("v2/member/note-list",
                          {"note_type": 0, "user_id": uid, "page": 1, "page_size": 1})
        d = r.get("data") or {}
        l = d.get("list") or []
    except Exception:
        return {"uid": uid, "name": "", "head": None, "total": 0}
    if not l:
        return {"uid": uid, "name": "", "head": None, "total": 0}
    return {"uid": uid, "name": l[0].get("user_name") or "",
            "head": l[0].get("user_head_img"),
            "total": d.get("note_total") or 0}


def author_fetch(uid, force=False):
    """先给第一页，剩下的后台补齐。page_size 全程固定，否则翻页会错位"""
    uid = int(uid)
    with _auth_lk:
        a = AUTH.get(uid)
        if a and not force and time.time() - a["ts"] < AUTH_TTL:
            return a
        if a and a.get("grow"):
            return a                      # 正在后台补，先把已有的给出去

    box = {"name": "", "head": None, "total": 0}
    got = []
    PS = 200

    def work(pg):
        try:
            r = xhs2.raw_post("v2/member/note-list",
                              {"note_type": 0, "user_id": uid,
                               "page": pg, "page_size": PS})
        except Exception:
            return 0
        d = r.get("data") or {}
        lst = d.get("list") or []
        if lst:
            if not box["name"]:
                box["name"] = lst[0].get("user_name") or ""
                box["head"] = lst[0].get("user_head_img")
            if d.get("note_total"):
                box["total"] = d["note_total"]
        got.extend(lst)
        return len(lst)

    n1 = work(1)
    pages = 1
    if box["total"]:
        pages = min(AUTH_MAX_PAGES, max(1, (box["total"] + PS - 1) // PS))

    def pack():
        items, seen = [], set()
        for o in list(got):
            _pick_note(o, items, seen)
        return items

    a = {"name": box["name"], "head": box["head"], "total": box["total"],
         "items": pack(), "ts": time.time(), "grow": pages > 1}
    with _auth_lk:
        AUTH[uid] = a
    if pages <= 1 or n1 == 0:
        a["grow"] = False
        return a

    def bg():
        for i in range(2, pages + 1, 6):
            batch = list(range(i, min(i + 6, pages + 1)))
            ts = [threading.Thread(target=work, args=(p,)) for p in batch]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        with _auth_lk:
            a["items"] = pack()
            a["ts"] = time.time()
            a["grow"] = False

    threading.Thread(target=bg, daemon=True).start()
    return a


# ------------------------------------------------------------------ 播放历史
HIST_FILE = os.path.join(ROOT, "history.json")
HIST_MAX = 400
_hist_lk = threading.Lock()


def _hist_load():
    try:
        with open(HIST_FILE, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, list):
            return d
    except Exception:
        pass
    return []


def _hist_save(items):
    try:
        tmp = HIST_FILE + ".p"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False)
        os.replace(tmp, HIST_FILE)
    except Exception as e:
        print("[hist] save fail:", e)


def hist_add(it, pos=0, dur=0):
    """同一 note 只留一条，最近播的排最前。前端把标题/封面一起送上来，省一次接口。"""
    try:
        nid = int(it.get("id") or 0)
    except Exception:
        return {"ok": 0}
    if not nid:
        return {"ok": 0}
    rec = {"id": nid,
           "title": (it.get("title") or "").strip(),
           "cover": it.get("cover") or "",
           "uid": it.get("uid"),
           "uname": (it.get("uname") or "").strip(),
           "pos": float(pos or 0),
           "dur": float(dur or 0),
           "ts": time.time()}
    with _hist_lk:
        items = [x for x in _hist_load() if x.get("id") != nid]
        items.insert(0, rec)
        _hist_save(items[:HIST_MAX])
    return {"ok": 1}


def hist_list(n=200):
    with _hist_lk:
        items = _hist_load()
    return items[:n]


def hist_clear():
    with _hist_lk:
        _hist_save([])
    return {"ok": 1}


# ------------------------------------------------------------------ 下载管理
def dl_list():
    out = []
    try:
        names = os.listdir(DOWNLOADS)
    except Exception:
        return out
    for fn in names:
        if not fn.lower().endswith(".mp4"):
            continue
        fp = os.path.join(DOWNLOADS, fn)
        try:
            st = os.stat(fp)
        except Exception:
            continue
        m = re.match(r"^(\d+)_(.+)$", fn)
        nid = int(m.group(1)) if m else 0
        title = m.group(2) if m else fn[:-4]
        out.append({"name": fn, "id": nid, "title": title,
                    "size": st.st_size, "ts": st.st_mtime})
    out.sort(key=lambda x: -x["ts"])
    return out


def dl_del(name):
    fn = os.path.basename((name or "").strip())
    if not fn.lower().endswith(".mp4"):
        return {"ok": 0, "err": "bad name"}
    fp = os.path.abspath(os.path.join(DOWNLOADS, fn))
    if not fp.startswith(os.path.abspath(DOWNLOADS) + os.sep):
        return {"ok": 0, "err": "path"}
    try:
        os.remove(fp)
        return {"ok": 1, "name": fn}
    except Exception as e:
        return {"ok": 0, "err": str(e)[:80]}


MYLIB_FILE = os.path.join(ROOT, "my_lib.json")
_MYLIB = {"ts": 0.0, "data": None}
_MYLIB_TTL = 60.0


def my_lib(kind="all"):
    """读 xhs_lib.py 产出的 my_lib.json，按分区返回前端 item"""
    with _lk:
        if _MYLIB["data"] is None or time.time() - _MYLIB["ts"] > _MYLIB_TTL:
            if not os.path.exists(MYLIB_FILE):
                raise RuntimeError("my_lib.json 不存在，先跑 python xhs_lib.py")
            with open(MYLIB_FILE, encoding="utf-8") as f:
                _MYLIB["data"] = json.load(f)
            _MYLIB["ts"] = time.time()
        d = _MYLIB["data"]
    items = d.get(kind) or []
    out = []
    for it in items:
        if not it.get("id"):
            continue
        out.append({
            "id": it["id"],
            "cover": it.get("cover") or "",
            "size": it.get("cover_size") or it.get("size") or "",
            "dur": it.get("dur") or 0,
            "title": it.get("title") or "",
            "uid": it.get("uid"),
            "uname": it.get("uname") or "",
            "cin": it.get("cin") or 0,
            "gid": it.get("gid") or 0,
            "src": it.get("src") or [],
        })
    counts = {k: len(d.get(k) or []) for k in ("unlock", "like", "collect", "all")}
    return {"items": out, "counts": counts,
            "account": d.get("account") or {}, "saved_at": d.get("saved_at") or 0}


def author_items(uid, kw="", force=False):
    a = author_fetch(uid, force)
    items = a["items"]
    if kw:
        items = [x for x in items if kw in (x.get("title") or "")]
    return {"uid": int(uid), "name": a["name"], "head": a["head"],
            "total": a["total"], "n": len(a["items"]), "items": items,
            "grow": bool(a.get("grow"))}


def build_feed(n=36):
    """并发逛一圈，挑能播的"""
    import random
    global BL
    BL = load_blacklist()
    out, seen = [], set()
    lkk = threading.Lock()
    pages = list(range(1, 8))
    random.shuffle(pages)          # 每次刷新从不同的页起，首条才会真的变
    feeds = [("v2/home/discover-note", {"page": i}) for i in pages]
    feeds += [("v2/home/vip-note", {"page": 1}),
              ("v2/home/nearby-note", {"page": 1})]
    # 掺两个随机关键词，否则每次刷新的第一批顺序完全一样 =「刷新没反应」
    feeds += [("v2/search/note-list",
               {"q": kw, "note_sort": 0, "note_type": 2,
                "category_id": 0, "page": 1})
              for kw in random.sample(SRC_KEYWORDS, 2)]
    random.shuffle(feeds)
    # 只提交够用的源：每个源约 10 条，多提交的也得等它跑完才返回
    feeds = feeds[:max(2, (n + 9) // 10)]

    def absorb(o, local):
        if isinstance(o, dict):
            nid = o.get("note_id")
            if nid and o.get("note_cover"):
                if ((o.get("group_id") or 0) == 0 and o.get("is_by") != 0
                        and o.get("user_id") not in BL):
                    local.append({"id": nid, "cover": o.get("note_cover"),
                                  "size": o.get("note_cover_size") or "",
                                  "dur": o.get("hd_duration") or 0,
                                  "title": (o.get("note_title") or "").strip(),
                                  "uid": o.get("user_id"),
                                  "uname": (o.get("user_name") or "").strip()})
            for v in o.values():
                absorb(v, local)
        elif isinstance(o, list):
            for v in o:
                absorb(v, local)

    def work(path, prm):
        try:
            r = xhs2.raw_post(path, prm)
        except Exception:
            return
        local = []
        absorb(r.get("data"), local)
        if not local:
            return
        with lkk:
            for it in local:
                if len(out) >= n:
                    break
                if it["id"] not in seen:
                    seen.add(it["id"])
                    out.append(it)

    # 服务端按账号串行，本地再并发只会互相抢队（实测 9 并发 7.1s vs 串行 3.6s）。
    # 这里老老实实一个一个打，6 个源约 2.4 秒。
    for path, prm in feeds:
        work(path, prm)
        if len(out) >= n:
            break

    random.shuffle(out)
    return out[:n]


_build_lk = threading.Lock()


def refresh_feed(n=36, force=False, wait=8.0):
    """双检锁：只有第一个线程真去抓，其余等它出结果（最多等 wait 秒）"""
    if not force:
        with _lk:
            if FEED["items"] and time.time() - FEED["ts"] < 600:
                return list(FEED["items"])
    if not _build_lk.acquire(timeout=wait):
        with _lk:
            return list(FEED["items"])          # 还在抓，先把现有的给出去
    try:
        with _lk:
            if FEED["items"] and time.time() - FEED["ts"] < 600 and not force:
                return list(FEED["items"])
        xhs2.ensure_account()
        items = build_feed(n)
        with _lk:
            FEED["items"] = items
            FEED["ts"] = time.time()
        return items
    finally:
        _build_lk.release()


_refresh_state = {"busy": False, "gen": 0, "ts": 0.0}
_rf_lk = threading.Lock()


def refresh_async():
    """立刻返回，后台真抓。前端轮询 /api/feed 看首条变了就重绘。"""
    with _rf_lk:
        if _refresh_state["busy"]:
            return {"ok": 1, "busy": 1, "gen": _refresh_state["gen"]}
        _refresh_state["busy"] = True

    def run():
        t0 = time.time()
        try:
            # 不走 refresh_feed：那个要排 _build_lk，预热线程占着就会一直等到超时
            xhs2.ensure_account()
            items = build_feed(20)
            with _lk:
                FEED["items"] = items
                FEED["ts"] = time.time()
            print("[refresh] ok %d 条 %.1fs" % (len(items), time.time() - t0), flush=True)
        except Exception as e:
            print("[refresh] fail %.1fs %s" % (time.time() - t0, repr(e)[:140]), flush=True)
        finally:
            with _rf_lk:
                _refresh_state["busy"] = False
                _refresh_state["gen"] += 1
                _refresh_state["ts"] = time.time()

    threading.Thread(target=run, daemon=True).start()
    with _rf_lk:
        return {"ok": 1, "busy": 1, "gen": _refresh_state["gen"]}


# ------------------------------------------------------------------ HTML
HTML = r"""<!doctype html><html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>·</title>
<style>
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{background:#000;overflow-x:hidden}
body{font:0/0 a}
#wall{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:3px;padding:3px}
.cell{position:relative;aspect-ratio:9/16;overflow:hidden;background:#111;cursor:pointer;border-radius:2px}
.cell img{width:100%;height:100%;object-fit:cover;display:block;transition:transform .35s,filter .35s;filter:saturate(.95)}
.cell:hover img{transform:scale(1.04);filter:saturate(1.1) brightness(.75)}
.cell .play{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;opacity:0;transition:opacity .2s;pointer-events:none}
.cell:hover .play{opacity:1}
.play svg{width:46px;height:46px;fill:#fff;filter:drop-shadow(0 2px 8px #000)}
.cell .dl{position:absolute;right:6px;top:6px;width:30px;height:30px;border-radius:50%;
 background:rgba(0,0,0,.55);backdrop-filter:blur(4px);display:flex;align-items:center;justify-content:center;
 opacity:.7;transition:.2s;pointer-events:auto}
.cell:hover .dl{opacity:1;background:rgba(255,255,255,.3)}
.cell .dl:hover{background:rgba(255,255,255,.25)}
.dl svg{width:15px;height:15px;fill:#fff}
.cell .tt{position:absolute;left:0;right:0;bottom:0;padding:30px 7px 7px;
 font:12px/1.35 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;color:#fff;text-shadow:0 1px 3px #000;
 background:linear-gradient(to top,rgba(0,0,0,.88),rgba(0,0,0,.5) 55%,transparent);
 display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;
 pointer-events:none;word-break:break-all}
.cell .q{position:absolute;left:6px;top:6px;width:7px;height:7px;border-radius:50%;background:#3a3;box-shadow:0 0 4px #000}
.cell .q.busy{background:#fa3;animation:bl 1s infinite}
.cell .q.err{background:#f33}
@keyframes bl{50%{opacity:.25}}
.cell.done::after{content:"";position:absolute;inset:0;border:2px solid #3a3;pointer-events:none}
#more{padding:26px;text-align:center}
#more i{display:inline-block;width:26px;height:26px;border-radius:50%;border:2px solid #222;border-top-color:#666;animation:sp .8s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}
#lb{position:fixed;inset:0;background:#000;display:none;z-index:50;align-items:center;justify-content:center}
#lb.on{display:flex}
#vid{max-width:100%;max-height:100%;background:#000;outline:0}
#x{position:fixed;top:12px;right:14px;width:40px;height:40px;border-radius:50%;background:rgba(255,255,255,.1);
 display:flex;align-items:center;justify-content:center;cursor:pointer;z-index:60;backdrop-filter:blur(6px)}
#x:hover{background:rgba(255,255,255,.22)}
#x svg{width:18px;height:18px;stroke:#fff;stroke-width:2;fill:none}
#d2{position:fixed;bottom:18px;right:18px;width:46px;height:46px;border-radius:50%;background:rgba(255,255,255,.12);
 display:none;align-items:center;justify-content:center;cursor:pointer;z-index:60;backdrop-filter:blur(6px)}
#d2.on{display:flex}
#d2:hover{background:rgba(255,255,255,.25)}
#d2 svg{width:19px;height:19px;fill:#fff}
#lu{position:absolute;top:9px;left:14px;right:66px;z-index:60;cursor:pointer;
 font:12px/1.3 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;color:#8fb6ff;
 text-shadow:0 1px 4px #000;opacity:.9;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#lu:active{opacity:.55}
#lu:empty{display:none}
#lt{position:absolute;top:29px;left:14px;right:66px;color:#fff;z-index:60;pointer-events:none;
 font:13px/1.45 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;text-shadow:0 1px 4px #000;opacity:.92;
 display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;word-break:break-all}
#wait{position:fixed;inset:0;display:none;align-items:center;justify-content:center;z-index:55;pointer-events:none}
#wait.on{display:flex}
#wait div{width:34px;height:34px;border-radius:50%;border:2px solid #222;border-top-color:#ddd;animation:sp .8s linear infinite}
#top{position:fixed;top:10px;left:12px;right:12px;z-index:40;display:flex;gap:8px;align-items:center;pointer-events:none}
#q{flex:0 1 300px;height:38px;border-radius:19px;background:rgba(18,18,18,.72);backdrop-filter:blur(8px);
 border:1px solid rgba(255,255,255,.09);color:#ddd;font:13px/1 -apple-system,BlinkMacSystemFont,sans-serif;
 padding:0 16px;outline:0;pointer-events:auto;transition:border-color .2s,background .2s}
#q::placeholder{color:#5a5a5a}
#q:focus{border-color:rgba(255,255,255,.35);background:rgba(30,30,30,.9)}
#rf{margin-left:auto;width:38px;height:38px;border-radius:50%;background:rgba(20,20,20,.7);backdrop-filter:blur(6px);
 display:flex;align-items:center;justify-content:center;cursor:pointer;opacity:.5;transition:.2s;pointer-events:auto}
#rf:hover{opacity:1;background:rgba(60,60,60,.85)}
#rf svg{width:16px;height:16px;fill:none;stroke:#fff;stroke-width:2}
#rf.spin svg{animation:sp .7s linear infinite}
#toast{position:fixed;left:50%;bottom:30px;transform:translateX(-50%) translateY(20px);background:rgba(255,255,255,.14);
 backdrop-filter:blur(10px);color:#fff;font:13px/1 -apple-system,sans-serif;padding:11px 18px;border-radius:20px;
 opacity:0;transition:.25s;pointer-events:none;z-index:99;white-space:nowrap;max-width:80vw;overflow:hidden;text-overflow:ellipsis}
#toast.on{opacity:1;transform:translateX(-50%) translateY(0)}
#au{position:fixed;inset:0;background:#000;z-index:45;display:none;overflow-y:auto;
 -webkit-overflow-scrolling:touch;overscroll-behavior:contain}
#au.on{display:block}
#autop{position:sticky;top:0;z-index:3;display:flex;gap:8px;align-items:center;padding:8px 10px;
 background:linear-gradient(to bottom,rgba(0,0,0,.97),rgba(0,0,0,.8) 65%,transparent)}
#aubk{flex:0 0 auto;width:34px;height:34px;border-radius:50%;background:rgba(32,32,32,.85);
 display:flex;align-items:center;justify-content:center;cursor:pointer;color:#fff;
 font:600 19px/1 system-ui,sans-serif;user-select:none}
#aubk:active{background:rgba(70,70,70,.9)}
#aunm{flex:0 1 auto;max-width:34vw;font:13px/1.2 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
 color:#d0d0d0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#auq{flex:1 1 90px;min-width:0;height:32px;border-radius:16px;background:rgba(18,18,18,.85);
 border:1px solid rgba(255,255,255,.1);color:#ddd;font:12px/1 -apple-system,sans-serif;
 padding:0 12px;outline:0}
#auq::placeholder{color:#555}
#auq:focus{border-color:rgba(255,255,255,.32)}
#aurf{flex:0 0 auto;width:32px;height:32px;border-radius:50%;background:rgba(32,32,32,.85);
 display:flex;align-items:center;justify-content:center;cursor:pointer}
#aurf svg{width:14px;height:14px;fill:none;stroke:#fff;stroke-width:2}
#aurf.spin svg{animation:sp .7s linear infinite}
#auwall{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:3px;padding:3px}
#auwall .cell{cursor:pointer}
#aumore{padding:26px 16px 40px;text-align:center;color:#5a5a5a;
 font:12px/1.6 -apple-system,"Microsoft YaHei",sans-serif}
#aumore i{display:inline-block;width:26px;height:26px;border-radius:50%;
 border:2px solid #222;border-top-color:#666;animation:sp .8s linear infinite}
</style></head><body>

<div id="top"><input id="q" placeholder="搜索" autocomplete="off" spellcheck="false"><div id="rf"><svg viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-2.6-6.4M21 3v6h-6"/></svg></div></div>
<div id="wall"></div>
<div id="more"><i></i></div>

<div id="au">
  <div id="autop">
    <div id="aubk">&#8249;</div>
    <div id="aunm"></div>
    <input id="auq" placeholder="搜这个作者的作品" autocomplete="off" spellcheck="false">
    <div id="aurf"><svg viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-2.6-6.4M21 3v6h-6"/></svg></div>
  </div>
  <div id="auwall"></div>
  <div id="aumore"><i></i></div>
</div>

<div id="lb">
  <video id="vid" playsinline webkit-playsinline controls></video>
  <div id="lu"></div>
  <div id="lt"></div>
  <div id="x"><svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg></div>
  <div id="d2"><svg viewBox="0 0 24 24"><path d="M12 3v12m0 0l-4.5-4.5M12 15l4.5-4.5M4 19h16"/></svg></div>
  <div id="wait"><div></div></div>
</div>

<div id="toast"></div>

<script src="/hls.js"></script>
<script>
const wall=document.getElementById('wall'),more=document.getElementById('more');
const lb=document.getElementById('lb'),vid=document.getElementById('vid');
const wait=document.getElementById('wait'),d2=document.getElementById('d2');
const lt=document.getElementById('lt'),lu=document.getElementById('lu');
const qi=document.getElementById('q'),rf=document.getElementById('rf');
let cur=null,busy=false,page=0,mode='feed',hls=null,noMore=false;
const au=document.getElementById('au'),auwall=document.getElementById('auwall'),
      aumore=document.getElementById('aumore'),aunm=document.getElementById('aunm'),
      auq=document.getElementById('auq'),aurf=document.getElementById('aurf'),
      aubk=document.getElementById('aubk');
let auStack=[],auUid=null,auName='',auKw='',auBusy=false,luUid=null;
const AU_CACHE={};

const ICON_PLAY='<svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>';
const ICON_DL='<svg viewBox="0 0 24 24"><path d="M12 3v12m0 0l-4.5-4.5M12 15l4.5-4.5M4 19h16"/></svg>';

function toast(t){const e=document.getElementById('toast');e.textContent=t;e.classList.add('on');
 clearTimeout(e._t);e._t=setTimeout(()=>e.classList.remove('on'),2200)}

function addCells(items,box){
  const host=box||wall;
  const frag=document.createDocumentFragment();
  for(const it of items){
    const c=document.createElement('div');c.className='cell';c.dataset.id=it.id;
    if(it.uid)c.dataset.uid=it.uid;
    if(it.uname)c.dataset.uname=it.uname;
    const im=document.createElement('img');im.loading='lazy';im.src='/thumb/'+it.id;
    if(it.size){const[ww,hh]=it.size.split('*').map(Number);if(ww&&hh)im.style.aspectRatio=ww+'/'+hh;}
    const pl=document.createElement('div');pl.className='play';pl.innerHTML=ICON_PLAY;
    const tt=document.createElement('div');tt.className='tt';tt.textContent=it.title||'';
    const d=document.createElement('div');d.className='dl';d.innerHTML=ICON_DL;
    const dt=document.createElement('div');dt.className='q';
    d.onclick=e=>{e.stopPropagation();dl(it.id,c)};
    c.onclick=()=>open(it.id);
    c.append(im,pl,tt,d,dt);frag.appendChild(c);
  }
  host.appendChild(frag);
}

async function load(){
  if(busy||noMore)return;
  if(au.classList.contains('on'))return;
  busy=true;
  try{
    const kw=qi.value.trim();
    if(mode==='search'&&!kw){mode='feed';}
    const url = mode==='search'
      ? '/api/search?q='+encodeURIComponent(kw)+'&p='+(++page)
      : '/api/feed?n=36&p='+(++page);
    const r=await fetch(url);
    const j=await r.json();
    if(j.err){more.textContent='读取失败：'+j.err;busy=false;return}
    if(!j.items.length){
      if(j.pending){
        more.textContent='正在拉取…';
        busy=false;
        setTimeout(()=>{page=0;load()},1500);
        return;
      }
      if(mode==='search'){
        noMore=true;
        more.textContent=page===1?'没搜到':'搜索到底了';
      }else{
        page=0;more.textContent='到头了 · 点右上角换一批';
      }
      busy=false;return;
    }
    more.textContent='';
    addCells(j.items);
    if(j.unfiltered)toast('这个词被刷屏号占满了，已显示未过滤结果');
  }catch(e){more.textContent='网络错误，检查服务是否还活着'}
  busy=false;
}

async function dl(id,cell){
  await fetch('/api/dl/'+id,{method:'POST'});
  toast('已加入下载');
  const q=cell?cell.querySelector('.q'):null;
  if(q)q.className='q busy';
  const t=setInterval(async()=>{
    const j=await (await fetch('/api/job?k=dl:'+id)).json();
    if(j.state==='ready'){clearInterval(t);if(q)q.className='q';if(cell)cell.classList.add('done');toast('存好了 '+j.msg)}
    else if(j.state==='error'){clearInterval(t);if(q)q.className='q err';toast('下载失败')}
  },1200);
}

function open(id){
  const wasPlaying=!!cur;
  cur=id;lb.classList.add('on');d2.classList.add('on');
  const cc=document.querySelector('.cell[data-id="'+id+'"]');
  const ttt=cc?cc.querySelector('.tt'):null;
  lt.textContent=ttt?ttt.textContent:'';
  luUid=cc&&cc.dataset.uid?Number(cc.dataset.uid):null;
  lu.textContent=cc&&cc.dataset.uname?cc.dataset.uname:'';
  if(!wasPlaying){rememberScroll();history.pushState({au:auStack.slice(),play:true},'');}
  if(hls){try{hls.destroy()}catch(e){};hls=null}
  vid.removeAttribute('src');try{vid.load()}catch(e){}
  wait.classList.add('on');
  const src='/hls/'+id+'/index.m3u8';
  const fallback=()=>{
    wait.classList.remove('on');
    vid.src=src;
    vid.play().catch(()=>{});
  };
  if(window.Hls&&Hls.isSupported()){
    hls=new Hls({maxBufferLength:40,maxMaxBufferLength:80,manifestLoadingMaxRetry:1,
                 fragLoadingMaxRetry:3,startLevel:0});
    hls.on(Hls.Events.MANIFEST_PARSED,()=>{
      wait.classList.remove('on');
      vid.play().catch(()=>{});
    });
    hls.on(Hls.Events.ERROR,(ev,d)=>{
      if(!d.fatal)return;
      if(d.type===Hls.ErrorTypes.NETWORK_ERROR){hls.startLoad();return}
      wait.classList.remove('on');
      toast('这条打不开');
      close();
    });
    hls.loadSource(src);
    hls.attachMedia(vid);
    setTimeout(()=>wait.classList.remove('on'),2500);
  }else{
    fallback();
  }
}

function closePlayer(){
  const was=!!cur;
  lb.classList.remove('on');
  if(hls){try{hls.destroy()}catch(e){};hls=null}
  vid.pause();vid.removeAttribute('src');try{vid.load()}catch(e){}
  cur=null;wait.classList.remove('on');
  return was;
}
function close(){
  const was=closePlayer();
  const st=history.state;
  if(was&&st&&st.play)history.back();
}
lu.onclick=()=>{
  if(!luUid)return;
  const uid=luUid,nm=lu.textContent||'';
  const st=history.state||{};
  closePlayer();
  if(st.play){
    /* 从播放器直接跳作者：
       先把「播放」这条改写成「墙」，再 push 一条「作者」。
       这样返回一次 = 回墙，而不是直接退出网页。 */
    history.replaceState({au:[],play:false,sy:st.sy||0,asy:0},'');
    auStack=[uid];
    history.pushState({au:auStack.slice(),play:false,sy:st.sy||0,asy:0},'');
    showAuthor(uid,nm,null);
  }else{
    auGo(uid,nm,null);
  }
};
document.getElementById('x').onclick=close;
d2.onclick=()=>{if(cur)dl(cur,null)};
document.addEventListener('keydown',e=>{
  if(e.key==='Escape')close();
  if(!cur)return;
  if(e.key===' '){e.preventDefault();vid.paused?vid.play():vid.pause()}
  if(e.key==='ArrowRight')vid.currentTime+=5;
  if(e.key==='ArrowLeft')vid.currentTime-=5;
});
rf.onclick=async()=>{
  if(rf.classList.contains('spin'))return;
  rf.classList.add('spin');toast('换一批…');
  mode='feed';noMore=false;page=0;
  wall.innerHTML='';more.innerHTML='<i></i>';
  try{await fetch('/api/refresh',{method:'POST'})}catch(e){}
  await load();
  rf.classList.remove('spin');
};

qi.addEventListener('keydown',e=>{
  if(e.key!=='Enter')return;
  const v=qi.value.trim();
  if(!v){qi.blur();return}
  mode='search';noMore=false;page=0;
  wall.innerHTML='';more.innerHTML='<i></i>';
  load();qi.blur();
});

qi.addEventListener('input',()=>{
  if(!qi.value.trim()&&mode==='search'){
    mode='feed';noMore=false;page=0;
    wall.innerHTML='';more.innerHTML='<i></i>';
    load();
  }
});

/* ---------------- 作者主页 ---------------- */
function rememberScroll(){
  const st=history.state;
  if(st){st.sy=window.scrollY;st.asy=au.scrollTop;st.auq=auq.value;}
}

function auGo(uid,name,noteId){
  rememberScroll();
  auStack=auStack.concat([uid]);
  history.pushState({au:auStack.slice(),play:!!cur,asy:0},'');
  showAuthor(uid,name,noteId);
}

function showAuthor(uid,name,noteId){
  auUid=uid;auName=name||'';auKw='';auq.value='';
  aunm.textContent=auName||('#'+uid);
  au.classList.add('on');
  auwall.innerHTML='';
  aumore.innerHTML='<i></i>';
  au.scrollTop=0;
  renderAuthor(noteId);
}

async function renderAuthor(noteId){
  if(auBusy)return;auBusy=true;
  const uid=auUid,kw=auKw,key=uid+'|'+kw;
  try{
    let d=AU_CACHE[key];
    if(!d){
      const url='/api/author/'+uid+(kw?('?q='+encodeURIComponent(kw)):'');
      const j=await (await fetch(url)).json();
      if(j.err){aumore.textContent='读取失败：'+j.err;auBusy=false;return}
      d=AU_CACHE[key]=j;
    }
    if(auUid!==uid)return;
    auName=d.name||auName;
    aunm.textContent=auName||('#'+uid);
    let items=(d.items||[]).slice();
    if(noteId&&!kw){
      const k=items.findIndex(x=>x.id===noteId);
      if(k>0)items=items.slice(k).concat(items.slice(0,k));
    }
    auwall.innerHTML='';
    if(!items.length){
      aumore.textContent=kw?('没有标题含「'+kw+'」的'):'这个作者没有非粉丝团的视频';
      auBusy=false;return;
    }
    addCells(items,auwall);
    if(d.grow&&!kw){
      aumore.textContent='已载入 '+items.length+' 条 · 还在拉剩下的…';
      pollAuthor(uid,key);
    }else{
      aumore.textContent=(d.n&&d.n>items.length&&!kw)
        ?('已载入 '+items.length+' / 共 '+d.n+' 条 · 点右上刷新')
        :('共 '+items.length+' 条');
    }
  }catch(e){aumore.textContent='网络错误'}
  auBusy=false;
}

let auPoll=null;
function pollAuthor(uid,key){
  if(auPoll)clearInterval(auPoll);
  let n=0;
  auPoll=setInterval(async()=>{
    n++;
    if(n>40||auUid!==uid||!au.classList.contains('on')){clearInterval(auPoll);auPoll=null;return}
    try{
      const j=await (await fetch('/api/author/'+uid)).json();
      if(j.err||auUid!==uid)return;
      const prev=(AU_CACHE[key]||{});
      const pn=(prev.items||[]).length;
      delete AU_CACHE[key];
      AU_CACHE[key]=j;
      if(!j.grow&&(j.items||[]).length!==pn){
        clearInterval(auPoll);auPoll=null;
        if(!auKw){renderAuthor(null);}
      }else if(!j.grow){
        clearInterval(auPoll);auPoll=null;
        aumore.textContent='共 '+(j.items||[]).length+' 条';
      }else if((j.items||[]).length>pn){
        aumore.textContent='已载入 '+(j.items||[]).length+' 条 · 还在拉剩下的…';
      }
    }catch(e){}
  },1500);
}

aubk.onclick=()=>history.back();
aurf.onclick=async()=>{
  if(aurf.classList.contains('spin'))return;
  aurf.classList.add('spin');
  delete AU_CACHE[auUid+'|'+auKw];
  try{
    const j=await (await fetch('/api/author/'+auUid+'?f=1'+
      (auKw?('&q='+encodeURIComponent(auKw)):''))).json();
    AU_CACHE[auUid+'|'+auKw]=j;
  }catch(e){}
  await renderAuthor(null);
  aurf.classList.remove('spin');
};
auq.addEventListener('keydown',e=>{
  if(e.key!=='Enter')return;
  auKw=auq.value.trim();
  delete AU_CACHE[auUid+'|'+auKw];
  auwall.innerHTML='';aumore.innerHTML='<i></i>';
  renderAuthor(null);
  auq.blur();
});
auq.addEventListener('input',()=>{
  if(!auq.value.trim()&&auKw){
    auKw='';auwall.innerHTML='';aumore.innerHTML='<i></i>';renderAuthor(null);
  }
});

window.addEventListener('popstate',e=>{
  const st=e.state||{au:[],play:false};
  const tg=st.au||[];
  if(cur&&!st.play)closePlayer();
  auStack=tg.slice();
  if(!tg.length){
    au.classList.remove('on');auUid=null;
  }else{
    const uid=tg[tg.length-1];
    if(auUid!==uid)showAuthor(uid,'',null);
    else au.classList.add('on');
  }
  const restore=()=>{
    if(typeof st.sy==='number')window.scrollTo(0,st.sy);
    if(typeof st.asy==='number'&&au.classList.contains('on'))au.scrollTop=st.asy;
    if(typeof st.auq==='string'&&auq.value!==st.auq){auq.value=st.auq;auKw=st.auq.trim();}
  };
  requestAnimationFrame(restore);
  setTimeout(restore,80);
  setTimeout(restore,260);
});

history.replaceState({au:[],play:false,sy:0,asy:0},'');

new IntersectionObserver(es=>{if(es[0].isIntersecting)load()},{rootMargin:'600px'}).observe(more);
load();
</script></body></html>"""


# ------------------------------------------------------------------ HTTP
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "p"

    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # 让 file:// 直接打开页面也能取数据（origin 为 null）
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_OPTIONS(self):
        self._send(204, b"", "text/plain")

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def do_POST(self):
        u = urlparse(self.path)
        p = u.path
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except Exception:
            n = 0
        body = {}
        if n:
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8")) or {}
            except Exception:
                body = {}
        try:
            if p == "/api/refresh":
                return self._json(refresh_async())
            if p == "/api/hist":
                return self._json(hist_add(body, body.get("pos"), body.get("dur")))
            if p == "/api/hist/clear":
                return self._json(hist_clear())
            if p == "/api/dlm/del":
                return self._json(dl_del(body.get("name")))
            m = re.match(r"^/api/prep/(\d+)$", p)
            if m:
                prep(int(m.group(1)))
                return self._json({"ok": 1})
            m = re.match(r"^/api/dl/(\d+)$", p)
            if m:
                do_download(int(m.group(1)))
                return self._json({"ok": 1})
        except Exception as e:
            return self._json({"err": str(e)[:100]}, 500)
        self._send(404, b"")

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        q = parse_qs(u.query)

        try:
            if p == "/":
                return self._send(200, HTML.encode("utf-8"), "text/html; charset=utf-8")

            if p in ("/preview", "/preview.html", "/ui-preview.html"):
                fp = os.path.join(ROOT, "ui-preview.html")
                if not os.path.exists(fp):
                    return self._send(404, b"ui-preview.html not found")
                with open(fp, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8",
                                      {"Cache-Control": "no-store"})

            if p == "/api/feed":
                n = int((q.get("n") or ["36"])[0])
                pn = int((q.get("p") or ["1"])[0])
                try:
                    items = refresh_feed(60, wait=1.2)
                except Exception as e:
                    with _lk:
                        items = list(FEED["items"])
                    if not items:
                        return self._json({"items": [], "total": 0, "err": repr(e)[:150]})
                total = len(items)
                if not total:
                    return self._json({"items": [], "total": 0, "pending": 1})
                start = (pn - 1) * n
                if start >= total:
                    start = 0                      # 到底回绕，无限滚
                return self._json({"items": items[start:start + n], "total": total})

            if p == "/hls.js":
                if not os.path.exists(HLSJS):
                    return self._send(404, b"")
                with open(HLSJS, "rb") as f:
                    return self._send(200, f.read(), "application/javascript",
                                      {"Cache-Control": "max-age=86400"})

            if p == "/api/search":
                kw = unquote((q.get("q") or [""])[0]).strip()
                pn = int((q.get("p") or ["1"])[0])
                force = (q.get("f") or ["0"])[0] == "1"
                if not kw:
                    return self._json({"items": [], "total": 0})
                t0 = time.time()
                try:
                    items, grow = search_items(kw, pn, force=force)
                    filler = 0
                    if not items:
                        # 全被刷屏号占了：立刻退回未过滤，别让用户看空白
                        items, _ = search_items(kw, pn, nobl=True)
                        filler = 1
                except Exception as e:
                    return self._json({"items": [], "err": repr(e)[:120]})
                return self._json({"items": items, "total": len(items),
                                   "q": kw, "p": pn, "unfiltered": filler,
                                   "grow": bool(grow), "ms": int((time.time() - t0) * 1000)})

            m = re.match(r"^/api/prefetch/(\d+)$", p)
            if m:
                return self._json({"ok": 1 if prefetch(int(m.group(1))) else 0,
                                   **prefetch_status()})

            if p == "/api/hist":
                n2 = int((q.get("n") or ["200"])[0] or 200)
                return self._json({"items": hist_list(n2)})
            if p == "/api/dlm":
                items = dl_list()
                return self._json({"items": items, "count": len(items),
                                   "bytes": sum(x["size"] for x in items)})
            m = re.match(r"^/dlf/(.+\.mp4)$", p)
            if m:
                fn = os.path.basename(unquote(m.group(1)))
                fp = os.path.abspath(os.path.join(DOWNLOADS, fn))
                if not fp.startswith(os.path.abspath(DOWNLOADS) + os.sep) or not os.path.exists(fp):
                    return self._send(404, b"")
                return self._send_file(fp, "video/mp4")
            if p == "/api/my":
                kind = (q.get("k") or ["all"])[0]
                try:
                    return self._json(my_lib(kind))
                except Exception as e:
                    return self._json({"items": [], "counts": {}, "err": repr(e)[:140]})

            m = re.match(r"^/api/author/(\d+)$", p)
            if m:
                uid = int(m.group(1))
                kw2 = unquote((q.get("q") or [""])[0]).strip()
                force = (q.get("f") or ["0"])[0] == "1"
                try:
                    r = author_items(uid, kw2, force)
                except Exception as e:
                    return self._json({"items": [], "err": repr(e)[:140]})
                anchor = (q.get("a") or [""])[0]
                items = r["items"]
                if anchor and not kw2:
                    # 从点进来的那条开始（往下给，不够再从头补）
                    try:
                        ai = int(anchor)
                        k = next((i for i, x in enumerate(items) if x["id"] == ai), None)
                        if k:
                            items = items[k:] + items[:k]
                    except Exception:
                        pass
                return self._json({"uid": r["uid"], "name": r["name"], "head": r["head"],
                                   "total": r["total"], "n": r["n"], "grow": r["grow"],
                                   "items": items})

            m = re.match(r"^/hls/(\d+)/index\.m3u8$", p)
            if m:
                nid = int(m.group(1))
                try:
                    d = note_detail(nid)
                    media = d.get("note_media_url") or ""
                    if not media:
                        return self._send(404, b"")
                    media = xhs_fast.rewrite(media)
                    if not media.endswith(".m3u8"):
                        return self._send(404, b"locked")
                    txt = xhs_dl.fetch(media).decode("utf-8", "replace")
                    hls_info(nid)
                    outl, i = [], 0
                    for line in txt.splitlines():
                        s = line.strip()
                        if s.startswith("#EXT-X-KEY"):
                            continue
                        if s and not s.startswith("#"):
                            outl.append("s/%d.ts" % i)
                            i += 1
                            continue
                        outl.append(line)
                    body = ("\n".join(outl) + "\n").encode()
                    return self._send(200, body, "application/vnd.apple.mpegurl")
                except Exception as e:
                    return self._send(500, repr(e)[:100].encode())
                return

            m = re.match(r"^/hls/(\d+)/s/(\d+)\.ts$", p)
            if m:
                nid, idx = int(m.group(1)), int(m.group(2))
                try:
                    data = hls_seg(nid, idx)
                except Exception as e:
                    return self._send(404, repr(e)[:80].encode())
                return self._send(200, data, "video/mp2t",
                                  {"Cache-Control": "max-age=3600"})

            if p == "/api/job":
                k = (q.get("k") or [""])[0]
                with _lk:
                    j = JOBS.get(k)
                return self._json(j or {"state": "none"})

            m = re.match(r"^/thumb/(\d+)$", p)
            if m:
                nid = m.group(1)
                fpo = os.path.join(THUMBS, nid + ".jpg")
                fpp = os.path.join(THUMBS, nid + "_s.jpg")
                if os.path.exists(fpp):
                    data = open(fpp, "rb").read()
                else:
                    if os.path.exists(fpo):
                        raw = open(fpo, "rb").read()
                    else:
                        d = note_detail(nid)
                        cov = d.get("note_cover") or ""
                        raw = fetch_image(cov) if cov else None
                        if not raw:
                            with _lk:
                                it = next((x for x in FEED["items"] if str(x["id"]) == nid), None)
                            if it and it.get("cover"):
                                raw = fetch_image(it["cover"])
                        if not raw:
                            return self._send(404, b"")
                        try:
                            open(fpo, "wb").write(raw)
                        except Exception:
                            pass
                    data = shrink_jpeg(raw)
                    try:
                        open(fpp, "wb").write(data)
                    except Exception:
                        pass
                return self._send(200, data, "image/jpeg", {"Cache-Control": "max-age=86400"})

            m = re.match(r"^/media/(\d+)\.mp4$", p)
            if m:
                nid = int(m.group(1))
                fp = os.path.join(VIDEOS, "%d.mp4" % nid)
                if not os.path.exists(fp):
                    return self._send(404, b"")
                return self._send_file(fp, "video/mp4")

        except Exception as e:
            try:
                return self._send(500, str(e).encode()[:200])
            except Exception:
                return

        self._send(404, b"")

    def _send_file(self, fp, ctype):
        size = os.path.getsize(fp)
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        code = 200
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
                code = 206
        n = max(0, end - start + 1)
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(n))
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        try:
            with open(fp, "rb") as f:
                f.seek(start)
                left = n
                while left > 0:
                    chunk = f.read(min(262144, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except Exception:
            pass


def _lan_ips():
    ips = set()
    try:
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        u.connect(("223.5.5.5", 80))
        ips.add(u.getsockname()[0])
        u.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    return sorted(x for x in ips
                  if not x.startswith(("127.", "169.254.", "198.18.")))


def main():
    port = 8770
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except Exception:
            pass
    print("准备账号...")
    xhs2.ensure_account()
    _load_scache()
    print("  搜索缓存 %d 个词" % len(SCACHE))

    def warm():
        try:
            items = refresh_feed(60, wait=600)
            print("  预热 %d 条" % len(items))
        except Exception as e:
            print("  预热失败:", e)
        # 热词预热：只挑 3 个，而且等首屏出来一阵再跑。
        # 服务端按账号串行，预热开太多会把用户自己的搜索挤到排队。
        time.sleep(10)
        try:
            for kw in ("黑丝", "偷拍", "自慰"):
                search_items(kw, 1)
                time.sleep(1.5)
            print("  热词已预热")
        except Exception as e:
            print("  热词预热失败:", e)

    threading.Thread(target=warm, daemon=True).start()

    srv = ThreadingHTTPServer(("0.0.0.0", port), H)
    srv.daemon_threads = True
    print()
    print("  本机    http://127.0.0.1:%d/" % port)
    for ip in _lan_ips():
        print("  局域网  http://%s:%d/" % (ip, port))
    print()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
