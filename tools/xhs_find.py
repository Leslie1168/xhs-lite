# -*- coding: utf-8 -*-
"""小黄书 搜索 → 挑选 → 下载  一条龙

用法：
  python xhs_find.py 白鹿                    # 只搜索，出列表
  python xhs_find.py 白鹿 -p 5               # 翻 5 页（每页 20 条）
  python xhs_find.py 白鹿 --pick 1,3,5       # 直接下这几条
  python xhs_find.py 白鹿 --pick 2-8         # 区间
  python xhs_find.py 白鹿 --pick all         # 全下
  python xhs_find.py -i                      # 交互：搜完等你敲序号
  python xhs_find.py 白鹿 -i                 # 带初始关键词的交互

选项：
  -p N        翻几页，默认 3
  -t          顺带把封面解密存到 covers/
  -v          只要视频（note_type=2）
  -g          只要图文（note_type=1）
  --ts        只留 ts，不转 mp4
  --cover     只下封面不下正文

黑名单（屏蔽作者，本地记住 + 可同步服务端）：
  python xhs_find.py --block 15583959:吃瓜爱好者    # 按 user_id 加
  python xhs_find.py --block 吃瓜爱好者            # 只写名字也认
  python xhs_find.py --unblock 吃瓜爱好者
  python xhs_find.py --blacklist                   # 看当前名单
  python xhs_find.py 白鹿 --block-pick 1,3         # 搜完直接屏蔽这几条的作者
  交互模式下：b 1,3 屏蔽 · u 名字 解除 · bl 看名单
  ** 搜索时自动跳过黑名单作者，列表里不再出现 **
"""
import os, sys, time, subprocess

from xhs2 import ensure_account, raw_post
import xhs_dl

FFMPEG = os.environ.get("FFMPEG") or "ffmpeg"
UA = ("Mozilla/5.0 (Linux; Android 10; SM-G975F) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/91.0.4472.114 Mobile Safari/537.36")
IMG_KEY = b"525202f9149e061d"
COVER_DIR = "covers"
BLACKLIST_FILE = "blacklist.json"
os.makedirs(COVER_DIR, exist_ok=True)
os.makedirs("downloads", exist_ok=True)

_sess = None
_black = None


def load_blacklist():
    """返回 {user_id(字符串): 名字}"""
    global _black
    if _black is None:
        _black = {}
        if os.path.exists(BLACKLIST_FILE):
            try:
                import json
                raw = json.load(open(BLACKLIST_FILE, encoding="utf-8"))
                for k, v in raw.items():
                    _black[str(k)] = v
            except Exception:
                pass
    return _black


