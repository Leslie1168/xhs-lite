# xhs-lite

一个 Android 端第三方客户端的**技术实现**，以及配套的 Python 协议工具链。

仓库里只有代码：**不含任何成品 APK、签名私钥、账号信息、内容文件**。

---

## 项目构成

```
.
├── AndroidManifest.xml          # minSdk 24 / targetSdk 34
├── assets/
│   ├── index.html               # 单页 UI（深色、双列封面墙、全屏播放器）
│   └── hls.min.js               # hls.js 1.5.17，本地内置，不依赖 CDN
├── java/com/yg/xhslite/
│   ├── MainActivity.java        # WebView 宿主 + JS 桥 + 下载器 + 媒体库写入
│   ├── Net.java                 # 请求封装、AES 加解密、会话管理
│   └── LocalServer.java         # 本地回环 HTTP 服务（实验性，未启用）
├── res/                         # 图标与字符串
├── icons.py                     # 从一张源图生成各 dpi 图标
├── pack.py                      # 手工 APK 打包（zipfile 重排 + arsc 对齐）
├── _c.py                        # 前端 JS 语法自检
└── tools/                       # Python 协议工具链
    ├── xhs2.py                  # 协议核心：加密、请求、建会话
    ├── xhs_dl.py                # HLS 下载与分片解密
    ├── xhs_fast.py              # 裸 socket 版高速下载器
    ├── xhs_find.py              # 关键词搜索 → 挑选 → 下载
    ├── xhs_batch.py             # 按编号批量下载系列
    ├── xhs_group.py             # 付费内容查询与逐条解锁
    ├── xhs_lib.py               # 拉取"已解锁/喜欢/收藏"三类列表
    ├── xhs_auto.py              # 签到 + 扫描 + 解锁流水线
    ├── player.py                # 本地 Web 版播放器（可脱离手机调试）
    └── player_check.py          # 回归自检
```

---

## 协议要点

接口是 HTTPS + POST，包体整体加密，响应也是密文。

### 请求头

```
Content-Type: application/octet-stream; charset=utf-8
Accept: application/octet-stream
Accept-Language: zh-hk
Client-Type: 1
Client-Version: <版本号>
Client-Channel: <渠道号>
User-Id: <账号 hash>
User-Agent: okhttp/3.12.0
```

`Content-Type` 写成 `application/json` 会被网关直接挡掉，返回 `{"result":-1,"message":"請求錯誤"}`。

### 包体

```
AES/CBC/PKCS5Padding
KEY = 525202f9149e061d   (16 字节 ASCII)
IV  = 985204f4819ec31c   (16 字节 ASCII)

明文 = {"s_time": <毫秒>, "user_token": "<token>", ...业务参数}
```

响应体同法解密，外层是 `{"data":..., "message":"...", "result":<int>}`，`result == 1` 为成功。

### 图片

封面图 URL 形如 `.../codstatic/img/<日期>/<哈希>.jpg`，返回的不是图片而是密文：

```
AES/ECB/NoPadding，KEY 同上（对应官方 AESUtils.Bitmapdecrypt）
```

解密后首字节为 `0xFF 0xD8` 才是合法 JPEG。

### 视频

无 mp4 直链，全部 HLS：

```
#EXT-X-KEY:METHOD=AES-128,URI="/static/m3u8/enc.key",IV=0x...
分段：localhost.t0.ts / t1.ts / ...
```

`enc.key` 是 16 字节裸密钥、无鉴权、随包体 KEY 无关，单独 GET 即可。

**注意**：`URI="/static/m3u8/enc.key"` 是**站点根路径**，不是相对于 m3u8 所在目录。
按相对路径拼成 `.../<日期>/<6位码>/static/m3u8/enc.key` 会 404，取不到 key 就会把密文原样拼成 `.ts`，播放器报 `Invalid data found`。

解密后是标准 MPEG-TS：每 188 字节一个包、以 `0x47` 开头。可以用这个做落地前校验。

---

## 构建

不需要 Gradle。JDK 17 + build-tools r34 + platform-34 的 `android.jar` 就够。

