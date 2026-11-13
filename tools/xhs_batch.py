# -*- coding: utf-8 -*-
"""按 series 批量下载：给定 note_id → 序号列表，自动排序 + 改名 + 转 mp4

用法：
  python xhs_batch.py                 # 跑内置的「替父还债·本以为是表演节目」九部
  python xhs_batch.py 250798 191636   # 手动给 id
"""
import os, re, sys, subprocess, time

from xhs2 import ensure_account, raw_post
import xhs_dl

FFMPEG = os.environ.get("FFMPEG") or "ffmpeg"
OUT = "downloads"

CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"

# 目标系列：请叫我雷锋(162361) 的【替父还债】本以为是表演节目、手段残忍
SERIES_IDS = [250798, 191636, 250800, 250801, 250802, 250803, 191638, 250804, 220123]
SERIES_NAME = "请叫我雷锋-替父还债·本以为是表演节目"


def order_of(title):
    """从标题里抠圈码序号，抠不到返回 99"""
    for ch in title or "":
        if ch in CIRCLED:
            return CIRCLED.index(ch) + 1
    m = re.match(r"\s*(\d+)", title or "")
    return int(m.group(1)) if m else 99


def safe(s):
    return re.sub(r'[\\/:*?"<>|\r\n]', " ", s).strip()[:80]


def to_mp4(ts_path):
    mp4 = os.path.splitext(ts_path)[0] + ".mp4"
    if os.path.exists(mp4):
        return mp4
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", ts_path,
                    "-c", "copy", mp4], check=False)
    return mp4 if os.path.exists(mp4) else None


def main():
    ids = [int(x) for x in sys.argv[1:] if x.isdigit()] or SERIES_IDS
    os.makedirs(OUT, exist_ok=True)

    # 1) 先全部查一遍，拿标题和 m3u8
    metas = []
    for nid in ids:
        r = raw_post("v2/note/view", {"note_id": int(nid)})
        if r.get("result") != 1:
            print(f"[跳过] {nid} result={r.get('result')} {r.get('message')}")
            continue
        d = r["data"]
        metas.append({
            "id": nid,
            "title": (d.get("note_title") or "").strip(),
            "media": d.get("note_media_url") or "",
            "cin": d.get("note_cin"),
            "is_by": d.get("is_by"),
            "dur": d.get("hd_duration") or 0,
        })
        time.sleep(0.1)

    # 2) 按圈码序号排
    metas.sort(key=lambda m: order_of(m["title"]))
    print(f"\n共 {len(metas)} 部，按序号排好：")
    for m in metas:
        print(f"  {order_of(m['title']):>2}. {m['id']}  cin={m['cin']}  {m['title']}")

    # 3) 逐个下载
    ok, fail = [], []
    for i, m in enumerate(metas, 1):
        seq = order_of(m["title"])
        seqs = f"{seq:02d}" if seq != 99 else f"{i:02d}"
        name = f"{seqs}_{safe(m['title'])}"
        print(f"\n[{i}/{len(metas)}] {name}")
        if not m["media"]:
            print("  没有 m3u8，跳过"); fail.append(m); continue
        try:
            # 注意：xhs_dl.download 内部自己会拼 downloads/ 前缀，这里只传裸名字
            ts = xhs_dl.download(m["media"], name, workers=8)
        except Exception as e:
            print(f"  下载失败 {type(e).__name__}: {str(e)[:100]}"); fail.append(m); continue
        if not ts:
            print("  没有产出 ts"); fail.append(m); continue
        mp4 = to_mp4(ts)
        size = os.path.getsize(mp4 or ts) / 1048576
        print(f"  OK {os.path.basename(mp4 or ts)}  {size:.2f} MB")
        ok.append((name, mp4))

    print(f"\n完成 {len(ok)}/{len(metas)}")
    for n, p in ok:
        print(f"  {p}")
    if fail:
        print("失败：")
        for m in fail:
            print(f"  {m['id']} {m['title']}")


if __name__ == "__main__":
    ensure_account()
    main()
