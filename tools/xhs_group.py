import os
#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
xhs_group.py —— 粉丝团（fun-group）笔记单条购买 + 下载

核心发现（2026-11-13 实测）：
    粉丝团（group_id != 0）的笔记虽然只给 10 秒试看，
    但 v2/pucha/note-by 可以【单条购买】，价格就是该笔记的 note_cin，
    不需要买整团（整团 288 书币起，单条常常只要 2~18 书币）。
    买完 is_paid 变 1、is_by 变 1，v2/note/view 立刻返回完整版 m3u8。

用法：
    python xhs_group.py scan <user_id> [--pages N]
        扫该博主全部笔记，列出粉丝团笔记的价格表

    python xhs_group.py buy <note_id>
        单条购买（会真扣书币，先 scan 看清价格）

    python xhs_group.py grab <note_id> [-w 32]
        购买（若未买）+ 下载 + 转 mp4

    python xhs_group.py cheap <user_id> --under 5
        自动买下该博主所有 note_cin <= 5 的团笔记并下载

    python xhs_group.py fund
        查当前书币余额
"""
import sys, os, json, time, argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import xhs2
import xhs_dl
import xhs_batch

FFMPEG = os.environ.get("FFMPEG") or "ffmpeg"
CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"


# ---------- 基础 ----------

def fund():
    d = xhs2.raw_post("v2/mine/exchange-book-cin-page", {}).get("data") or {}
    return d.get("user_wat") or {}


def human_dur(sec):
    try:
        sec = int(sec)
    except Exception:
        return "?"
    return "%d:%02d" % (sec // 60, sec % 60)


def list_notes(user_id, pages=6):
    """拉博主主页全部笔记，返回原始 NoteListBean 条目"""
    out, seen = [], set()
    for page in range(1, pages + 1):
        r = xhs2.raw_post("v2/member/note-list",
                          {"note_type": 0, "user_id": int(user_id), "page": page})
        if r.get("result") != 1:
            break
        data = r.get("data") or {}
        notes = data.get("note_list") or data.get("list") or []
        if not notes:
            break
        add = 0
        for n in notes:
            nid = n.get("note_id")
            if nid in seen:
                continue
            seen.add(nid)
            out.append(n)
            add += 1
        if add == 0:
            break
        time.sleep(0.25)
    return out


def note_detail(nid):
    r = xhs2.raw_post("v2/note/view", {"note_id": int(nid)})
    return r.get("data") or {} if r.get("result") == 1 else {}


def buy(nid, retry=3):
    """单条购买。返回 (ok, message)"""
    last = ""
    for i in range(retry):
        r = xhs2.raw_post("v2/pucha/note-by", {"note_id": int(nid)})
        msg = r.get("message") or ""
        if r.get("result") == 1:
            return True, msg
        last = msg
        if "處理中" in msg or "处理中" in msg:
            time.sleep(20)
            continue
        return False, msg
    return False, last


# ---------- 命令 ----------

def cmd_scan(args):
    w = fund()
    print("当前余额: book_cin=%s  gold_cin=%s\n" % (w.get("book_cin"), w.get("gold_cin")))
    notes = list_notes(args.user_id, args.pages)
    print("博主 %s 共拉到 %d 条笔记" % (args.user_id, len(notes)))

    rows = []
    for n in notes:
        if not n.get("group_id"):
            continue
        d = note_detail(n.get("note_id"))
        rows.append({
            "note_id": n.get("note_id"),
            "cin": n.get("note_cin", d.get("note_cin")),
            "title": (n.get("note_title") or d.get("note_title") or "").strip(),
            "dur": d.get("hd_duration") or 0,
            "is_by": d.get("is_by", 0),
            "media": d.get("note_media_url") or "",
            "paid": "/static/shirt/" not in (d.get("note_media_url") or "")
                    and "/static/short/" not in (d.get("note_media_url") or ""),
        })

    if not rows:
        print("这个博主没有粉丝团专属笔记。")
        return

    rows.sort(key=lambda x: (x["cin"] or 0))
    print("\n粉丝团笔记 %d 条（按价格升序）\n" % len(rows))
    print("%-4s %-10s %-6s %-7s %-6s %s" % ("#", "note_id", "书币", "时长", "已购", "标题"))
    print("-" * 96)
    for i, r in enumerate(rows, 1):
        print("%-4d %-10s %-6s %-7s %-6s %s" % (
            i, r["note_id"], r["cin"], human_dur(r["dur"]),
            "是" if r["is_by"] else "否", r["title"][:44]))

    cheap = [r for r in rows if (r["cin"] or 0) <= (w.get("book_cin") or 0)]
    print("\n买得起的（cin <= %s）: %d 条" % (w.get("book_cin"), len(cheap)))
    if cheap:
        print("  例: python xhs_group.py grab %s" % cheap[0]["note_id"])


def cmd_buy(args):
    w = fund()
    print("余额: book_cin=%s" % w.get("book_cin"))
    ok, msg = buy(args.note_id)
    print(("购买成功: %s" if ok else "购买失败: %s") % msg)
    if ok:
        print("余额: book_cin=%s" % (fund().get("book_cin")))


def cmd_grab(args):
    d = note_detail(args.note_id)
    if not d:
        print("拿不到笔记信息")
        return
    title = (d.get("note_title") or "").strip()
    media = d.get("note_media_url") or ""
    print("note %s  cin=%s  is_by=%s  %s" % (args.note_id, d.get("note_cin"), d.get("is_by"), title))
    print("  media =", media)

    if "/static/shirt/" in media or "/static/short/" in media:
        print("\n>>> 还是试看版，先买单条（价格 %s 书币）" % d.get("note_cin"))
        ok, msg = buy(args.note_id)
        print("  购买:", "成功" if ok else "失败", "—", msg)
        if not ok:
            return
        d = note_detail(args.note_id)
        media = d.get("note_media_url") or ""
        print("  新 media =", media)
        if "/static/shirt/" in media or "/static/short/" in media:
            print("  买完仍然是试看版，这条可能只能整团解锁。")
            return

    name = "%s_%s" % (args.note_id, xhs_batch.safe(title))
    ts = xhs_dl.download(media, name, workers=args.workers)
    out = xhs_batch.to_mp4(ts)
    if out and os.path.exists(out):
        print("\n成品:", os.path.abspath(out),
              "%.2f MB" % (os.path.getsize(out) / 1048576.0))
    else:
        print("\n转封装失败，ts 保留:", os.path.abspath(ts))


DEFAULT_KWS = "情趣,换妻,抖音,反差,学生,熟女,偷拍,自慰,裸舞,酒店,日本,浴室,网红,少妇"


def cmd_market(args):
    """全站扫粉丝团笔记价格：多关键词 × 多页，按价格排序"""
    kws = [k.strip() for k in args.kws.split(",") if k.strip()]
    found = {}
    for kw in kws:
        for page in range(1, args.pages + 1):
            r = xhs2.raw_post("v2/search/note-list", {"q": kw, "page": page})
            if r.get("result") != 1:
                break
            lst = (r.get("data") or {}).get("list") or []
            for n in lst:
                if not n.get("group_id"):
                    continue
                nid = n.get("note_id")
                if nid in found:
                    continue
                found[nid] = {
                    "cin": n.get("note_cin") or 0,
                    "user": n.get("user_name") or "",
                    "uid": n.get("user_id"),
                    "title": (n.get("note_title") or "").strip(),
                }
            time.sleep(0.15)

    w = fund()
    have = w.get("book_cin") or 0
    print("余额 book_cin = %s   扫到粉丝团笔记 %d 条\n" % (have, len(found)))
    if not found:
        return

    rows = sorted(found.items(), key=lambda kv: kv[1]["cin"])
    print("%-11s %-5s %-12s %s" % ("note_id", "书币", "博主", "标题"))
    print("-" * 94)
    for nid, v in rows[:args.top]:
        print("%-11s %-5s %-12s %s" % (nid, v["cin"], v["user"][:11], v["title"][:46]))

    from collections import Counter
    dist = Counter(v["cin"] for v in found.values())
    print("\n价格分布:", dict(sorted(dist.items())))

    afford = [(nid, v) for nid, v in rows if v["cin"] <= have]
    print("买得起的（cin <= %s）: %d 条" % (have, len(afford)))
    for nid, v in afford[:8]:
        print("   python xhs_group.py grab %s   (%s 币) %s" % (nid, v["cin"], v["title"][:40]))


def cmd_cheap(args):
    w = fund()
    budget = w.get("book_cin") or 0
    print("余额 book_cin=%s，预算上限 %s\n" % (budget, args.under))
    notes = list_notes(args.user_id, args.pages)
    targets = []
    for n in notes:
        if not n.get("group_id"):
            continue
        d = note_detail(n.get("note_id"))
        if d.get("is_by"):
            continue
        cin = d.get("note_cin") or 0
        if cin <= args.under:
            targets.append((cin, n.get("note_id"), (d.get("note_title") or "").strip()))
    targets.sort()
    if not targets:
        print("没有符合的笔记。")
        return
    print("候选 %d 条:" % len(targets))
    for cin, nid, t in targets:
        print("  %s 书币  %s  %s" % (cin, nid, t[:50]))

    if not args.yes:
        print("\n加 --yes 才真买。")
        return

    for cin, nid, t in targets:
        if (fund().get("book_cin") or 0) < cin:
            print("\n余额不足，停止。")
            break
        print("\n>>> 买 %s (%s 书币) %s" % (nid, cin, t))
        ok, msg = buy(nid)
        print("   ", "成功" if ok else "失败", "—", msg)
        if not ok:
            continue
        args.note_id = nid
        args.workers = 32
        cmd_grab(args)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("scan"); p.add_argument("user_id"); p.add_argument("--pages", type=int, default=6)
    p = sub.add_parser("buy"); p.add_argument("note_id", type=int)
    p = sub.add_parser("grab"); p.add_argument("note_id", type=int); p.add_argument("-w", "--workers", type=int, default=32)
    p = sub.add_parser("cheap"); p.add_argument("user_id"); p.add_argument("--under", type=int, default=5)
    p.add_argument("--pages", type=int, default=6); p.add_argument("--yes", action="store_true")
    p = sub.add_parser("market"); p.add_argument("--kws", default=DEFAULT_KWS)
    p.add_argument("--pages", type=int, default=2); p.add_argument("--top", type=int, default=60)
    sub.add_parser("fund")

    args = ap.parse_args()
    if not args.cmd:
        ap.print_help()
        return
    xhs2.ensure_account()
    {"scan": cmd_scan, "buy": cmd_buy, "grab": cmd_grab, "market": cmd_market,
     "cheap": cmd_cheap, "fund": lambda a: print(fund())}[args.cmd](args)


if __name__ == "__main__":
    main()
