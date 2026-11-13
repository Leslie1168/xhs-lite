package com.yg.xhslite;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.ContentUris;
import android.content.ContentValues;
import android.database.Cursor;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.provider.MediaStore;
import android.util.Log;
import android.view.KeyEvent;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.webkit.JavascriptInterface;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URLEncoder;
import java.net.URL;
import java.util.Iterator;
import java.util.Locale;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public class MainActivity extends Activity {

    volatile String BASE = "file:///android_asset/";
    LocalServer srv;

    WebView web;
    FrameLayout root;
    View fullView;
    WebChromeClient.CustomViewCallback fullCb;

    final ExecutorService pool = Executors.newFixedThreadPool(4);
    final ConcurrentHashMap<String, String> COVER = new ConcurrentHashMap<String, String>();
    File thumbDir, dlDir, dataDir;

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        getWindow().setFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON,
                WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);

        Net.init(this);

        dataDir = getFilesDir();
        thumbDir = new File(getCacheDir(), "thumb");
        dlDir = new File(getExternalFilesDir(null) != null ? getExternalFilesDir(null) : getFilesDir(), "dl");
        thumbDir.mkdirs();
        dlDir.mkdirs();

        root = new FrameLayout(this);
        root.setBackgroundColor(0xFF0A0A0C);

        web = new WebView(this);
        web.setBackgroundColor(0xFF0A0A0C);

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        s.setCacheMode(WebSettings.LOAD_DEFAULT);
        s.setUserAgentString(s.getUserAgentString() + " XhsLite/2.0");
        if (Build.VERSION.SDK_INT >= 21) {
            s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        }

        web.addJavascriptInterface(new Bridge(), "Native");
        android.webkit.WebSettings ws = web.getSettings();
        ws.setJavaScriptEnabled(true);
        ws.setDomStorageEnabled(true);
        ws.setAllowFileAccess(true);
        ws.setAllowContentAccess(true);
        ws.setAllowFileAccessFromFileURLs(true);
        ws.setAllowUniversalAccessFromFileURLs(true);
        ws.setMediaPlaybackRequiresUserGesture(false);
        ws.setMixedContentMode(android.webkit.WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);

        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onShowCustomView(View v, CustomViewCallback cb) {
                fullView = v;
                fullCb = cb;
                root.addView(v, new FrameLayout.LayoutParams(-1, -1));
                hideSys();
            }

            @Override
            public void onHideCustomView() {
                exitFull();
            }
        });

        web.setWebViewClient(new WebViewClient() {
            @Override
            public WebResourceResponse shouldInterceptRequest(WebView v, WebResourceRequest req) {
                return intercept(req.getUrl().toString());
            }

            @SuppressWarnings("deprecation")
            @Override
            public WebResourceResponse shouldInterceptRequest(WebView v, String u) {
                return intercept(u);
            }
        });

        root.addView(web, new FrameLayout.LayoutParams(-1, -1));
        setContentView(root);

        pool.execute(new Runnable() {
            public void run() {
                final boolean ok = Net.ensure();
                runOnUiThread(new Runnable() {
                    public void run() {
                        loadPage(ok);
                    }
                });
            }
        });
    }

    void loadPage(boolean logged) {
        // 未登录不再当错误：页面自己弹登录层
        BOOT_ERR = null;
        dbg("loadPage logged=" + logged + " BASE=" + BASE);
        try {
            InputStream in = getAssets().open("index.html");
            ByteArrayOutputStream bo = new ByteArrayOutputStream();
            byte[] buf = new byte[16384];
            int n;
            while ((n = in.read(buf)) > 0) bo.write(buf, 0, n);
            in.close();
            String html = new String(bo.toByteArray(), "UTF-8");
            String err = BOOT_ERR == null ? "" : BOOT_ERR;
            if (err.length() > 0) {
                html = html.replace("__BOOT_ERR__", err);
                web.loadDataWithBaseURL(BASE, html, "text/html", "utf-8", null);
                return;
            }
            // 正常路径：从 assets 直接 loadUrl（file:// 页面），这样主文档能正常渲染，
            // 同时 shouldInterceptRequest 对 https://app.local/t/<id> 才会触发。
            web.loadUrl(BASE + "index.html");
        } catch (Exception e) {
            web.loadDataWithBaseURL(BASE,
                    "<body style='background:#0a0a0c;color:#f2f2f5;font:16px sans-serif;padding:40px'>" +
                            "资源损坏：" + e.getMessage() + "</body>", "text/html", "utf-8", null);
        }
    }

    // ---------- 本地资源 / 缩略图 ----------

    static volatile String BOOT_ERR = null;

    /** 华为对非 debuggable 应用吞 Log，只能往外部存储写 */
    void dbg(String s) {
        try {
            File f = new File(getExternalFilesDir(null), "dbg.txt");
            FileOutputStream fo = new FileOutputStream(f, true);
            fo.write((System.currentTimeMillis() + " " + s + "\n").getBytes("UTF-8"));
            fo.close();
        } catch (Exception ig) {
        }
    }

    static String mimeOf(String n) {
        String l = n.toLowerCase(Locale.US);
        if (l.endsWith(".html") || l.endsWith(".htm")) return "text/html; charset=utf-8";
        if (l.endsWith(".js")) return "application/javascript; charset=utf-8";
        if (l.endsWith(".css")) return "text/css; charset=utf-8";
        if (l.endsWith(".png")) return "image/png";
        if (l.endsWith(".jpg") || l.endsWith(".jpeg")) return "image/jpeg";
        if (l.endsWith(".webp")) return "image/webp";
        if (l.endsWith(".mp4")) return "video/mp4";
        if (l.endsWith(".ts")) return "video/mp2t";
        if (l.endsWith(".m3u8")) return "application/vnd.apple.mpegurl";
        if (l.endsWith(".json")) return "application/json; charset=utf-8";
        return "application/octet-stream";
    }

    static byte[] bytesOf(InputStream in) {
        try {
            ByteArrayOutputStream bo = new ByteArrayOutputStream();
            byte[] buf = new byte[16384];
            int n;
            while ((n = in.read(buf)) > 0) bo.write(buf, 0, n);
            in.close();
            return bo.toByteArray();
        } catch (Exception e) {
            return new byte[0];
        }
    }

    static final String ALBUM = "黄书Lite";
    static final String RELATIVE = "Movies/" + ALBUM;

    /** 把 mp4 塞进系统相册 Movies/黄书Lite，图库和文件管理都能直接看到 */
    boolean saveToGallery(File src, String displayName) {
        try {
            ContentValues v = new ContentValues();
            v.put(MediaStore.Video.Media.DISPLAY_NAME, displayName);
            v.put(MediaStore.Video.Media.MIME_TYPE, "video/mp4");
            if (Build.VERSION.SDK_INT >= 29) {
                v.put(MediaStore.Video.Media.RELATIVE_PATH, RELATIVE);
                v.put(MediaStore.Video.Media.IS_PENDING, 1);
            }
            Uri uri = getContentResolver().insert(MediaStore.Video.Media.EXTERNAL_CONTENT_URI, v);
            if (uri == null) { dbg("gallery insert null"); return false; }
            OutputStream os = getContentResolver().openOutputStream(uri);
            FileInputStream in = new FileInputStream(src);
            byte[] buf = new byte[1 << 16];
            int n;
            while ((n = in.read(buf)) > 0) os.write(buf, 0, n);
            in.close();
            os.close();
            if (Build.VERSION.SDK_INT >= 29) {
                ContentValues v2 = new ContentValues();
                v2.put(MediaStore.Video.Media.IS_PENDING, 0);
                getContentResolver().update(uri, v2, null, null);
            }
            dbg("gallery ok " + displayName);
            return true;
        } catch (Exception e) {
            dbg("gallery EX " + e);
            return false;
        }
    }

    /** 按文件名找相册里的那条视频 */
    Uri findVideo(String name) {
        Cursor c = null;
        try {
            String sel;
            String[] args;
            if (Build.VERSION.SDK_INT >= 29) {
                sel = MediaStore.Video.Media.DISPLAY_NAME + "=? AND "
                        + MediaStore.Video.Media.RELATIVE_PATH + " LIKE ?";
                args = new String[]{name, RELATIVE + "%"};
            } else {
                sel = MediaStore.Video.Media.DISPLAY_NAME + "=? AND "
                        + MediaStore.Video.Media.DATA + " LIKE ?";
                args = new String[]{name, "%/" + RELATIVE + "/%"};
            }
            c = getContentResolver().query(MediaStore.Video.Media.EXTERNAL_CONTENT_URI,
                    new String[]{MediaStore.Video.Media._ID}, sel, args, null);
            if (c != null && c.moveToFirst()) {
                long id = c.getLong(0);
                return ContentUris.withAppendedId(MediaStore.Video.Media.EXTERNAL_CONTENT_URI, id);
            }
        } catch (Exception e) {
            dbg("findVideo EX " + e);
        } finally {
            if (c != null) c.close();
        }
        return null;
    }

    /** 本地服务路由：/ → index.html，/t/<id> → 缩略图，/dl/<name> → 下载的文件 */
    Object[] serve(String path) {
        Log.i("XHSLITE", "serve " + path);
        try {
            if (path.equals("/") || path.equals("/index.html")) {
                String html = new String(bytesOf(getAssets().open("index.html")), "UTF-8");
                String err = BOOT_ERR;
                html = html.replace("__BOOT_ERR__", err == null ? "" : err);
                return new Object[]{"text/html; charset=utf-8", html.getBytes("UTF-8")};
            }
            if (path.startsWith("/t/")) {
                byte[] b = thumbBytes(path.substring(3));
                return b == null ? null : new Object[]{"image/jpeg", b};
            }
            if (path.startsWith("/dl/")) {
                // 下载的文件进了系统相册（Movies/黄书Lite），优先从 MediaStore 取
                String nm = java.net.URLDecoder.decode(path.substring(4), "UTF-8");
                Uri u = findVideo(nm);
                if (u != null) {
                    InputStream in = getContentResolver().openInputStream(u);
                    return new Object[]{"video/mp4", bytesOf(in)};
                }
                File f = new File(dlDir, nm);
                String root = dlDir.getCanonicalPath();
                if (!f.exists() || !f.getCanonicalPath().startsWith(root)) return null;
                return new Object[]{mimeOf(f.getName()), f};
            }
            String p = path.startsWith("/") ? path.substring(1) : path;
            if (p.isEmpty()) p = "index.html";
            InputStream in = getAssets().open(p);
            return new Object[]{mimeOf(p), bytesOf(in)};
        } catch (Exception e) {
            return null;
        }
    }

    WebResourceResponse local(String path) {
        try {
            Object[] r = serve("/" + path);
            if (r == null) return null;
            Object body = r[1];
            InputStream in = body instanceof File
                    ? new FileInputStream((File) body)
                    : new ByteArrayInputStream((byte[]) body);
            return new WebResourceResponse((String) r[0], null, in);
        } catch (Exception e) {
            return null;
        }
    }

    /** 统一拦截入口：CDN 封面要 AES 解密，其余走 assets / 下载目录 */
    WebResourceResponse intercept(String u) {
        if (u == null) return null;

        // 封面/缩略图：CDN 上的 codstatic 图是 AES-ECB 密文，浏览器直连显示不出来
        if (u.contains("codstatic/") || u.contains("/static/vod/")) {
            String fid = Integer.toHexString(u.hashCode());
            File cache = new File(thumbDir, "cdn_" + fid + ".jpg");
            byte[] b = null;
            try {
                if (cache.exists() && cache.length() > 512) {
                    b = readF(cache);
                } else {
                    byte[] raw = httpGet(u);
                    if (raw != null && raw.length > 64) {
                        if (raw[0] == (byte) 0xff && raw[1] == (byte) 0xd8) {
                            b = raw;                      // 已经是明文 JPEG
                        } else {
                            byte[] d = Net.ecbDec(raw);
                            if (d.length > 4 && d[0] == (byte) 0xff && d[1] == (byte) 0xd8) b = d;
                            else if (d.length > 4 && d[0] == (byte) 0x89 && d[1] == (byte) 0x50) b = d;
                        }
                        if (b != null) {
                            byte[] sm = shrink(b, 540, 82);
                            if (sm != null && sm.length > 512) b = sm;
                            FileOutputStream fo = new FileOutputStream(cache);
                            fo.write(b);
                            fo.close();
                        } else {
                            dbg("cdn FAIL " + u);
                        }
                    }
                }
            } catch (Exception e) {
                dbg("cdn EX " + e);
            }
            if (b == null) b = new byte[0];
            WebResourceResponse wr = new WebResourceResponse("image/jpeg", null,
                    new ByteArrayInputStream(b));
            java.util.HashMap<String, String> hd = new java.util.HashMap<String, String>();
            hd.put("Content-Length", String.valueOf(b.length));
            hd.put("Cache-Control", "max-age=86400");
            try {
                wr.setStatusCodeAndReasonPhrase(200, "OK");
            } catch (Exception ig) {
            }
            wr.setResponseHeaders(hd);
            return wr;
        }

        int k = u.indexOf("/t/");
        if (u.startsWith("https://app.local/") || u.startsWith("http://app.local/")) {
            if (k > 0) {
                String id = u.substring(k + 3);
                int qm = id.indexOf('?');
                if (qm > 0) id = id.substring(0, qm);
                byte[] b = thumbBytes(id);
                if (b == null) b = new byte[0];
                dbg("intercept t/" + id + " -> " + b.length);
                WebResourceResponse wr = new WebResourceResponse("image/jpeg", null,
                        new ByteArrayInputStream(b));
                java.util.HashMap<String, String> hd = new java.util.HashMap<String, String>();
                hd.put("Content-Length", String.valueOf(b.length));
                hd.put("Cache-Control", "max-age=86400");
                try {
                    wr.setStatusCodeAndReasonPhrase(200, "OK");
                } catch (Exception ig) {
                }
                wr.setResponseHeaders(hd);
                return wr;
            }
            return local(u.substring(u.indexOf("app.local/") + 10));
        }
        if (u.startsWith(BASE)) return local(u.substring(BASE.length()));
        return null;
    }

    WebResourceResponse file(File f) {
        try {
            if (!f.exists()) return null;
            String mime = f.getName().endsWith(".mp4") ? "video/mp4"
                    : f.getName().endsWith(".ts") ? "video/mp2t" : "application/octet-stream";
            return new WebResourceResponse(mime, null, new java.io.FileInputStream(f));
        } catch (Exception e) {
            return null;
        }
    }

    byte[] thumbBytes(String id) {
        File f = thumbFile(id);
        if (f == null) return null;
        try {
            return readF(f);
        } catch (Exception e) {
            return null;
        }
    }

    File thumbFile(String id) {
        final String fid = id.split("[?]")[0];
        File cache = new File(thumbDir, fid + ".jpg");
        if (cache.exists() && cache.length() > 512) return cache;
        String url = COVER.get(fid);
        if (url == null) {
            dbg("thumb MISS id=" + fid + " reg=" + COVER.size());
            return null;
        }
        try {
            byte[] raw = httpGet(url);
            if (raw == null || raw.length < 64) { dbg("thumb GETFAIL " + fid + " url=" + url); return null; }
            byte[] out = shrink(raw, 540, 82);
            if (out == null) out = raw;
            FileOutputStream fo = new FileOutputStream(cache);
            fo.write(out);
            fo.close();
            dbg("thumb OK " + fid + " " + out.length);
            return cache;
        } catch (Exception e) {
            dbg("thumb EX " + fid + " " + e);
            return null;
        }
    }

    static byte[] readF(File f) throws Exception {
        java.io.FileInputStream in = new java.io.FileInputStream(f);
        ByteArrayOutputStream bo = new ByteArrayOutputStream();
        byte[] b = new byte[8192];
        int n;
        while ((n = in.read(b)) > 0) bo.write(b, 0, n);
        in.close();
        return bo.toByteArray();
    }

    static byte[] httpGet(String u) {
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL(u).openConnection();
            if (c instanceof javax.net.ssl.HttpsURLConnection) {
                javax.net.ssl.HttpsURLConnection h = (javax.net.ssl.HttpsURLConnection) c;
                h.setSSLSocketFactory(Net.ssf());
                h.setHostnameVerifier(Net.hv);
            }
            c.setConnectTimeout(15000);
            c.setReadTimeout(20000);
            c.setRequestProperty("User-Agent", "okhttp/3.12.0");
            c.setRequestProperty("Referer", "https://play.solidedge.com.cn/");
            int rc = c.getResponseCode();
            if (rc != 200) {
                Log.w("XHSLITE", "httpGet " + rc + " " + u);
                return null;
            }
            InputStream in = c.getInputStream();
            ByteArrayOutputStream bo = new ByteArrayOutputStream();
            byte[] b = new byte[16384];
            int n;
            while ((n = in.read(b)) > 0) bo.write(b, 0, n);
            in.close();
            return bo.toByteArray();
        } catch (Exception e) {
            return null;
        } finally {
            try {
                if (c != null) c.disconnect();
            } catch (Exception ig) {
            }
        }
    }

    static byte[] shrink(byte[] raw, int w, int q) {
        try {
            BitmapFactory.Options o = new BitmapFactory.Options();
            o.inJustDecodeBounds = true;
            BitmapFactory.decodeByteArray(raw, 0, raw.length, o);
            if (o.outWidth <= 0) return null;
            int sc = 1;
            while (o.outWidth / (sc * 2) >= w) sc *= 2;
            BitmapFactory.Options o2 = new BitmapFactory.Options();
            o2.inSampleSize = sc;
            Bitmap bm = BitmapFactory.decodeByteArray(raw, 0, raw.length, o2);
            if (bm == null) return null;
            if (bm.getWidth() > w) {
                int h = Math.max(1, bm.getHeight() * w / bm.getWidth());
                Bitmap s = Bitmap.createScaledBitmap(bm, w, h, true);
                if (s != bm) bm.recycle();
                bm = s;
            }
            ByteArrayOutputStream bo = new ByteArrayOutputStream();
            bm.compress(Bitmap.CompressFormat.JPEG, q, bo);
            bm.recycle();
            return bo.toByteArray();
        } catch (Exception e) {
            return null;
        }
    }

    // ---------- JS 桥 ----------

    public class Bridge {

        @JavascriptInterface
        public void req(final int id, final String path, final String json) {
            pool.execute(new Runnable() {
                public void run() {
                    String out;
                    try {
                        JSONObject p = (json == null || json.trim().isEmpty())
                                ? new JSONObject() : new JSONObject(json);
                        JSONObject r = Net.post(path, p, null);
                        out = r.toString();
                    } catch (Exception e) {
                        String m = e.getClass().getSimpleName() + ": " + String.valueOf(e.getMessage());
                        out = "{\"__err\":" + JSONObject.quote(m) + "}";
                    }
                    final String fo = out;
                    runOnUiThread(new Runnable() {
                        public void run() {
                            web.evaluateJavascript("window.__cb(" + id + "," + JSONObject.quote(fo) + ")", null);
                        }
                    });
                }
            });
        }

        @JavascriptInterface
        public void reg(String id, String url) {
            if (id != null && url != null) COVER.put(id, url);
        }

        /** 缩略图桥：返回本地文件绝对路径，JS 用 file:// 读。
         *  base64 走 evaluateJavascript 会因字符串过长被截断，只能走文件。 */
        @JavascriptInterface
        public void thumb(final int cb, final String id) {
            pool.execute(new Runnable() {
                public void run() {
                    File f = null;
                    try {
                        f = thumbFile(id);
                    } catch (Exception ig) {
                    }
                    final String p = f == null ? "" : f.getAbsolutePath();
                    runOnUiThread(new Runnable() {
                        public void run() {
                            web.evaluateJavascript("window.__thumb(" + cb + "," + JSONObject.quote(p) + ")", null);
                        }
                    });
                }
            });
        }

        @JavascriptInterface
        public String me() {
            JSONObject o = new JSONObject();
            try {
                o.put("uid", Net.uid);
                o.put("hash", Net.hash);
                o.put("token", Net.token);
                o.put("name", Net.nick);
            } catch (Exception ig) {
            }
            return o.toString();
        }

        /** 登录页启动状态：是否已登录 + 记住的账号密码 */
        @JavascriptInterface
        public String state() {
            JSONObject o = new JSONObject();
            try {
                o.put("logged", (Net.token != null && Net.token.length() > 0) ? 1 : 0);
                o.put("valid", Net.ok ? 1 : 0);
                o.put("phone", Net.accPhone);
                o.put("pass", Net.accPass);
                o.put("name", Net.nick);
                o.put("uid", Net.uid);
            } catch (Exception ig) {
            }
            return o.toString();
        }

        /** 账号密码登录，返回 {"ok":1,...} 或 {"ok":0,"msg":".."} */
        @JavascriptInterface
        public String login(String phone, String pass) {
            return Net.login(phone, pass).toString();
        }

        /** 退出登录（保留记住的手机号密码，方便一键重登） */
        @JavascriptInterface
        public void logout() {
            Net.logout();
        }

        @JavascriptInterface
        public String histGet() {
            try {
                File f = new File(dataDir, "hist.json");
                if (!f.exists()) return "[]";
                return new String(readF(f), "UTF-8");
            } catch (Exception e) {
                return "[]";
            }
        }

        @JavascriptInterface
        public void histAdd(String json) {
            try {
                JSONArray old = new JSONArray(histGet());
                JSONArray out = new JSONArray();
                JSONObject add = new JSONObject(json);
                String id = add.optString("id");
                out.put(add);
                for (int i = 0; i < old.length() && out.length() < 400; i++) {
                    JSONObject o = old.getJSONObject(i);
                    if (!id.equals(o.optString("id"))) out.put(o);
                }
                FileOutputStream fo = new FileOutputStream(new File(dataDir, "hist.json"));
                fo.write(out.toString().getBytes("UTF-8"));
                fo.close();
            } catch (Exception ig) {
            }
        }

        @JavascriptInterface
        public void histClear() {
            try {
                File f = new File(dataDir, "hist.json");
                if (f.exists()) f.delete();
            } catch (Exception ig) {
            }
        }

        @JavascriptInterface
        public String dlGet() {
            JSONArray a = new JSONArray();
            Cursor c = null;
            try {
                String sel = null, order;
                String[] args = null;
                if (Build.VERSION.SDK_INT >= 29) {
                    sel = MediaStore.Video.Media.RELATIVE_PATH + " LIKE ?";
                    args = new String[]{RELATIVE + "%"};
                } else {
                    sel = MediaStore.Video.Media.DATA + " LIKE ?";
                    args = new String[]{"%/" + RELATIVE + "/%"};
                }
                order = MediaStore.Video.Media.DATE_ADDED + " DESC";
                c = getContentResolver().query(MediaStore.Video.Media.EXTERNAL_CONTENT_URI,
                        new String[]{MediaStore.Video.Media.DISPLAY_NAME,
                                MediaStore.Video.Media.SIZE,
                                MediaStore.Video.Media.DATE_ADDED},
                        sel, args, order);
                while (c != null && c.moveToNext()) {
                    String nm = c.getString(0);
                    if (nm == null) continue;
                    JSONObject o = new JSONObject();
                    o.put("name", nm);
                    o.put("size", c.getLong(1));
                    o.put("ts", c.getLong(2));
                    String base = nm;
                    int dot = base.lastIndexOf('.');
                    if (dot > 0) base = base.substring(0, dot);
                    int us = base.indexOf('_');
                    if (us > 0) {
                        o.put("id", base.substring(0, us));
                        o.put("title", base.substring(us + 1));
                    } else {
                        o.put("id", "");
                        o.put("title", base);
                    }
                    a.put(o);
                }
            } catch (Exception ig) {
            } finally {
                if (c != null) c.close();
            }
            return a.toString();
        }

        @JavascriptInterface
        public boolean dlDel(String name) {
            try {
                String nm = new File(name).getName();
                Uri u = findVideo(nm);
                if (u != null) {
                    int n = getContentResolver().delete(u, null, null);
                    if (n > 0) return true;
                }
                File f = new File(dlDir, nm);
                return f.exists() && f.delete();
            } catch (Exception e) {
                return false;
            }
        }

        @JavascriptInterface
        public String dlPath(String name) {
            return "https://app.local/dl/" + URLEncoder.encode(name).replace("+", "%20");
        }

        @JavascriptInterface
        public void dlStart(final String id, final String title, final String m3u8) {
            pool.execute(new Runnable() {
                public void run() {
                    String msg = doDownload(id, title, m3u8);
                    final String m = msg;
                    runOnUiThread(new Runnable() {
                        public void run() {
                            android.widget.Toast.makeText(MainActivity.this, m,
                                    android.widget.Toast.LENGTH_SHORT).show();
                        }
                    });
                }
            });
        }

        @JavascriptInterface
        public void toast(final String s) {
            runOnUiThread(new Runnable() {
                public void run() {
                    android.widget.Toast.makeText(MainActivity.this, s,
                            android.widget.Toast.LENGTH_SHORT).show();
                }
            });
        }
    }

    // ---------- 下载 ----------

    String doDownload(String id, String title, String m3u8) {
        dbg("dl start " + id + " media=" + m3u8);
        try {
            byte[] plRaw = httpGet(m3u8);
            if (plRaw == null) { dbg("dl playlist null"); return "取清单失败"; }
            String txt = new String(plRaw, "UTF-8");
            String base = m3u8.substring(0, m3u8.lastIndexOf('/') + 1);

            String keyUri = null, ivHex = null;
            java.util.List<String> segs = new java.util.ArrayList<String>();
            for (String line : txt.split("\n")) {
                line = line.trim();
                if (line.isEmpty()) continue;
                if (line.startsWith("#EXT-X-KEY")) {
                    int a = line.indexOf("URI=\"");
                    if (a > 0) {
                        int b = line.indexOf('"', a + 5);
                        if (b > a) keyUri = line.substring(a + 5, b);
                    }
                    int c = line.indexOf("IV=0x");
                    if (c >= 0) ivHex = line.substring(c + 5).replace(",", "").trim();
                } else if (!line.startsWith("#")) {
                    segs.add(line);
                }
            }
            if (segs.isEmpty()) return "清单里没有分片";
            if (segs.size() > 3) {
                runOnUiThread(new Runnable() {
                    public void run() {
                        android.widget.Toast.makeText(MainActivity.this, "开始下载…",
                                android.widget.Toast.LENGTH_SHORT).show();
                    }
                });
            }

            byte[] key = null;
            if (keyUri != null) {
                // /static/m3u8/enc.key 是站点绝对路径，必须拼到域名根。
                // 拼成 base + key 会得到 .../20210704/5IBj0Q/static/m3u8/enc.key → 404
                String ku;
                if (keyUri.startsWith("http")) {
                    ku = keyUri;
                } else if (keyUri.startsWith("/")) {
                    java.util.regex.Matcher mu = java.util.regex.Pattern
                            .compile("^(https?://[^/]+)").matcher(m3u8);
                    ku = mu.find() ? (mu.group(1) + keyUri) : (base + keyUri.substring(1));
                } else {
                    ku = base + keyUri;
                }
                key = httpGet(ku);
                dbg("key " + ku + " len=" + (key == null ? 0 : key.length));
                if (key == null || key.length < 16) return "密钥取不到：" + ku;
            }

            ByteArrayOutputStream all = new ByteArrayOutputStream();
            for (String s : segs) {
                String su = s.startsWith("http") ? s : base + s;
                byte[] raw = httpGet(su);
                if (raw == null) return "分片取不到：" + s;
                if (key != null) raw = aesSeg(raw, key, ivHex);
                if (raw == null) return "分片解密失败";
                all.write(raw);
            }
            byte[] ts = all.toByteArray();
            // MPEG-TS 每个包 188 字节、以 0x47 开头；对不上就是没解成，别落盘
            if (ts.length > 376 && ((ts[0] & 0xFF) != 0x47 || (ts[188] & 0xFF) != 0x47)) {
                dbg("ts sync fail head=" + (ts[0] & 0xFF) + " @188=" + (ts[188] & 0xFF));
                return "解密失败，分片不是 TS（首字节 " + (ts[0] & 0xFF) + "）";
            }

            String safe = title == null ? "" : title.replaceAll("[\\\\/:*?\"<>|\\r\\n]", " ").trim();
            if (safe.length() > 60) safe = safe.substring(0, 60);
            if (safe.isEmpty()) safe = "video";
            File tsFile = new File(dlDir, id + "_" + safe + ".ts");
            FileOutputStream fo = new FileOutputStream(tsFile);
            fo.write(ts);
            fo.close();

            File mp4 = new File(dlDir, id + "_" + safe + ".mp4");
            if (ts2mp4(tsFile, mp4)) {
                tsFile.delete();
                // 塞进相册，图库/文件管理直接能看到；成功就把私有目录那份删掉
                if (saveToGallery(mp4, mp4.getName())) {
                    long kb = mp4.length() / 1024;
                    mp4.delete();
                    return "已存到相册 · Movies/" + ALBUM + "（" + kb + " KB）";
                }
                return "下好了：" + mp4.getName() + " · " + (mp4.length() / 1048576) + " MB";
            }
            return "下好了（ts 格式）：" + tsFile.getName();
        } catch (Exception e) {
            return "下载失败：" + e.getClass().getSimpleName();
        }
    }

    static byte[] aesSeg(byte[] data, byte[] key, String ivHex) {
        try {
            byte[] iv = new byte[16];
            if (ivHex != null && ivHex.length() >= 32) {
                for (int i = 0; i < 16; i++) {
                    iv[i] = (byte) Integer.parseInt(ivHex.substring(i * 2, i * 2 + 2), 16);
                }
            }
            javax.crypto.Cipher c = javax.crypto.Cipher.getInstance("AES/CBC/NoPadding");
            c.init(javax.crypto.Cipher.DECRYPT_MODE,
                    new javax.crypto.spec.SecretKeySpec(key, "AES"),
                    new javax.crypto.spec.IvParameterSpec(iv));
            byte[] out = c.doFinal(data);
            int n = out.length;
            if (n > 0) {
                int pad = out[n - 1] & 0xFF;
                if (pad >= 1 && pad <= 16 && n - pad >= 0) {
                    boolean ok = true;
                    for (int i = n - pad; i < n; i++) if ((out[i] & 0xFF) != pad) { ok = false; break; }
                    if (ok) {
                        byte[] cut = new byte[n - pad];
                        System.arraycopy(out, 0, cut, 0, cut.length);
                        return cut;
                    }
                }
            }
            return out;
        } catch (Exception e) {
            return null;
        }
    }

    static boolean ts2mp4(File in, File out) {
        android.media.MediaExtractor ex = new android.media.MediaExtractor();
        android.media.MediaMuxer mx = null;
        try {
            ex.setDataSource(in.getAbsolutePath());
            int n = ex.getTrackCount();
            if (n <= 0) return false;
            mx = new android.media.MediaMuxer(out.getAbsolutePath(),
                    android.media.MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4);
            int[] map = new int[n];
            for (int i = 0; i < n; i++) {
                android.media.MediaFormat f = ex.getTrackFormat(i);
                map[i] = (f != null) ? mx.addTrack(f) : -1;
            }
            mx.start();
            java.nio.ByteBuffer buf = java.nio.ByteBuffer.allocateDirect(2 * 1024 * 1024);
            android.media.MediaCodec.BufferInfo bi = new android.media.MediaCodec.BufferInfo();
            for (int i = 0; i < n; i++) {
                if (map[i] < 0) continue;
                ex.selectTrack(i);
                while (true) {
                    int sz = ex.readSampleData(buf, 0);
                    if (sz < 0) break;
                    bi.offset = 0;
                    bi.size = sz;
                    bi.presentationTimeUs = ex.getSampleTime();
                    bi.flags = ex.getSampleFlags();
                    mx.writeSampleData(map[i], buf, bi);
                    ex.advance();
                }
                ex.unselectTrack(i);
            }
            return out.length() > 1024;
        } catch (Exception e) {
            return false;
        } finally {
            try {
                if (mx != null) { mx.stop(); mx.release(); }
            } catch (Exception ig) {
            }
            try { ex.release(); } catch (Exception ig) { }
        }
    }

    // ---------- 全屏 / 返回 ----------

    void exitFull() {
        if (fullView != null) {
            root.removeView(fullView);
            fullView = null;
        }
        if (fullCb != null) {
            fullCb.onCustomViewHidden();
            fullCb = null;
        }
        root.getRootView().setSystemUiVisibility(View.SYSTEM_UI_FLAG_LAYOUT_STABLE);
    }

    void hideSys() {
        root.setSystemUiVisibility(
                View.SYSTEM_UI_FLAG_FULLSCREEN
                        | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                        | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                        | View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                        | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                        | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION);
    }

    private long lastBack = 0L;

    @Override
    public boolean onKeyDown(int code, KeyEvent e) {
        if (code == KeyEvent.KEYCODE_BACK) {
            if (fullView != null) {
                exitFull();
                return true;
            }
            if (web == null) return super.onKeyDown(code, e);
            final long now = System.currentTimeMillis();
            web.evaluateJavascript("(window.__back && window.__back()) ? 1 : 0",
                    new ValueCallback<String>() {
                        @Override
                        public void onReceiveValue(String v) {
                            if (v != null && v.contains("1")) return;   // 前端已回退一层
                            if (web.canGoBack()) { web.goBack(); return; }
                            if (now - lastBack < 2000) {
                                lastBack = 0L;
                                finish();
                            } else {
                                lastBack = now;
                                android.widget.Toast.makeText(MainActivity.this,
                                        "再按一次退出", android.widget.Toast.LENGTH_SHORT).show();
                            }
                        }
                    });
            return true;
        }
        return super.onKeyDown(code, e);
    }

    @Override
    protected void onPause() {
        super.onPause();
        if (web != null) web.onPause();
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (web != null) web.onResume();
    }

    @Override
    protected void onDestroy() {
        if (web != null) {
            web.loadUrl("about:blank");
            web.destroy();
            web = null;
        }
        pool.shutdownNow();
        super.onDestroy();
    }
}
