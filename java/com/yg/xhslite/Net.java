package com.yg.xhslite;

import android.content.Context;
import android.content.SharedPreferences;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.security.SecureRandom;
import java.security.cert.X509Certificate;
import java.util.Iterator;
import java.util.Random;

import javax.crypto.Cipher;
import javax.crypto.spec.IvParameterSpec;
import javax.crypto.spec.SecretKeySpec;
import javax.net.ssl.HostnameVerifier;
import javax.net.ssl.HttpsURLConnection;
import javax.net.ssl.SSLContext;
import javax.net.ssl.SSLSession;
import javax.net.ssl.SSLSocketFactory;
import javax.net.ssl.TrustManager;
import javax.net.ssl.X509TrustManager;

/** 小黄书 v2.6.0 协议直连实现（纯客户端，无服务端中转） */
public class Net {

    static final String HOST = "app.zonghengnet.com";
    static final byte[] KEY = "525202f9149e061d".getBytes();
    static final byte[] IV = "985204f4819ec31c".getBytes();

    static volatile String token = "";
    static volatile String hash = "";
    static volatile String device = "";
    static volatile int uid = 0;
    static volatile String nick = "";
    /** 记住的账号密码（登录页预填 / 一键重登） */
    static volatile String accPhone = "";
    static volatile String accPass = "";
    /** 启动时 token 校验结果（false = 需要重新登录） */
    static volatile boolean ok = false;

    static SharedPreferences sp;
    static final Object LK = new Object();
    static SSLSocketFactory _ssf;

    // ---------- 初始化 ----------

    static void init(Context c) {
        sp = c.getSharedPreferences("xhs", Context.MODE_PRIVATE);
        token = sp.getString("token", "");
        hash = sp.getString("hash", "");
        device = sp.getString("device", "");
        uid = sp.getInt("uid", 0);
        nick = sp.getString("nick", "");
        accPhone = sp.getString("acc_phone", "");
        accPass = sp.getString("acc_pass", "");
    }

    static void save() {
        sp.edit()
                .putString("token", token)
                .putString("hash", hash)
                .putString("device", device)
                .putInt("uid", uid)
                .putString("nick", nick)
                .putString("acc_phone", accPhone)
                .putString("acc_pass", accPass)
                .apply();
    }

    static void reset() {
        token = "";
        hash = "";
        device = "";
        uid = 0;
        nick = "";
        save();
    }

    // ---------- 加密 ----------

    static byte[] enc(byte[] plain) throws Exception {
        SecretKeySpec ks = new SecretKeySpec(KEY, "AES");
        IvParameterSpec iv = new IvParameterSpec(IV);
        Cipher c = Cipher.getInstance("AES/CBC/PKCS5Padding");
        c.init(Cipher.ENCRYPT_MODE, ks, iv);
        return c.doFinal(plain);
    }

    static byte[] dec(byte[] data) throws Exception {
        SecretKeySpec ks = new SecretKeySpec(KEY, "AES");
        IvParameterSpec iv = new IvParameterSpec(IV);
        Cipher c = Cipher.getInstance("AES/CBC/PKCS5Padding");
        c.init(Cipher.DECRYPT_MODE, ks, iv);
        return c.doFinal(data);
    }

    /** 封面图：AES/ECB/NoPadding，同 KEY（对应官方 AESUtils.Bitmapdecrypt） */
    static byte[] ecbDec(byte[] data) throws Exception {
        SecretKeySpec ks = new SecretKeySpec(KEY, "AES");
        Cipher c = Cipher.getInstance("AES/ECB/NoPadding");
        c.init(Cipher.DECRYPT_MODE, ks);
        int n = data.length - (data.length % 16);
        return c.doFinal(data, 0, n);
    }

    // ---------- TLS（该站证书过期，必须跳过校验） ----------

