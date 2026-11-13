# -*- coding: utf-8 -*-
"""小黄书 v2.6.0 直连客户端
Host 走 app.zonghengnet.com（四域名里唯一没被 SNI 阻断的）
Content-Type / Accept = application/octet-stream  ← 这一条错了就全盘拒绝
"""
import ssl, socket, http.client, json, time, random, string, sys, os, threading

KEY = b"525202f9149e061d"
IV  = b"985204f4819ec31c"

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

CRED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "creds.json")


def enc(obj):
    return AES.new(KEY, AES.MODE_CBC, IV).encrypt(
        pad(json.dumps(obj, separators=(',', ':')).encode(), 16))


def dec(b):
    return unpad(AES.new(KEY, AES.MODE_CBC, IV).decrypt(b), 16).decode('utf-8')


TARGET_IP = "107.148.45.6"      # app.zonghengnet.com 的 A 记录
HOST = "app.zonghengnet.com"
USE_DIRECT_IP = False           # True=手动 DNS 直连；False=直接域名（verify 关掉即可通）

_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE

STATE = {"token": "", "hash": "", "device": "", "uid": 0}


def load():
    if os.path.exists(CRED):
        try:
            STATE.update(json.load(open(CRED, encoding="utf-8")))
        except Exception:
            pass


def save():
    json.dump(STATE, open(CRED, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


_G = {"c": None}
_G_LK = threading.RLock()


def _get_conn():
    """全局只留一个 HTTPS 长连接，跨线程复用。

    服务端本来就按账号串行处理，本地并发拿不到任何好处；而
    ThreadingHTTPServer 每个请求都是新线程，如果用 threading.local，
    每个请求都要重做一次 TLS 握手（1.4~1.8 秒）。所以改成全局单连接 + 锁。
    """
    c = _G.get("c")
    if c is not None:
        return c
    c = http.client.HTTPSConnection(HOST, 443, timeout=25, context=_ctx)
    _G["c"] = c
    return c


def _drop_conn():
    c = _G.pop("c", None)
    try:
        if c is not None:
            c.close()
    except Exception:
        pass


def raw_post(path, params, user_id=None, timeout=20, retries=3):
    body_obj = {"s_time": int(time.time() * 1000), "user_token": STATE["token"]}
    body_obj.update(params)
    body = enc(body_obj)
    uid = user_id if user_id is not None else (STATE["hash"] or STATE["device"])
    hdrs = {
        "Content-Type": "application/octet-stream; charset=utf-8",
        "Accept": "application/octet-stream",
        "User-Id": uid,
        "Client-Type": "1",
        "Client-Version": "2.6.0",
        "Client-Channel": "1333",
        "Accept-Language": "zh-hk",
        "Content-Length": str(len(body)),
        "Connection": "keep-alive",
    }
    last = None
    for i in range(retries):
        try:
            if USE_DIRECT_IP:
                raw = socket.create_connection((TARGET_IP, 443), timeout=timeout)
                s = _ctx.wrap_socket(raw, server_hostname=HOST)
                c = http.client.HTTPConnection(TARGET_IP, 443, timeout=timeout)
                c.sock = s
                try:
                    c.request("POST", "/" + path, body=body, headers=hdrs)
                    r = c.getresponse()
                    data = r.read()
                    if r.status != 200:
                        raise RuntimeError(f"HTTP {r.status}")
                    return json.loads(dec(data))
                finally:
                    try:
                        c.close()
                    except Exception:
                        pass
            with _G_LK:                    # HTTPSConnection 不是线程安全的
                c = _get_conn()
                c.request("POST", "/" + path, body=body, headers=hdrs)
                r = c.getresponse()
                data = r.read()
            if r.status != 200:
                raise RuntimeError(f"HTTP {r.status}")
            return json.loads(dec(data))
        except Exception as e:
            last = e
            _drop_conn()          # 长连接可能被服务端掐掉，重建
            if i < retries - 1:
                time.sleep(0.5 * (i + 1))
    raise last


def new_device():
    k = random.randrange(4)
    h = lambda n: ''.join(random.choice('0123456789abcdef') for _ in range(n))
    d = lambda n: ''.join(random.choice(string.digits) for _ in range(n))
    if k == 0:   return h(12) + "889X"
    if k == 1:   return d(15) + "X"
    if k == 2:   return h(16) + "I"
    return h(30) + "AI"      # 必须 30 位


def ensure_account(reuse=True):
    load()
    if reuse and STATE["token"]:
        try:
            r = raw_post("v2/mine/user-info", {})
            if r.get("result") == 1:
                print(f"[复用] user_id={STATE['uid']} hash={STATE['hash']} token={STATE['token'][:16]}...")
                return True
        except Exception as e:
            print("[复用失败]", type(e).__name__, str(e)[:50])

    STATE["device"] = new_device()
    STATE["token"] = ""; STATE["hash"] = ""
    print(f"[1] app/init   device={STATE['device']}")
    r1 = raw_post("v2/app/init", {}, user_id=STATE["device"])
    print(f"    result={r1.get('result')} message={r1.get('message')!r}")
    time.sleep(0.8)
    print("[2] login-with-guest")
    r2 = raw_post("v2/user/login-with-guest", {}, user_id=STATE["device"])
    d = r2.get("data") or {}
    STATE["token"] = d.get("user_token", "")
    STATE["hash"] = d.get("user_hash", "")
    print(f"    result={r2.get('result')} hash={STATE['hash']} token_len={len(STATE['token'])}")
    if r2.get("result") != 1:
        print("    !! 建号失败"); return False
    time.sleep(0.8)
    r3 = raw_post("v2/mine/user-info", {})
    ui = (r3.get("data") or {}).get("user_info") or {}
    STATE["uid"] = ui.get("user_id", 0)
    vp = (r3.get("data") or {}).get("user_vp") or {}
    print(f"[3] user-info  user_id={STATE['uid']} name={ui.get('user_name')} "
          f"vp_status={vp.get('vp_status')} vp_end={vp.get('vp_end')} svp_end={vp.get('svp_end')}")
    save()
    return True


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd in ("run", "login"):
        ensure_account()
    elif cmd == "reset":
        if os.path.exists(CRED):
            os.remove(CRED)
        STATE.update(token="", hash="", device="", uid=0)
        ensure_account(reuse=False)
    elif cmd == "raw":
        ensure_account()
        p = sys.argv[2]
        params = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
        print(json.dumps(raw_post(p, params), ensure_ascii=False, indent=2)[:6000])