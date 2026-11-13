# -*- coding: utf-8 -*-
"""
把指定账号的 解锁 / 喜欢 / 收藏 / 收藏专辑 全量拉下来，存成 my_lib.json

用法:
    python xhs_lib.py                 # 用 creds_186.json 里的号
    python xhs_lib.py --who           # 只看账号信息
    python xhs_lib.py --cred xxx.json
"""
import json
import os
import sys
import time

import xhs2

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "my_lib.json")
DEF_CRED = os.path.join(ROOT, "creds_186.json")

PATHS = {
    "unlock":  "v2/member/pucha-note-list",     # 已解锁（花过币）
    "like":    "v2/member/like-note-list",      # 喜欢
    "collect": "v2/member/collect-note-list",   # 收藏笔记
}
ALBUM = "v2/member/collect-album-list"
ALBUM_NOTES = "v2/member/collect-album-note-list"


def use_account(cred_path):
    """切到指定账号的 token，返回原 STATE 备份"""
    j = json.load(open(cred_path, encoding="utf-8"))
    d = j.get("data") or {}
    tok = d.get("user_token") or j.get("user_token")
    h = d.get("user_hash") or j.get("user_hash")
    if not tok:
        raise SystemExit("creds 里没有 user_token: %s" % cred_path)
    old = dict(xhs2.STATE)
    xhs2.STATE["token"] = tok
    xhs2.STATE["hash"] = h or ""
    return old, tok, h


def restore(old):
    xhs2.STATE.clear()
    xhs2.STATE.update(old)
    xhs2._drop_conn()


def pick(o):
    """列表项 -> 精简记录"""
    cover = o.get("note_cover") or o.get("note_thumbnail") or ""
    return {
        "id": o.get("note_id"),
        "title": (o.get("note_title") or "").strip(),
        "cover": cover,
        "cover_size": o.get("note_cover_size") or "",
        "dur": o.get("hd_duration") or 0,
        "cin": o.get("note_cin") or 0,
        "gid": o.get("group_id") or 0,
        "type": o.get("note_type") or 0,
        "uid": o.get("user_id"),
        "uname": (o.get("user_name") or "").strip(),
        "likes": o.get("like_count") or 0,
    }


def walk(path, extra=None, max_page=60, sleep=0.15):
    """翻页拉到底，返回 (records, pages, raw_pages)"""
    out, seen, page = [], set(), 0
    while page < max_page:
        page += 1
        params = {"page": page}
        if extra:
            params.update(extra)
        try:
            r = xhs2.raw_post(path, params)
        except Exception as e:
            print("    page %d ERR %r" % (page, repr(e)[:100]))
            break
        if r.get("result") != 1:
            print("    page %d -> result=%s msg=%r" % (page, r.get("result"), r.get("message")))
            break
        d = r.get("data") or {}
        lst = d.get("list") or []
        got = 0
        for o in lst:
            if not isinstance(o, dict) or not o.get("note_id"):
                continue
            nid = o["note_id"]
            if nid in seen:
                continue
            seen.add(nid)
            out.append(pick(o))
            got += 1
        print("    page %-2d 返回 %d 条，新增 %d，累计 %d" % (page, len(lst), got, len(out)))
        if not lst or got == 0:
            break
        time.sleep(sleep)
    return out, page


def main():
    cred = DEF_CRED
    who_only = False
    if "--who" in sys.argv:
        who_only = True
    if "--cred" in sys.argv:
        cred = sys.argv[sys.argv.index("--cred") + 1]

    old, tok, h = use_account(cred)
    print("账号 token = %s...  hash = %s" % (tok[:12], h))
    try:
        info = xhs2.raw_post("v2/mine/user-info", {})
        d = info.get("data") or {}
        vp = d.get("user_vp") or {}
        print("昵称 = %r   uid = %s" % (d.get("user_name"), d.get("user_id")))
        print("VIP  = vp_status=%s vp_end=%s svp_end=%s" % (
            vp.get("vp_status"), vp.get("vp_end"), vp.get("svp_end")))
        w = d.get("user_wat") or {}
        print("钱包 = book_cin=%s gold_cin=%s" % (w.get("book_cin"), w.get("gold_cin")))
        if who_only:
            return

        res = {}
        for name, path in PATHS.items():
            print("\n== %s (%s)" % (name, path))
            items, pages = walk(path)
            res[name] = items
            print("   -> %d 条 / %d 页" % (len(items), pages))

        print("\n== 收藏专辑")
        r = xhs2.raw_post(ALBUM, {"page": 1})
        albums = ((r.get("data") or {}).get("list") or [])
        for a in albums:
            print("   专辑 %r id=%s note_count=%s" % (
                a.get("name"), a.get("note_album_id"), a.get("note_count")))
        album_items = []
        for a in albums:
            aid = a.get("note_album_id")
            if not aid or (a.get("note_count") or 0) == 0:
                continue
            got, _ = walk(ALBUM_NOTES, {"note_album_id": aid})
            album_items.extend(got)
        res["album_notes"] = album_items

        allmap = {}
        for k in ("unlock", "like", "collect", "album_notes"):
            for it in res.get(k, []):
                if it["id"] not in allmap:
                    it = dict(it)
                    it["src"] = []
                    allmap[it["id"]] = it
                allmap[it["id"]]["src"].append(k)

        out = {
            "saved_at": int(time.time()),
            "account": {"hash": h, "nick": d.get("user_name"), "uid": d.get("user_id")},
            "unlock": res.get("unlock", []),
            "like": res.get("like", []),
            "collect": res.get("collect", []),
            "album_notes": res.get("album_notes", []),
            "all": list(allmap.values()),
        }
        json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

        print("\n===== 汇总 =====")
        for k in ("unlock", "like", "collect", "album_notes", "all"):
            print("  %-12s %d 条" % (k, len(out[k])))
        group = [x for x in out["all"] if x["gid"]]
        print("  其中粉丝团 %d 条，普通 %d 条" % (len(group), len(out["all"]) - len(group)))
        print("  -> %s" % OUT)
    finally:
        restore(old)


if __name__ == "__main__":
    main()
