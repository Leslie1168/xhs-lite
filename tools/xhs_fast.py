# -*- coding: utf-8 -*-
"""小黄书 HLS 高速下载器

比 xhs_dl.py 快 4~6 倍，原因：
  1. 抛弃 requests —— 用裸 socket 手写 HTTP/1.1，省掉 urllib3 的流式解析开销
  2. keep-alive 长连接复用 —— 每片不再重新 TLS 握手（本地缓存节点握手 15ms，55 片就是 0.8s 纯等待）
  3. 多 IP 轮询分流 —— CDN 有 2 个 A 记录，各 32 条连接并行
  4. 并发可调，默认 32

用法:
   python xhs_fast.py <note_id>             # 下整条
   python xhs_fast.py -u <m3u8_url> <名字>   # 直接给地址
   python xhs_fast.py <note_id> -w 64        # 指定并发
"""
import os, re, sys, time, ssl, socket, threading, queue
from urllib.parse import urljoin
from Crypto.Cipher import AES

from xhs2 import ensure_account, raw_post

UA = ("Mozilla/5.0 (Linux; Android 10; SM-G975F) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/91.0.4472.114 Mobile Safari/537.36")
OUT = "downloads"
os.makedirs(OUT, exist_ok=True)

# 全站 CDN 有多台，内容互通（同一套存储，路径一致）。
# xn--z6uom.ienglish.store 解析到本地电信边缘缓存（合肥，握手 15ms）
# play.solidedge.com.cn   是日本大阪（握手 850ms，单片慢 15 倍）
# 统一改写到快域，是最有效的一招。
FAST_HOST = "xn--z6uom.ienglish.store"
REWRITE_HOSTS = ("play.solidedge.com.cn",)


def rewrite(url):
    for h in REWRITE_HOSTS:
        if h in url:
            return url.replace(h, FAST_HOST, 1)
    return url

_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE


# ---------------------------------------------------------------- 裸 HTTP
class Conn:
    """一条 keep-alive 的 TLS 连接"""

    def __init__(self, ip, host, timeout=25):
        self.ip, self.host = ip, host
        raw = socket.create_connection((ip, 443), timeout=timeout)
        self.s = _ctx.wrap_socket(raw, server_hostname=host)
        self.buf = b""

    def _read_until(self, sep):
        while sep not in self.buf:
            c = self.s.recv(262144)
            if not c:
                raise ConnectionError("closed")
            self.buf += c
        head, self.buf = self.buf.split(sep, 1)
        return head

    def _read_n(self, n):
        while len(self.buf) < n:
            c = self.s.recv(min(262144, n - len(self.buf)))
            if not c:
                raise ConnectionError("closed mid-body")
            self.buf += c
        d, self.buf = self.buf[:n], self.buf[n:]
        return d

    def get(self, path):
        req = (f"GET {path} HTTP/1.1\r\nHost: {self.host}\r\nUser-Agent: {UA}\r\n"
               f"Accept: */*\r\nAccept-Encoding: identity\r\nConnection: keep-alive\r\n\r\n")
        self.s.sendall(req.encode())
        head = self._read_until(b"\r\n\r\n").decode("latin1")
        status = head.split("\r\n", 1)[0]
        if " 200 " not in status:
            raise ConnectionError(f"HTTP {status.split()[1] if len(status.split())>1 else '?'}")
        hl = {k.lower(): v.strip() for k, v in
              (l.split(":", 1) for l in head.split("\r\n")[1:] if ":" in l)}
        if hl.get("transfer-encoding", "").lower() == "chunked":
            out = bytearray()
            while True:
                line = self._read_until(b"\r\n")
                size = int(line.split(b";")[0], 16)
                if size == 0:
                    self._read_until(b"\r\n")
                    break
                out += self._read_n(size)
                self._read_n(2)
            body = bytes(out)
        else:
            n = int(hl.get("content-length", "0"))
            body = self._read_n(n)
        # 服务器无视 Accept-Encoding: identity，m3u8 照样 gzip（ts 不压）
        if hl.get("content-encoding", "").lower() == "gzip" or body[:2] == b"\x1f\x8b":
            import gzip
            body = gzip.decompress(body)
        return body

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass


# ---------------------------------------------------------------- 工具
def plain_fetch(url, host, ips, tries=4):
    """单发请求（m3u8 / key 用）"""
    path = url.split(host, 1)[1] if host in url else url
    last = None
    for i in range(tries):
        ip = ips[i % len(ips)]
        try:
            c = Conn(ip, host)
            try:
                return c.get(path)
            finally:
                c.close()
        except Exception as e:
            last = e
            time.sleep(0.5 * (i + 1))
    raise RuntimeError(f"fetch {url} -> {last}")