def save_blacklist():
    import json
    json.dump(load_blacklist(), open(BLACKLIST_FILE, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)


def add_block(uid, name, server=False):
    """uid 可以是数字 user_id，也可以是 'name:作者名' 这种纯名字键"""
    key = str(uid)
    load_blacklist()[key] = name
    save_blacklist()
    kind = "作者" if key.startswith("name:") else "user_id"
    print(f"  已屏蔽 {name}（{kind}={key}），共 {len(_black)} 个")
    if server and not key.startswith("name:"):
        try:
            r = raw_post("v2/user/do-blacklist", {"user_id": int(key)})
            print(f"  服务端拉黑: result={r.get('result')} {r.get('message')!r}")
        except Exception as e:
            print(f"  服务端拉黑失败 {type(e).__name__}")


def server_block(uid):
    """把本地黑名单同步进服务端（v2/user/do-blacklist）"""
    try:
        r = raw_post("v2/user/do-blacklist", {"user_id": int(uid)})
        return r.get("result") == 1
    except Exception:
        return False


def del_block(key):
    b = load_blacklist()
    hit = [k for k, v in b.items() if k == str(key) or v == key]
    if not hit:
        print(f"  黑名单里没有 {key}")
        return
    for k in hit:
        print(f"  解除屏蔽 {b.pop(k)}（user_id={k}）")
    save_blacklist()


def is_blocked(it):
    b = load_blacklist()
    if not b:
        return False
    uid = str(it.get("user_id") or "")
    if uid and uid in b:
        return True
    return (it.get("user_name") or "") in b.values()


def http():
    global _sess
    if _sess is None:
        import requests, warnings
        warnings.filterwarnings("ignore")
        _sess = requests.Session()
        _sess.headers.update({"User-Agent": UA, "Referer": "https://play.solidedge.com.cn/"})
    return _sess


def dec_img(b):
    """codstatic 图片是 AES-ECB 密文，明文 JPEG 直接返回"""
    if b[:3] == b"\xff\xd8\xff":
        return b
    from Crypto.Cipher import AES
    if len(b) < 16:
        return b
    return AES.new(IMG_KEY, AES.MODE_ECB).decrypt(b[:len(b) // 16 * 16])


def human_dur(sec):
    try:
        sec = int(sec)
    except Exception:
        return "?"
    if sec <= 0:
        return "-"
    m, s = divmod(sec, 60)
    return f"{m}:{s:02d}" if m < 60 else f"{m // 60}:{m % 60:02d}"


def search(keyword, pages=3, only=None, skip_blocked=True):
    seen, rows = set(), []
    hidden = 0
    for pg in range(1, pages + 1):
        prm = {"q": keyword, "page": pg}
        if only:
            prm["note_type"] = only
        try:
            r = raw_post("v2/search/note-list", prm)
        except Exception as e:
            print(f"  第 {pg} 页失败: {type(e).__name__}"); break
        if r.get("result") != 1:
            print(f"  第 {pg} 页 result={r.get('result')} {r.get('message')!r}"); break
        d = r.get("data")
        items = d if isinstance(d, list) else (d or {}).get("list") or []
        if not items:
            break
        new = 0
        for it in items:
            nid = it.get("note_id")
            if nid in seen:
                continue
            seen.add(nid)
            if skip_blocked and is_blocked(it):
                hidden += 1
                continue
            new += 1
            rows.append({
                "idx": len(rows) + 1, "note_id": nid,
                "title": (it.get("note_title") or "").replace("\n", " ").strip(),
                "cin": it.get("note_cin", 0), "dur": it.get("hd_duration", 0),
                "user": it.get("user_name") or "",
                "uid": it.get("user_id"),
                "thumb": it.get("note_thumbnail") or it.get("note_cover") or "",
                "type": it.get("note_type"),
            })
        print(f"  第 {pg} 页: {len(items)} 条, 新增 {new}" + (f", 屏蔽 {hidden}" if hidden else ""))
        if len(items) == 0:
            break
        time.sleep(0.2)
    return rows


def show(rows):
    if not rows:
        print("没搜到。换关键词。"); return
    nv = sum(1 for r in rows if r["type"] == 2)
    bl = load_blacklist()
    tag = f"，已屏蔽 {len(bl)} 个作者" if bl else ""
    print(f"\n共 {len(rows)} 条（视频 {nv} / 图文 {len(rows) - nv}{tag}）：\n")
    print(f"{'序':<5}{'note_id':<12}{'类型':<6}{'时长':<9}{'币':<6}{'作者(user_id)':<24}标题")
    print("-" * 116)
    for r in rows:
        kind = "视频" if r["type"] == 2 else "图文"
        coin = "免费" if not r["cin"] else str(r["cin"])
        who = f"{r['user'][:10]}({r.get('uid')})"
        print(f"[{r['idx']}]".ljust(5) + str(r["note_id"]).ljust(12) + kind.ljust(6)
              + human_dur(r["dur"]).ljust(9) + coin.ljust(6)
              + who[:23].ljust(24) + r["title"][:40])


def grab_covers(rows):
    got = 0
    for r in rows:
        if not r["thumb"]:
            continue
        path = os.path.join(COVER_DIR, f"{r['idx']:03d}_{r['note_id']}.jpg")
        if os.path.exists(path):
            r["cover"] = path; got += 1; continue
        try:
            resp = http().get(r["thumb"], timeout=15, verify=False)
            b = dec_img(resp.content)
            if b[:3] == b"\xff\xd8\xff":
                open(path, "wb").write(b); r["cover"] = path; got += 1
        except Exception:
            pass
    print(f"\n封面 {got}/{len(rows)} 张 -> {os.path.abspath(COVER_DIR)}")


def to_mp4(ts):
    if not os.path.exists(FFMPEG):
        return None
    mp4 = ts[:-3] + ".mp4"
    if os.path.exists(mp4):
        return mp4
    r = subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", ts, "-c", "copy", mp4],
                       capture_output=True)
    return mp4 if r.returncode == 0 and os.path.exists(mp4) else None


def safe(name):
    for c in '\\/:*?"<>|':
        name = name.replace(c, "_")
    return name.strip()[:60]


def save_images(nid, title, images):
    d = os.path.join("downloads", f"{nid}_{safe(title)}")
    os.makedirs(d, exist_ok=True)
    n = 0
    for i, o in enumerate(images, 1):
        u = o.get("image_url") if isinstance(o, dict) else o
        if not u:
            continue
        fp = os.path.join(d, f"{i:02d}.jpg")
        if os.path.exists(fp):
            n += 1; continue
        for attempt in range(3):
            try:
                b = dec_img(http().get(u, timeout=30, verify=False).content)
                if b[:3] == b"\xff\xd8\xff":
                    open(fp, "wb").write(b); n += 1
                break
            except Exception:
                time.sleep(1 + attempt)
    total = sum(os.path.getsize(os.path.join(d, f)) for f in os.listdir(d))
    print(f"  图文: {n}/{len(images)} 张  {total/1048576:.2f} MB -> {os.path.abspath(d)}")
    return d


def run_download(rows, picks, do_mp4=True, cover_only=False):
    sel = [r for r in rows if r["idx"] in picks]
    if not sel:
        print("没选中。"); return
    print(f"\n下载 {len(sel)} 条\n" + "=" * 74)
    ok = 0
    for i, r in enumerate(sel, 1):
        print(f"\n[{i}/{len(sel)}] #{r['idx']} · {r['note_id']} · {r['title'][:42]}")
        try:
            det = raw_post("v2/note/view", {"note_id": int(r["note_id"])})
            d = det.get("data") or {}
            if "note_info" in d:
                d = d["note_info"]
            if det.get("result") != 1:
                print("  详情失败:", det.get("message")); continue
            title = d.get("note_title") or r["title"]
            flag = "" if not d.get("note_cin") else f" [付费 {d['note_cin']} 币]"
            print(f"  type={d.get('note_type')} is_by={d.get('is_by')}{flag}")

            if cover_only:
                u = d.get("note_thumbnail") or d.get("note_cover")
                if u:
                    b = dec_img(http().get(u, timeout=20, verify=False).content)
                    fp = os.path.join(COVER_DIR, f"{r['note_id']}_cover.jpg")
                    open(fp, "wb").write(b)
                    print(f"  封面 -> {fp}"); ok += 1
                continue

            if d.get("note_type") == 1 or (d.get("note_image_list") and not d.get("note_media_url")):
                imgs = d.get("note_image_list") or []
                if not imgs:
                    print("  没有图片"); continue
                save_images(r["note_id"], title, imgs); ok += 1; continue

            url = d.get("note_media_url")
            if not url:
                print("  没有媒体地址，跳过"); continue
            ts = xhs_dl.download(url, f"{r['note_id']}_{safe(title)}")
            if do_mp4 and ts:
                m = to_mp4(ts)
                if m:
                    print(f"  MP4: {m}  {os.path.getsize(m)/1048576:.2f} MB")
            ok += 1
        except Exception as e:
            print(f"  失败: {type(e).__name__} {str(e)[:90]}")
    print(f"\n完成 {ok}/{len(sel)}")


def parse_picks(s, rows):
    s = s.strip().lower()
    if s in ("all", "*", "全", "都"):
        return {r["idx"] for r in rows}
    out = set()
    for p in s.replace("，", ",").replace("、", ",").replace(" ", ",").split(","):
        if not p:
            continue
        if "-" in p:
            a, b = p.split("-", 1)
            if a.isdigit() and b.isdigit():
                out.update(range(int(a), int(b) + 1))
        elif p.isdigit():
            out.add(int(p))
    return out


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__); return
    pages, thumbs, do_mp4, only = 3, True, True, None
    interactive, pick, cover_only, kw = "-i" in args, None, False, []
    show_blocked, block_arg, unblock_arg, block_pick = False, None, None, None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-p" and i + 1 < len(args):
            pages = int(args[i + 1]); i += 2; continue
        if a == "--pick" and i + 1 < len(args):
            pick = args[i + 1]; i += 2; continue
        if a == "-t":
            thumbs = True; i += 1; continue
        if a == "--not-t":
            thumbs = False; i += 1; continue
        if a == "--ts":
            do_mp4 = False; i += 1; continue
        if a == "-v":
            only = 2; i += 1; continue
        if a == "-g":
            only = 1; i += 1; continue
        if a == "--cover":
            cover_only = True; i += 1; continue
        if a in ("-i", "--interactive"):
            i += 1; continue
        if a == "--block" and i + 1 < len(args):
            block_arg = args[i + 1]; i += 2; continue
        if a == "--unblock" and i + 1 < len(args):
            unblock_arg = args[i + 1]; i += 2; continue
        if a == "--block-pick" and i + 1 < len(args):
            block_pick = args[i + 1]; i += 2; continue
        if a == "--blacklist":
            show_blocked = True; i += 1; continue
        kw.append(a); i += 1

    # 纯黑名单操作，不需要登录
    if show_blocked:
        bl = load_blacklist()
        if not bl:
            print("黑名单是空的。")
        else:
            print(f"已屏蔽 {len(bl)} 个作者：")
            for k, v in bl.items():
                print(f"  {v}  user_id={k}")
        if not kw and not block_arg and not unblock_arg:
            return
    if unblock_arg:
        del_block(unblock_arg)
        if not kw:
            return
    if block_arg:
        for pair in block_arg.replace("，", ",").split(","):
            pair = pair.strip()
            if not pair:
                continue
            if ":" in pair or "=" in pair:
                uid, nm = pair.replace("=", ":").split(":", 1)
                add_block(uid, nm)
            elif pair.isdigit():
                add_block(pair, "手动屏蔽")
            else:
                # 只给了名字：用 name: 前缀做 key，is_blocked 靠 values 命中也拦得住
                add_block("name:" + pair, pair)

    keyword = " ".join(kw).strip()
    if not keyword:
        if block_arg or unblock_arg or show_blocked or block_pick:
            return
        keyword = input("搜索关键词 > ").strip()

    ensure_account()

    while True:
        print(f"\n>>> 搜「{keyword}」({pages} 页{', 仅视频' if only == 2 else ', 仅图文' if only == 1 else ''})")
        rows = search(keyword, pages, only)
        show(rows)
        if not rows:
            return
        if thumbs:
            grab_covers(rows)

        if block_pick:
            sel = parse_picks(block_pick, rows)
            for r in rows:
                if r["idx"] in sel and r.get("uid"):
                    add_block(r["uid"], r["user"])
            if not pick:
                return
        if pick:
            run_download(rows, parse_picks(pick, rows), do_mp4, cover_only)
            return
        if not interactive:
            print("\n（--pick 1,3,5 下载 / --block-pick 1,3 屏蔽作者 / -i 交互"
                  " / -v 只看视频 / -g 只看图文 / -p 5 多翻页）")
            return
        try:
            s = input("\n序号下载（1,3,5 / 2-6 / all）\n"
                      "b 1,3 屏蔽作者 · u 名字 解除 · bl 看黑名单\n"
                      "k 换词 · r 重搜 · q 退出 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print(); return
        low = s.lower()
        if low in ("q", "quit", "exit"):
            return
        if low in ("k", "kw"):
            keyword = input("新关键词 > ").strip(); continue
        if low in ("r", "re"):
            continue
        if low in ("bl", "blacklist"):
            bl = load_blacklist()
            if not bl:
                print("  黑名单空的。")
            for k2, v2 in bl.items():
                print(f"  {v2}  user_id={k2}")
            continue
        if low.startswith("b ") or low == "b":
            arg = s[1:].strip()
            if not arg:
                print("  要选序号，比如 b 1,3"); continue
            sel = parse_picks(arg, rows)
            for r in rows:
                if r["idx"] in sel and r.get("uid"):
                    add_block(r["uid"], r["user"])
            print("  重新搜索中…")
            continue
        if low.startswith("u ") or low == "u":
            arg = s[1:].strip()
            if not arg:
                print("  要写名字或 user_id，比如 u 吃瓜爱好者"); continue
            del_block(arg)
            continue
        sel = parse_picks(s, rows)
        if not sel:
            print("没看懂。"); continue
        run_download(rows, sel, do_mp4, cover_only)


if __name__ == "__main__":
    main()