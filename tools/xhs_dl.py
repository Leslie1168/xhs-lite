# -*- coding: utf-8 -*-
"""小黄书 HLS 下载器 — 直连 m3u8 + enc.key + AES-128-CBC 切片解密
用法:
   python xhs_dl.py <note_id>               # 自动查 note/view 拿地址并下全片
   python xhs_dl.py -u <m3u8_url> <输出前缀> # 直接给地址
产出:
   <前缀>.ts  解密合并后的 TS 流（可直接播放；ffmpeg -i x.ts -c copy x.mp4 转封装）
"""
import os, re, sys, time, threading
from urllib.parse import urljoin
import requests
from Crypto.Cipher import AES

from xhs2 import ensure_account, raw_post, STATE

UA = ("Mozilla/5.0 (Linux; Android 10; SM-G975F) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/91.0.4472.114 Mobile Safari/537.36")
S = requests.Session()
S.headers.update({"User-Agent": UA, "Referer": "https://play.solidedge.com.cn/"})
OUT = "downloads"
os.makedirs(OUT, exist_ok=True)


def fetch(url, tries=4, timeout=40):
    last = None
    for i in range(tries):
        try:
            r = S.get(url, timeout=timeout)
            if r.status_code == 200:
                return r.content
            last = f"HTTP {r.status_code}"
        except Exception as e:
            last = f"{type(e).__name__}"
        time.sleep(0.8 * (i + 1))
    raise RuntimeError(f"fetch failed {url} -> {last}")


def parse_playlist(m3u8_url):
    txt = fetch(m3u8_url).decode("utf-8", "replace")
    keyuri = None
    iv = None
    m = re.search(r'#EXT-X-KEY:.*?URI="([^"]+)"', txt)
    if m:
        keyuri = m.group(1)
    m = re.search(r'IV=0x([0-9a-fA-F]+)', txt)
    if m:
        iv = bytes.fromhex(m.group(1))
    base = m3u8_url.rsplit("/", 1)[0] + "/"
    segs = [urljoin(base, l.strip()) for l in txt.splitlines()
            if l.strip() and not l.startswith("#")]
    return keyuri, iv, segs, base


def get_key(keyuri, base, m3u8_url):
    if not keyuri:
        return None
    u = urljoin(m3u8_url, keyuri) if keyuri.startswith("/") else urljoin(base, keyuri)
    k = fetch(u)
    if len(k) != 16:
        raise RuntimeError(f"密钥长度 {len(k)} != 16")
    return k


def decrypt_seg(data, key, iv):
    if not key:
        return data
    d = AES.new(key, AES.MODE_CBC, iv).decrypt(data)
    p = d[-1]
    if 1 <= p <= 16 and d[-p:] == bytes([p]) * p:
        d = d[:-p]
    return d


def download(m3u8_url, name, workers=8):
    keyuri, iv, segs, base = parse_playlist(m3u8_url)
    key = get_key(keyuri, base, m3u8_url)
    print(f"  m3u8: {len(segs)} 片, key={'有' if key else '无'}, "
          f"IV={iv.hex() if iv else '(按序号)'}")
    out = [None] * len(segs)

    def work(i):
        for attempt in range(4):
            try:
                out[i] = decrypt_seg(fetch(segs[i]), key, iv)
                return
            except Exception as e:
                if attempt == 3:
                    print(f"    !! 片 {i} 失败 {e}")
                time.sleep(1 + attempt)

    done = [0]
    lock = threading.Lock()

    def run(i):
        work(i)
        with lock:
            done[0] += 1
            if done[0] % 10 == 0 or done[0] == len(segs):
                print(f"    {done[0]}/{len(segs)}")

    ths = []
    for i in range(len(segs)):
        t = threading.Thread(target=run, args=(i,))
        t.start()
        ths.append(t)
        while len([x for x in ths if x.is_alive()]) >= workers:
            time.sleep(0.05)
    for t in ths:
        t.join()

    miss = [i for i, v in enumerate(out) if v is None]
    if miss:
        print(f"  !! 缺 {len(miss)} 片: {miss[:10]}")
    path = os.path.join(OUT, name + ".ts")
    with open(path, "wb") as f:
        for v in out:
            if v:
                f.write(v)
    size = os.path.getsize(path)
    print(f"  写出 {path}  {size/1048576:.2f} MB")
    # 校验
    with open(path, "rb") as f:
        head = f.read(1)
    print(f"  首字节 0x{head[0]:02x} {'✓ MPEG-TS' if head[0] == 0x47 else '✗'}")
    return path


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "-u":
        url = sys.argv[2]
        name = sys.argv[3] if len(sys.argv) > 3 else "video"
        download(url, name)
    else:
        nid = int(sys.argv[1])          # 必须整数，字符串会「請求解碼錯誤」
        ensure_account()
        r = raw_post("v2/note/view", {"note_id": nid})
        if r.get("result") != 1:
            print("查详情失败:", r.get("message")); sys.exit(1)
        d = r.get("data") or {}
        if "note_info" in d:
            d = d["note_info"]
        url = d.get("note_media_url")
        title = (d.get("note_title") or "").replace("/", "_").replace("\\", "_")[:40]
        print(f"note {nid}  cin={d.get('note_cin')} is_by={d.get('is_by')}  {title}")
        print(f"media: {url}")
        if not url:
            print("没有媒体地址（图文帖或已删除）"); sys.exit(1)
        download(url, f"{nid}_{title}")