def parse_playlist(txt, m3u8_url):
    keyuri = iv = None
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


def decrypt_seg(data, key, iv, seq=0):
    if not key:
        return data
    ivx = iv if iv else seq.to_bytes(16, "big")
    d = AES.new(key, AES.MODE_CBC, ivx).decrypt(data)
    p = d[-1]
    if 1 <= p <= 16 and d[-p:] == bytes([p]) * p:
        d = d[:-p]
    return d


# ---------------------------------------------------------------- 主下载
def download(m3u8_url, name, workers=32):
    m3u8_url = rewrite(m3u8_url)
    host = m3u8_url.split("/")[2]
    ips = list(dict.fromkeys([i[4][0] for i in
                              socket.getaddrinfo(host, 443, socket.AF_INET)]))
    print(f"  host={host}  IP={ips}")

    txt = plain_fetch(m3u8_url, host, ips).decode("utf-8", "replace")
    keyuri, iv, segs, base = parse_playlist(txt, m3u8_url)
    key = None
    m3u8_path = m3u8_url.split(host, 1)[1]
    if keyuri:
        ku = urljoin(m3u8_url, keyuri) if keyuri.startswith("/") else urljoin(base, keyuri)
        key = plain_fetch(ku, host, ips)
        if len(key) != 16:
            raise RuntimeError(f"密钥长度 {len(key)} != 16")
    print(f"  {len(segs)} 片, key={'有' if key else '无'}, "
          f"IV={iv.hex() if iv else '(按序号)'}, 并发 {workers}")

    paths = [u.split(host, 1)[1] if host in u else u for u in segs]
    out = [None] * len(segs)
    fail = []
    q = queue.Queue()
    for i in range(len(paths)):
        q.put(i)
    lock = threading.Lock()
    done = [0]
    t_start = time.time()

    def worker(wid):
        ip = ips[wid % len(ips)]
        conn = None
        while True:
            try:
                i = q.get_nowait()
            except queue.Empty:
                break
            ok = False
            for attempt in range(4):
                try:
                    if conn is None:
                        conn = Conn(ip, host)
                    raw = conn.get(paths[i])
                    out[i] = decrypt_seg(raw, key, iv, i)
                    ok = True
                    break
                except Exception:
                    if conn:
                        conn.close()
                    conn = None
                    ip = ips[(wid + attempt) % len(ips)]
                    time.sleep(0.3 * (attempt + 1))
            if not ok:
                with lock:
                    fail.append(i)
            with lock:
                done[0] += 1
                if done[0] % 20 == 0 or done[0] == len(segs):
                    el = time.time() - t_start
                    print(f"    {done[0]}/{len(segs)}  {el:.1f}s")
        if conn:
            conn.close()

    ths = [threading.Thread(target=worker, args=(w,), daemon=True)
           for w in range(min(workers, len(paths)))]
    for t in ths:
        t.start()
    for t in ths:
        t.join()

    el = time.time() - t_start
    got = [v for v in out if v]
    total = sum(len(v) for v in got)
    print(f"  下载 {len(got)}/{len(segs)} 片  {total/1048576:.2f} MB  "
          f"{el:.2f}s  {total/1048576/max(el,0.01):.2f} MB/s")
    if fail:
        print(f"  !! 缺片 {fail[:10]}")

    path = os.path.join(OUT, name + ".ts")
    with open(path, "wb") as f:
        for v in out:
            if v:
                f.write(v)
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        head = f.read(1)
    print(f"  写出 {path}  {size/1048576:.2f} MB  "
          f"首字节 0x{head[0]:02x} {'✓ MPEG-TS' if head[0] == 0x47 else '✗'}")
    return path


if __name__ == "__main__":
    args = [a for a in sys.argv[1:]]
    W = 32
    if "-w" in args:
        k = args.index("-w")
        W = int(args[k + 1])
        del args[k:k + 2]
    if args and args[0] == "-u":
        url = args[1]
        nm = args[2] if len(args) > 2 else "video"
        download(url, nm, W)
    else:
        nid = int(args[0])
        ensure_account()
        r = raw_post("v2/note/view", {"note_id": nid})
        if r.get("result") != 1:
            print("查详情失败:", r.get("message")); sys.exit(1)
        d = r.get("data") or {}
        url = d.get("note_media_url")
        title = re.sub(r'[\\/:*?"<>|\r\n]', " ", (d.get("note_title") or ""))[:40].strip()
        print(f"note {nid}  cin={d.get('note_cin')} is_by={d.get('is_by')}  {title}")
        if not url:
            print("没有媒体地址"); sys.exit(1)
        download(url, f"{nid}_{title}", W)