    static SSLSocketFactory ssf() throws Exception {
        if (_ssf != null) return _ssf;
        TrustManager[] tm = new TrustManager[]{
                new X509TrustManager() {
                    public void checkClientTrusted(X509Certificate[] chain, String auth) {
                    }

                    public void checkServerTrusted(X509Certificate[] chain, String auth) {
                    }

                    public X509Certificate[] getAcceptedIssuers() {
                        return new X509Certificate[0];
                    }
                }
        };
        SSLContext ctx = SSLContext.getInstance("TLS");
        ctx.init(null, tm, new SecureRandom());
        _ssf = ctx.getSocketFactory();
        return _ssf;
    }

    static HostnameVerifier hv = new HostnameVerifier() {
        public boolean verify(String h, SSLSession s) {
            return true;
        }
    };

    static byte[] readAll(InputStream in) throws IOException {
        ByteArrayOutputStream bo = new ByteArrayOutputStream();
        byte[] buf = new byte[8192];
        int n;
        while ((n = in.read(buf)) > 0) bo.write(buf, 0, n);
        return bo.toByteArray();
    }

    // ---------- 请求 ----------

    static JSONObject post(String path, JSONObject params, String userId) throws Exception {
        synchronized (LK) {
            JSONObject body = new JSONObject();
            body.put("s_time", System.currentTimeMillis());
            body.put("user_token", token == null ? "" : token);
            if (params != null) {
                Iterator<String> it = params.keys();
                while (it.hasNext()) {
                    String k = it.next();
                    Object v = params.get(k);
                    if (v != null) body.put(k, v);
                }
            }
            byte[] enc = enc(body.toString().getBytes("UTF-8"));

            String who = userId;
            if (who == null || who.length() == 0) who = hash.length() > 0 ? hash : device;

            Exception last = null;
            for (int attempt = 0; attempt < 3; attempt++) {
                HttpURLConnection c = null;
                try {
                    URL u = new URL("https://" + HOST + "/" + path);
                    c = (HttpURLConnection) u.openConnection();
                    if (c instanceof HttpsURLConnection) {
                        HttpsURLConnection h = (HttpsURLConnection) c;
                        h.setSSLSocketFactory(ssf());
                        h.setHostnameVerifier(hv);
                    }
                    c.setConnectTimeout(20000);
                    c.setReadTimeout(28000);
                    c.setRequestMethod("POST");
                    c.setDoOutput(true);
                    c.setUseCaches(false);
                    c.setRequestProperty("Content-Type", "application/octet-stream; charset=utf-8");
                    c.setRequestProperty("Accept", "application/octet-stream");
                    c.setRequestProperty("Accept-Language", "zh-hk");
                    c.setRequestProperty("Client-Type", "1");
                    c.setRequestProperty("Client-Version", "2.6.0");
                    c.setRequestProperty("Client-Channel", "1333");
                    c.setRequestProperty("User-Agent", "okhttp/3.12.0");
                    c.setRequestProperty("User-Id", who);
                    c.setFixedLengthStreamingMode(enc.length);

                    OutputStream o = c.getOutputStream();
                    o.write(enc);
                    o.flush();
                    o.close();

                    int code = c.getResponseCode();
                    if (code != 200) throw new IOException("HTTP " + code);
                    InputStream in = c.getInputStream();
                    byte[] raw = readAll(in);
                    in.close();
                    String s = new String(dec(raw), "UTF-8");
                    return new JSONObject(s);
                } catch (Exception e) {
                    last = e;
                    try {
                        if (c != null) c.disconnect();
                    } catch (Exception ig) {
                    }
                    if (attempt < 2) {
                        try {
                            Thread.sleep(400L * (attempt + 1));
                        } catch (InterruptedException ig) {
                        }
                    }
                } finally {
                    try {
                        if (c != null) c.disconnect();
                    } catch (Exception ig) {
                    }
                }
            }
            throw last == null ? new IOException("post failed") : last;
        }
    }

    // ---------- 身份 ----------

