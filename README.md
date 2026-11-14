# 小黄书 逆向版客户端（xhs-lite）

> 这是对某个 Android App「小黄书」（包名 `x2610.xiaoshushu02.com`）做过完整逆向分析之后，
> **从零重写的第三方客户端**，外加一套跑在电脑上的 Python 协议工具链。
>
> 原 App 里那些要充会员、要一条条付费解锁的内容，在这个客户端里不再受客户端侧限制。
>
> 仓库里只有代码，**不含任何成品 APK、签名私钥、账号、内容文件**。

---

## 一、先说清楚这是什么

原 App 是个套壳的成人内容 App，用小红书的皮做的马甲包，签名里写着 `CN=XiaoHongShu org`、SAN 填的是 `www.xiaohongshu.com`，跟小红书官方没有任何关系。

它有几个特点，决定了它好逆：

| 情况 | 结论 |
|---|---|
| 有没有加壳 | 没有。`classes.dex` 直接就是 `dex\n035`，10 万多条明文串随便读 |
| 有没有 VMP / Java2C | 没有。方法名、类名大面积明文，`RequestFactory`、`AESUtils`、`getBaseUrl` 都能直接看到 |
| 有没有 native 保护 | 没有。30 个 `.so` 全是 ijkplayer + ffmpeg 播放内核 + RenderScript |
| 有没有证书绑定 | 没有 pining，抓包直接能看 |
| 反调试 / 反模拟器 | 极弱。只有四处 `isRooted`、五处 `xposed`、四处 `frida` 字符串，没实际作用 |

所以整套东西就是「HTTPS + 全包体加密 + 客户端侧判断会员」，没有服务端强校验的硬骨头。

**这个仓库做的事**：抛掉原客户端，按逆向出来的协议自己写一个 WebView 壳 + Java 接口层，界面用 HTML/CSS/JS 画。功能自己说了算。

---

## 二、原 App 的技术底细（逆向结论）

### 网络栈

OkHttp3 + OkGo + Retrofit2 + RxJava2，域名不写死，由服务端下发：

```
GET v2/app/domain → ["https://app.xxx.com", ...] → 存 SharedPreferences(app_domain)
```

多域名热切换靠请求头 `Domain-Name` 指定，用的是 `me.jessyan.retrofiturlmanager`。

**推论**：改包硬编码换域名没用，得拦下发的域名，或者直接自己请求。

### 接口协议

HTTPS + POST，**包体和响应体整体加密**，135 个 `v2/` 接口全走这套。

请求头（少一个就 `{"result":-1,"message":"請求錯誤"}`）：

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

`Content-Type` 写成 `application/json` 会被网关直接挡掉，这是最容易栽的一步。

包体：

```
AES/CBC/PKCS5Padding
KEY = 525202f9149e061d   (16 字节 ASCII)
IV  = 985204f4819ec31c   (16 字节 ASCII)

明文 = {"s_time": <毫秒时间戳>, "user_token": "<token>", ...业务参数}
```

响应同法解密，外层 `{"data":..., "message":"...", "result":<int>}`，`result == 1` 才算成功。

### 图片是密文，不是图片

封面图 URL 长这样 `.../codstatic/img/<日期>/<哈希>.jpg`，但**拉下来不是 JPEG**，是密文：

```
AES/ECB/NoPadding，KEY 同上
解密后首字节 FF D8 才是合法 JPEG
```

对应原 App 里的 `AESUtils.Bitmapdecrypt`。

这一条是最坑的：你不解密，存下来的就是几十 KB 的白图，图库能打开、但一片白，看不出哪错了。

### 视频全是 HLS

没有 mp4 直链，m3u8 里挂：

```
#EXT-X-KEY:METHOD=AES-128,URI="/static/m3u8/enc.key",IV=0x...
分段：t0.ts / t1.ts / ...
```

`enc.key` 是 16 字节裸密钥，无鉴权，单独 GET 就给你。