```bash
# 1. 资源
aapt2 compile --dir res -o build/res.zip

# 2. 链接（生成 R.java 与 base.apk）
aapt2 link -o build/base.apk \
  -I $ANDROID_JAR --manifest AndroidManifest.xml \
  -R build/res.zip -A assets --java build/gen \
  --min-sdk-version 24 --target-sdk-version 34 --auto-add-overlay

# 3. 编译
javac -nowarn -source 8 -target 8 -bootclasspath $ANDROID_JAR \
  -encoding UTF-8 -d build/classes java/com/yg/xhslite/*.java build/gen/**/R.java

# 4. dex
java -cp $BUILD_TOOLS/lib/d8.jar com.android.tools.r8.D8 \
  --lib $ANDROID_JAR --min-api 24 --output build/dex build/classes/**/*.class

# 5. 重打包（pack.py 负责把 classes.dex 塞进去并修正 resources.arsc）
python pack.py

# 6. 对齐与签名
zipalign -f 4 build/unsigned.apk build/aligned.apk
apksigner sign --ks <your.jks> --out build/xhs-lite.apk build/aligned.apk
```

### 两个必踩的坑

**`resources.arsc` 必须 STORED 且 4 字节对齐。** targetSdk ≥ 30 时被压缩会直接拒装：

```
Failure [-124: Failed parse during installPackageLI:
requires the resources.arsc of installed APKs to be
stored uncompressed and aligned on a 4-byte boundary]
```

`pack.py` 里对这一个条目单独 `compress_type = ZIP_STORED`。

**EMUI 上 `adb install` 会挂死。** 只有一条命令能走通：

```bash
adb push x.apk /data/local/tmp/x.apk
adb shell cmd package install -r -t -g /data/local/tmp/x.apk
```

---

## 客户端实现要点

### 接口走 JS 桥，不走 HTTP 代理

WebView 里发不了带 AES 包体的请求，所以接口由 Java 侧完成，JS 只负责渲染：

```java
@JavascriptInterface
public void req(int id, String path, String json)   // 结果回调 window.__cb(id, str)
```

好处是不用给 WebView 配拦截器；坏处是每个接口都要过桥。

### 页面加载只能走 `file:///android_asset/`

试过并失败的三条路：

| 方式 | 结果 |
|---|---|
| `loadDataWithBaseURL` + `shouldInterceptRequest` | 子资源请求**不触发**拦截，`ERR_NAME_NOT_RESOLVED` |
| 本地 `127.0.0.1:PORT` + `loadUrl` | 华为 WebView 把回环响应当 `text/plain`，整屏显示源码 |
| base64 走 `evaluateJavascript` 塞图片 | 字符串过长被截断，图裂 |

`loadUrl("file:///android_asset/index.html")` 能正常渲染，`shouldInterceptRequest` 也**能**触发。缩略图就走这条路。

### 缩略图本地解密

`shouldInterceptRequest` 拦 `codstatic/` 与 `/static/vod/` 两类 URL：

1. 下载密文
2. 已是 JPEG 就用，否则 `AES/ECB/NoPadding` 解密
3. 缩到 540px、quality 82
4. 落盘缓存，下次直接给

### HLS 直连

CDN 带 `Access-Control-Allow-Origin: *`，所以手机端 hls.js 可以直接拉 m3u8 / ts / key，不需要本地代理，也不需要自己实现解密——hls.js 原生支持 `#EXT-X-KEY:METHOD=AES-128`。

### 下载器

Java 侧做，因为要把 ts 转成 mp4：

1. 拉 m3u8，解析 `#EXT-X-KEY` 与分段列表
2. 按 `scheme://host` + `URI` 拼 key 地址（**不是相对路径**）
3. 逐段下载 → `AES/CBC/NoPadding` 解密 → 手动去 PKCS5 填充
4. 拼成 ts，校验首字节 `0x47`
5. `MediaExtractor` + `MediaMuxer` 转封装成 mp4
6. 写进 `MediaStore`：`Movies/<相册名>/`，系统图库立即可见

第 6 步很关键。存 `getExternalFilesDir()` 的话，文件管理器要一路点进 `Android/data/...` 才看得到，系统媒体库根本不扫。

---

## 工具链用法

先建会话（游客号即可，不需要注册）：

```bash
python tools/xhs2.py            # 生成 creds.json
```

下载单条：

```bash
python tools/xhs_fast.py <note_id>
```

搜索并挑选：

```bash
python tools/xhs_find.py 关键词 -p 2      # 翻 2 页
python tools/xhs_find.py 关键词 --pick 1,3 # 下第 1、3 条
```

ffmpeg 路径从环境变量 `FFMPEG` 读，没设就走 `PATH` 里的 `ffmpeg`。

---

## 免责声明

本项目仅用于协议格式与客户端实现的技术研究，方便理解移动端「加密封包 + HLS AES-128 + 客户端鉴权」这一套常见结构是怎么搭起来的。

仓库内不含任何成品应用、破解补丁、内容资源或第三方账号。使用前请自行确认所在地法律与目标服务条款。

---

## License

MIT