    static String randHex(int n, Random r) {
        String H = "0123456789abcdef";
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < n; i++) sb.append(H.charAt(r.nextInt(16)));
        return sb.toString();
    }

    static String randDig(int n, Random r) {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < n; i++) sb.append((char) ('0' + r.nextInt(10)));
        return sb.toString();
    }

    static String newDevice() {
        Random r = new Random();
        int k = r.nextInt(4);
        if (k == 0) return randHex(12, r) + "889X";
        if (k == 1) return randDig(15, r) + "X";
        if (k == 2) return randHex(16, r) + "I";
        return randHex(30, r) + "AI";
    }

    /** 拉一次用户信息，填 uid / nick */
    static void pullMe() {
        try {
            JSONObject r3 = post("v2/mine/user-info", new JSONObject(), null);
            JSONObject data = r3.optJSONObject("data");
            if (data != null) {
                JSONObject ui = data.optJSONObject("user_info");
                if (ui != null) uid = ui.optInt("user_id", 0);
                JSONObject uv = data.optJSONObject("user_vp");
                nick = "";
                if (uv != null) nick = uv.optString("user_name", "");
                if (nick.length() == 0 && ui != null) nick = ui.optString("user_name", "");
            }
            save();
        } catch (Exception ig) {
        }
    }

    /**
     * 账号密码登录。返回 {"ok":1,"name":..,"uid":..} 或 {"ok":0,"msg":".."}
     * 成功后把手机号/密码记进 SP，登录页可预填、可一键重登。
     */
    static JSONObject login(String phone, String pass) {
        JSONObject out = new JSONObject();
        try {
            String ph = phone == null ? "" : phone.trim();
            String pw = pass == null ? "" : pass;
            if (ph.length() == 0) { out.put("ok", 0); out.put("msg", "请输入手机号"); return out; }
            if (pw.length() == 0) { out.put("ok", 0); out.put("msg", "请输入密码"); return out; }
            if (device == null || device.length() == 0) device = newDevice();
            try { post("v2/app/init", new JSONObject(), device); } catch (Exception ig) { }

            JSONObject p = new JSONObject();
            p.put("phone_area_code", "86");
            p.put("user_phone", ph);
            p.put("user_password", pw);
            JSONObject r = post("v2/user/login-with-password", p, device);
            if (r.optInt("result") != 1) {
                out.put("ok", 0);
                out.put("msg", r.optString("message", "登录失败"));
                return out;
            }
            JSONObject d = r.optJSONObject("data");
            if (d == null) { out.put("ok", 0); out.put("msg", "返回数据异常"); return out; }
            String tk = d.optString("user_token", "");
            if (tk.length() == 0) { out.put("ok", 0); out.put("msg", "没拿到登录凭证"); return out; }

            token = tk;
            hash = d.optString("user_hash", "");
            accPhone = ph;
            accPass = pw;
            save();
            pullMe();
            out.put("ok", 1);
            out.put("name", nick);
            out.put("uid", uid);
            return out;
        } catch (Exception e) {
            try { out.put("ok", 0); out.put("msg", "网络异常：" + e.getMessage()); } catch (Exception ig) { }
            return out;
        }
    }

    /** 退出登录（保留记住的手机号密码） */
    static void logout() {
        token = "";
        hash = "";
        uid = 0;
        nick = "";
        save();
    }

    /** 只校验已有 token，不再自动登录。false = 需要前端弹登录页 */
    static boolean ensure() {
        if (token == null || token.length() == 0) {
            ok = false;
            return false;
        }
        try {
            JSONObject r = post("v2/mine/user-info", new JSONObject(), null);
            if (r.optInt("result") == 1) {
                pullMe();
                ok = true;
                return true;
            }
        } catch (Exception e) {
        }
        ok = false;
        return false;
    }

    /** 已登录的账号（186 号），可选切换 */
    static void useAccount(String tk, String hs, int id) {
        token = tk;
        hash = hs;
        uid = id;
        save();
    }
}
