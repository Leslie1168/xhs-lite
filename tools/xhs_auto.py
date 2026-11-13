#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
xhs_auto.py — 粉丝团「低成本白嫖」流水线

粉丝团（group_id>0）是真硬锁：完整片不在公开 CDN，/static/shirt/ 只有 10 秒 t0.ts。
唯一不花钱的出口是书币，唯一不花钱的书币出口是签到（10 币/天，不能刷）。

本脚本做三件事：
  1) sign        签到，领当天 10 书币
  2) scan        全站扫粉丝团笔记，按价格排序（会记录到 group_pool.json）
  3) auto        签到 + 扫 + 用现有余额把最便宜的买下来 + 自动下载

用法:
    python xhs_auto.py sign
    python xhs_auto.py scan [--pages N]
    python xhs_auto.py auto [--max N] [--under CIN] [--dry]

价格事实（2026-11-13 全站 340 条抽样）:
    {18: 332, 2: 7, 28: 1}   ← 最低 2 币，没有 0 币的
    2 币档通常出现在「试看片很短」或「老笔记清仓」上，需要持续扫才碰得到。
"""
import sys, os, json, time, threading, queue, argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import xhs2
import xhs_fast
import xhs_batch
import xhs_group

POOL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "group_pool.json")
KWS = ["反差", "偷拍", "熟女", "抖音", "学生", "自慰", "裸舞", "酒店", "少妇", "浴室",
       "换妻", "人妻", "素人", "主播", "网红", "萝莉", "巨乳", "黑丝", "调教", "母狗",
       "福利", "私拍", "泄密", "偷情", "出轨", "按摩", "洗浴", "夜店", "KTV", "spa",
       "女同", "口交", "内射", "探花", "外围", "约炮", "制服", "旗袍", "丝袜", "师生"]


def fund():
    """当前书币"""
    r = xhs2.raw_post("v2/mine/wat-info", {})
    d = r.get("data") or {}
    w = d.get("user_wat") or d.get("wat_info") or d
    try:
        return int(w.get("book_cin") or 0)
    except Exception:
        return 0


def cmd_sign():
    r = xhs2.raw_post("v2/vod/sign-data", {})
    d = r.get("data") or {}
    print("签到 result=%s msg=%s" % (r.get("result"), (r.get("message") or "")[:40]))
    print("  本次得币: %s   累计签到: %s   连续: %s" % (d.get("coin"), d.get("count"), d.get("is_sign")))
    print("  当前书币: %s" % fund())


def _walk_collect(o, notes, authors, self_uid):
    if isinstance(o, dict):
        if o.get("group_id") and o.get("note_id"):
            notes[o["note_id"]] = {
                "note_id": o["note_id"],
                "cin": o.get("note_cin"),
                "gid": o.get("group_id"),
                "title": (o.get("note_title") or "")[:40],
                "uid": o.get("user_id"),
                "name": o.get("user_name"),
                "hd": o.get("hd_duration"),
            }
        if o.get("user_id") and o.get("user_id") != self_uid and (o.get("note_id") or o.get("note_title")):
            authors[o["user_id"]] = o.get("user_name")
        for v in o.values():
            _walk_collect(v, notes, authors, self_uid)
    elif isinstance(o, list):
        for v in o:
            _walk_collect(v, notes, authors, self_uid)


def cmd_scan(pages=2, quiet=False):
    """扫全站粉丝团笔记，返回 {note_id: info}"""
    me = xhs2.ensure_account()
    self_uid = getattr(xhs2, "USER_ID", None) or 69004456

    notes, authors = {}, {}
    lock = threading.Lock()

    # 阶段1：推荐流 + 关键词搜索，收集博主与团笔记
    feeds = []
    for pg in range(1, pages + 1):
        feeds.append(("v2/home/discover-note", {"page": pg}))
        feeds.append(("v2/home/vip-note", {"page": pg}))
        feeds.append(("v2/home/nearby-note", {"page": pg}))
    for path, prm in feeds:
        try:
            r = xhs2.raw_post(path, prm)
            _walk_collect(r.get("data"), notes, authors, self_uid)
        except Exception:
            pass
        time.sleep(0.2)

    for kw in KWS:
        for pg in range(1, pages + 1):
            try:
                r = xhs2.raw_post("v2/search/note-list",
                                  {"q": kw, "note_sort": 0, "note_type": 0, "category_id": 0, "page": pg})
                _walk_collect(r.get("data"), notes, authors, self_uid)
            except Exception:
                pass
            time.sleep(0.18)

    if not quiet:
        print("阶段1: 博主 %d 个, 直接命中团笔记 %d 条" % (len(authors), len(notes)))

    # 阶段2：批量拉每个博主的笔记列表
    q = queue.Queue()
    for uid, nm in authors.items():
        q.put((uid, nm))

    def worker():
        while True:
            try:
                uid, nm = q.get_nowait()
            except queue.Empty:
                return
            try:
                for pg in (1, 2):
                    r = xhs2.raw_post("v2/member/note-list",
                                      {"note_type": 0, "user_id": int(uid), "page": pg})
                    if r.get("result") != 1:
                        break
                    d = r.get("data") or {}
                    lst = d.get("note_list") or d.get("list") or []
                    if not lst:
                        break
                    for n in lst:
                        if (n.get("group_id") or 0) > 0:
                            with lock:
                                notes[n["note_id"]] = {
                                    "note_id": n["note_id"],
                                    "cin": n.get("note_cin"),
                                    "gid": n.get("group_id"),
                                    "title": (n.get("note_title") or "")[:40],
                                    "uid": uid,
                                    "name": nm,
                                    "hd": n.get("hd_duration"),
                                }
                    time.sleep(0.16)
            except Exception:
                pass

    ths = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in ths]
    [t.join() for t in ths]

    # 合并历史池
    if os.path.exists(POOL):
        try:
            old = json.load(open(POOL, encoding="utf-8"))
            old_notes = old.get("notes") or {}
            for k, v in old_notes.items():
                notes.setdefault(int(k), v)
        except Exception:
            pass

    json.dump({"notes": {str(k): v for k, v in notes.items()}, "ts": int(time.time())},
              open(POOL, "w", encoding="utf-8"), ensure_ascii=False)

    if not quiet:
        stat = {}
        for v in notes.values():
            stat[v["cin"]] = stat.get(v["cin"], 0) + 1
        print("阶段2: 团笔记 %d 条" % len(notes))
        print("价格分布:", json.dumps(_sorted_stat(stat), ensure_ascii=False))
    return notes


def _sorted_stat(stat):
    return dict(sorted(stat.items(), key=lambda x: (x[0] is None, x[0])))


def cmd_auto(max_buy=5, under=2, dry=False):
    cmd_sign()
    notes = cmd_scan(pages=2)
    cheap = sorted([v for v in notes.values() if v["cin"] is not None and v["cin"] <= under],
                   key=lambda x: x["cin"])
    print()
    print("=== 价格 <= %d 的团笔记: %d 条 ===" % (under, len(cheap)))
    for v in cheap[:20]:
        print("  %2s 币  %-14s %s" % (v["cin"], v["name"], v["title"]))

    bal = fund()
    print()
    print("当前书币: %d" % bal)
    if not cheap:
        print("没货，等下次扫描")
        return

    todo = []
    for v in cheap:
        if bal >= v["cin"] and len(todo) < max_buy:
            todo.append(v)
            bal -= v["cin"]
    if not todo:
        print("买不起，先签到攒币")
        return

    print("计划购买: %d 条，预计花费 %d 币 -> 剩余 %d" % (len(todo), sum(v["cin"] for v in todo), bal))
    if dry:
        print("(--dry 只打印不执行)")
        return

    got = []
    for v in todo:
        r = xhs2.raw_post("v2/pucha/note-by", {"note_id": int(v["note_id"])})
        okk = r.get("result") == 1
        print("  买 %s %s -> %s %s" % (v["note_id"], v["title"][:20], r.get("result"), (r.get("message") or "")[:20]))
        if okk:
            got.append(v)
        time.sleep(1.1)

    print()
    print("=== 下载 %d 条 ===" % len(got))
    for v in got:
        d = xhs2.raw_post("v2/note/view", {"note_id": int(v["note_id"])}).get("data") or {}
        media = d.get("note_media_url") or ""
        title = (d.get("note_title") or str(v["note_id"])).strip()
        if not any(x in media for x in ("/static/m3u8/", "/static/m4u8/")):
            print("  %s 仍非完整版，跳过 (%s)" % (v["note_id"], media[:60]))
            continue
        name = "%d_%s" % (v["note_id"], xhs_batch.safe(title))
        try:
            ts = xhs_fast.download(media, name, workers=32)
            mp4 = xhs_batch.to_mp4(ts)
            if mp4 and os.path.exists(mp4):
                print("  OK %.2f MB  %s" % (os.path.getsize(mp4) / 1048576.0, mp4))
                if os.path.exists(ts):
                    os.remove(ts)
        except Exception as e:
            print("  失败 %s: %s" % (v["note_id"], str(e)[:70]))

    print()
    print("剩余书币: %d" % fund())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["sign", "scan", "auto"])
    ap.add_argument("--pages", type=int, default=2)
    ap.add_argument("--max", type=int, default=5, dest="max_buy")
    ap.add_argument("--under", type=int, default=2)
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()

    if a.cmd == "sign":
        cmd_sign()
    elif a.cmd == "scan":
        cmd_scan(pages=a.pages)
    else:
        cmd_auto(max_buy=a.max_buy, under=a.under, dry=a.dry)


if __name__ == "__main__":
    main()