**坑在这里**：`URI="/static/m3u8/enc.key"` 是**站点根路径**，不是相对于 m3u8 所在目录。
当成相对路径拼成 `.../<日期>/<6位码>/static/m3u8/enc.key` 会 404，key 取不到 → 分片原样拼接 → 得到一个能播才怪的 `.ts`，ffprobe 报 `Invalid data found when processing input`。

解密后是标准 MPEG-TS：188 字节一个包、首字节 `0x47`。落地前校验这个字节，就不会产出坏文件。

### 会员 / 付费在哪

原 App 的会员判断在客户端侧，服务端只回一个字段，UI 层自己决定要不要挡。相关入口：

```
v2/member/user-info     v2/member/user-level    v2/home/vip-note
v2/member/authed-review v2/pucha/vp-by          vp-page
bean/VipInfoBean  bean/VipPrivilegeBean  bean/MemberNoteList
vip/MemberCentreActivity  VipFragment  SVipFragment
baseBottomSheetFrag/BottomDialogPay  MemberCentrePayPouWin
```

因为是客户端自己判断，重写一个客户端就绕开了这套 UI 层限制——这也是这个项目能存在的原因。

---

## 三、这个客户端做了什么

一个 WebView 壳，`minSdk 24 / targetSdk 34`，界面全是 HTML/CSS/JS，接口全走 Java 侧。

```
AndroidManifest.xml
assets/
  ├── index.html      单页 UI：深色、双列封面墙、全屏播放器
  └── hls.min.js      hls.js 1.5.17，本地内置，不走 CDN
java/com/yg/xhslite/
  ├── MainActivity.java   WebView 宿主 + JS 桥 + 下载器 + 媒体库写入 + 缩略图拦截
  ├── Net.java            请求封装、AES 加解密、会话管理、登录
  └── LocalServer.java    本地回环 HTTP 服务（实验性，未启用）
res/                     图标与字符串
```

功能清单：

- 首页信息流（推荐 / 视频 / 图文分类）
- 关键词搜索，**分页与触底加载**，图文和视频各自独立页码，按 note ID 去重
- 作者页、我的解锁 / 我的喜欢 / 我的收藏 / 全部合辑
- 播放历史
- 全屏播放器（hls.js 直连 CDN，原生支持 AES-128 解密）
- 单条下载：ts 解密 → 拼流 → 转封装 mp4 → 写进系统相册
- 图文下载：ECB 解密 → 校验可解码 → 重编码 JPEG → 写进系统相册
- 我的下载：视频和图片统一从 MediaStore 读，能播能删

---

## 四、四个必踩的坑

### 1. 页面只能从 `file:///android_asset/` 加载

试过并失败的三条路：

| 方式 | 结果 |
|---|---|
| `loadDataWithBaseURL` + `shouldInterceptRequest` | 子资源请求**不触发**拦截，`ERR_NAME_NOT_RESOLVED` |
| 本地 `127.0.0.1:PORT` + `loadUrl` | 华为 WebView 把回环响应当 `text/plain`，整屏显示源码 |
| base64 走 `evaluateJavascript` 塞图片 | 字符串过长被截断，图裂 |

只有 `loadUrl("file:///android_asset/index.html")` 能正常渲染，而且 `shouldInterceptRequest` **能**触发。缩略图就走这条路。

### 2. 接口走 JS 桥，不走 WebView 拦截器

WebView 里发不了带 AES 包体的请求，所以接口全部由 Java 侧完成，JS 只负责渲染：

```java
@JavascriptInterface
public void req(int id, String path, String json)   // 结果回调 window.__cb(id, str)
```

好处是不用给 WebView 配拦截器，坏处是每个接口都得过桥。

### 3. 下载要写进 MediaStore，不能写私有目录

存 `getExternalFilesDir()` 的话，文件在 `Android/data/com.xxx/files/`，**系统媒体库根本不扫**，图库看不见，文件管理器要一路点进去才找得到。

正确做法是插 MediaStore：

- 视频 → `Movies/<相册名>/`
- 图片 → `Pictures/<相册名>/`

插进去系统自动索引，图库、相册、文件管理立即可见。列表也读同一处，不会出现「两处各一份」占双倍空间。

