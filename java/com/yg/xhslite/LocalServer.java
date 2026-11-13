package com.yg.xhslite;

import java.io.BufferedInputStream;
import java.io.ByteArrayInputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.io.RandomAccessFile;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * 127.0.0.1 上的迷你 HTTP 服务。
 *
 * 为什么不用 shouldInterceptRequest：loadDataWithBaseURL 载入的页面，
 * 它里面的子资源请求不会被 WebView 交给 shouldInterceptRequest（真机实测
 * 一次都没触发，封面全黑，logcat 里是 net::ERR_NAME_NOT_RESOLVED）。
 * 起一个真的本地服务器，页面用 http://127.0.0.1:PORT/ 加载，一切正常，
 * 顺带支持 Range，视频可以拖动。
 */
public class LocalServer {

    public interface Handler {
        /** 返回 null 表示 404；否则返回 [mime, body] 或 [mime, File] */
        Object[] get(String path);
    }

    private final Handler h;
    private ServerSocket ss;
    private final ExecutorService pool = Executors.newFixedThreadPool(6);
    private volatile boolean running = true;

    public LocalServer(Handler h) {
        this.h = h;
    }

    public int start() {
        try {
            try {
                ss = new ServerSocket(8765, 32, InetAddress.getByName("127.0.0.1"));
            } catch (Exception busy) {
                ss = new ServerSocket(0, 32, InetAddress.getByName("127.0.0.1"));
            }
            final ServerSocket sock = ss;
            Thread t = new Thread(new Runnable() {
                public void run() {
                    while (running) {
                        try {
                            final Socket c = sock.accept();
                            pool.execute(new Runnable() {
                                public void run() {
                                    serve(c);
                                }
                            });
                        } catch (Exception e) {
                            if (!running) return;
                        }
                    }
                }
            }, "localsrv");
            t.setDaemon(true);
            t.start();
            return ss.getLocalPort();
        } catch (Exception e) {
            return 0;
        }
    }

    public void stop() {
        running = false;
        try {
            if (ss != null) ss.close();
        } catch (Exception ig) {
        }
    }

    private void serve(Socket c) {
        try {
            c.setSoTimeout(15000);
            BufferedInputStream in = new BufferedInputStream(c.getInputStream());
            String line = readLine(in);
            if (line == null) {
                c.close();
                return;
            }
            String[] parts = line.split(" ");
            String path = parts.length > 1 ? parts[1] : "/";
            int qm = path.indexOf('?');
            if (qm >= 0) path = path.substring(0, qm);

            String range = null;
            String l;
            while ((l = readLine(in)) != null && l.length() > 0) {
                String low = l.toLowerCase(Locale.US);
                if (low.startsWith("range:")) range = l.substring(6).trim();
            }

            Object[] res = h.get(path);
            OutputStream out = c.getOutputStream();
            if (res == null) {
                byte[] b = "not found".getBytes("UTF-8");
                head(out, 404, "text/plain", b.length, null);
                out.write(b);
                out.flush();
                c.close();
                return;
            }

            String mime = (String) res[0];
            Object body = res[1];

            if (body instanceof byte[]) {
                byte[] b = (byte[]) body;
                head(out, 200, mime, b.length, null);
                out.write(b);
            } else {
                File f = (File) body;
                long len = f.length();
                if (range != null && range.startsWith("bytes=")) {
                    String r = range.substring(6);
                    int dash = r.indexOf('-');
                    long s = 0, e = len - 1;
                    try {
                        if (dash > 0) s = Long.parseLong(r.substring(0, dash).trim());
                        if (dash >= 0 && dash + 1 < r.length()) {
                            String es = r.substring(dash + 1).trim();
                            if (es.length() > 0) e = Long.parseLong(es);
                        }
                    } catch (Exception ig) {
                    }
                    if (s < 0) s = 0;
                    if (e >= len) e = len - 1;
                    if (e < s) {
                        head(out, 416, mime, 0, "bytes */" + len);
                        out.flush();
                        c.close();
                        return;
                    }
                    long n = e - s + 1;
                    head(out, 206, mime, n, "bytes " + s + "-" + e + "/" + len);
                    RandomAccessFile raf = new RandomAccessFile(f, "r");
                    raf.seek(s);
                    byte[] buf = new byte[32768];
                    long left = n;
                    while (left > 0) {
                        int want = (int) Math.min(buf.length, left);
                        int got = raf.read(buf, 0, want);
                        if (got <= 0) break;
                        out.write(buf, 0, got);
                        left -= got;
                    }
                    raf.close();
                } else {
                    head(out, 200, mime, len, null);
                    FileInputStream fi = new FileInputStream(f);
                    byte[] buf = new byte[32768];
                    int got;
                    while ((got = fi.read(buf)) > 0) out.write(buf, 0, got);
                    fi.close();
                }
            }
            out.flush();
            c.close();
        } catch (Exception e) {
            try {
                c.close();
            } catch (Exception ig) {
            }
        }
    }

    private static void head(OutputStream out, int code, String mime, long len, String extra)
            throws Exception {
        StringBuilder sb = new StringBuilder();
        sb.append(code == 200 ? "HTTP/1.1 200 OK\r\n"
                : code == 206 ? "HTTP/1.1 206 Partial Content\r\n"
                : code == 416 ? "HTTP/1.1 416 Range Not Satisfiable\r\n"
                : "HTTP/1.1 404 Not Found\r\n");
        sb.append("Content-Type: ").append(mime).append("\r\n");
        sb.append("Content-Length: ").append(len).append("\r\n");
        sb.append("Accept-Ranges: bytes\r\n");
        sb.append("Cache-Control: max-age=86400\r\n");
        if (extra != null) sb.append("Content-Range: ").append(extra).append("\r\n");
        sb.append("Connection: close\r\n\r\n");
        out.write(sb.toString().getBytes("UTF-8"));
    }

    private static String readLine(InputStream in) {
        try {
            StringBuilder sb = new StringBuilder();
            int b;
            while ((b = in.read()) != -1) {
                if (b == '\r') continue;
                if (b == '\n') return sb.toString();
                sb.append((char) b);
                if (sb.length() > 8192) return sb.toString();
            }
            return sb.length() > 0 ? sb.toString() : null;
        } catch (Exception e) {
            return null;
        }
    }

    static byte[] bytes(InputStream in) {
        try {
            java.io.ByteArrayOutputStream bo = new java.io.ByteArrayOutputStream();
            byte[] b = new byte[16384];
            int n;
            while ((n = in.read(b)) > 0) bo.write(b, 0, n);
            in.close();
            return bo.toByteArray();
        } catch (Exception e) {
            return null;
        }
    }
}