### 4. `resources.arsc` 必须 STORED 且 4 字节对齐

被压缩过就拒装：

```
Failure [-124: Failed parse during installPackageLI:
requires the resources.arsc of installed APKs to be
stored uncompressed and aligned on a 4-byte boundary]
```

`pack.py` 里对这一个条目单独 `compress_type = ZIP_STORED`。

另外 EMUI 上 `adb install` 会挂死，只有这一条能走通：

```bash
adb push x.apk /data/local/tmp/x.apk
adb shell cmd package install -r -t -g /data/local/tmp/x.apk
```

---

## 五、怎么编译

不需要 Gradle。JDK 17 + build-tools + platform 的 `android.jar` 就够。

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

# 4. dex（d8 必须显式列出全部 .class，传目录会报 Unsupported source file type）
java -cp $BUILD_TOOLS/lib/d8.jar com.android.tools.r8.D8 \
  --lib $ANDROID_JAR --min-api 24 --output build/dex build/classes/**/*.class

# 5. 重打包
python pack.py

# 6. 对齐与签名（用你自己的 keystore）
zipalign -f 4 build/unsigned.apk build/aligned.apk
apksigner sign --ks your.jks --out build/xhs-lite.apk build/aligned.apk
```

改完前端只动 `assets/index.html` 的话，不用重编 Java，把新 index.html 塞回 base.apk 再签名就行，快得多。

前端语法自检：

```bash
python _c.py            # 抽出内联 JS 到 _c.js
node --check _c.js      # 过一遍语法
```

---

## 六、Python 工具链

电脑端那套，方便不装 App 也能拉东西：

```
tools/
  ├── xhs2.py       协议核心：加解密、发请求、建会话
  ├── xhs_dl.py     HLS 下载与分片解密
  ├── xhs_fast.py   裸 socket 版高速下载器
  ├── xhs_find.py   关键词搜索 → 挑选 → 下载
  ├── xhs_batch.py  按编号批量下载系列
  ├── xhs_group.py  付费内容查询与逐条处理
  ├── xhs_lib.py    拉「已解锁 / 喜欢 / 收藏」三类列表
  ├── xhs_auto.py   签到 + 扫描 + 处理流水线
  ├── player.py     本地 Web 版播放器（脱离手机调试用）
  └── player_check.py  回归自检
```

先建会话（游客号即可，不用注册）：

```bash
python tools/xhs2.py            # 生成 creds.json
```

然后：

```bash
python tools/xhs_fast.py <note_id>              # 下单条
python tools/xhs_find.py 关键词 -p 2             # 搜两页
python tools/xhs_find.py 关键词 --pick 1,3       # 下第 1、3 条
```

ffmpeg 路径从环境变量 `FFMPEG` 读，没设就用 `PATH` 里的 `ffmpeg`。

---

## 七、这一版改了什么

按修复顺序：

1. **搜索分页**：原来视频只请求第 1 页、图文写死 `page 1`、总数砍在 40 条，且每次搜索清空重画，没有追加。改成图文/视频各自维护页码、按 note ID 去重、滚动触底加载、空页终止、失败保留页码可重试。
2. **图文下载空白**：`codstatic/` 的图是 AES/ECB 密文，原样存下来就是白图。改成先解密、再校验 `BitmapFactory` 能解码、才重编码 JPEG 落库。
3. **下载列表看不见文件**：原来只查 `MediaStore.Video`，图片压根不列。改成视频 + 图片都查、都能在 App 里播放/删除，统一到同一套 MediaStore 记录。
4. **「我的」页死入口**：「播放设置」「黑名单」两个入口没有 id、没有 handler，点了没任何反应，删掉。黑名单本身有本地实现在别处，不受影响。

---

## 八、声明

这个仓库是用来记录「移动端加密封包 + HLS AES-128 + 客户端侧鉴权」这套结构长什么样、以及怎么逆的。

仓库里不含成品应用、破解补丁、内容资源、第三方账号、任何个人信息。

自己拿去干什么，自己负责。
