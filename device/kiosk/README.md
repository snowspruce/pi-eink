# kiosk：Kindle 上的显示端

一个只有 WebView 的 App（约 120 行），用来替换装在 Kindle Android 侧的原厂浏览器。

## 为什么不用原厂浏览器

那台设备（CracKDroid / KPW3，Android 4.4.2）自带的 `com.android.browser` 是 AOSP Browser
4.4.2。它有两个和本项目直接冲突的行为，实测确认：

| 现象 | 原因 |
|---|---|
| 页面顶部一条 42 CSS px 黑带 | 它把 `fixed_titlebar_container` 高度压成 0，却给 `main_content` 留下陈旧的 89px 偏移（`0,89-1072,1356`）。重启浏览器能压住一阵，随后自己回来 |
| 软键盘盖住输入框 | 它是全屏窗口，全屏窗口在 Android 上**不会**因为 IME 被 resize，只会被覆盖 |

两个都是那个 App 自己的 UI，和 WebView 引擎无关。所以这里换成一个没有 UI 的壳：

- 无标题栏、无地址栏 → 没有可折叠的工具栏，也就没有黑带
- **非全屏**（`Theme.NoTitleBar` + `windowSoftInputMode=adjustResize`）→ IME 弹出时窗口重排
- BACK 重新加载页面而不是退出：这是个挂在墙上的显示端

## 实测数字（KPW3，1072×1448 @ 2.1）

```
原厂浏览器 + 全屏：  main_content 0,89-1072,1356     ← 89px 黑带
本 App 全屏（第一版）：WebView 0,0-1072,1356
                       点输入框 → mInputShown=true，高度仍 1356   ← 键盘盖住，不合格
本 App 非全屏：       WebView 0,0-1072,1312
                       点输入框 → 高度缩到 0,0-1072,621          ← 页面自己把输入框滚上来
```

代价：顶部 44px 系统状态栏（时间/电量）。全屏可以省掉它，但键盘就会盖住输入框——这一条是
Android 的行为，改不了。

## 图标

启动图标就是 pi 的官方 logo（`https://pi.dev/logo-auto.svg`），`res/mipmap-*/ic_launcher.png`
五档密度。原 SVG 留白多，直接渲染会比其他 App 的图标小一圈，所以生成时先 `-trim` 去掉空白、
再补 6% 边距、再缩放到各密度。

`build.sh` 里多了一步 `aapt2 compile --dir res`——没有资源时它跳过，所以之前的构建不需要。

## 构建与安装

```sh
# 需要 JDK 17+ 和一个带 build-tools/34.0.0 + platforms/android-19 的 SDK
ANDROID_SDK=/path/to/sdk JAVA_HOME=/path/to/jdk ./build.sh
adb install -r out/kiosk.apk
```

APK 约 12 KB，装在 `/data`（不是系统应用），`adb uninstall ink.eink.kiosk` 即可回退。

## 地址从哪来（不依赖路由器）

换网络时 IP 会变，设备又解析不了 `.local`（Android 4.4 没有 mDNS），所以地址是自己找的：

1. **显式 intent**（`eink-kiosk` 从 Mac 推过来）
2. **上次用过的地址**
3. **LAN 广播** `PI-EINK-DISCOVER?` → viewer 回 `PI-EINK-HERE <port>`，用回包的源地址
4. **扫本机 /24**：GET `http://<ip>:8000/whois`，回 `PI-EINK 8000` 的那台就是
5. **公网地址** `https://eink.blueberry.cafe/`（出门时的路）

广播常被 AP 丢掉（这台就是：Mac 的 UDP 监听一个包都没收到），所以才需要第 4 步——
254 个地址、32 线程、250ms 超时，大约 2 秒能找到一跳之外的那台机器。

**token 和地址是分开记的**：`/whois` 故意不吐 token（它免认证），所以 token 从任何一次带
`?k=` 的地址里学下来，之后拼到发现到的地址上。换网络只换地址，token 不用重输。

## 没有地址栏，怎么输地址

导航栏的 **⋮** 键 → 弹框里可以改地址，也可以点"自动发现"再扫一次。输入会记住。

## 安全模型（设备端）

三件今天补上的事，之前都是假的：

1. **证书只认 Let's Encrypt + CN 匹配**。Chromium 30 不信任当前 LE 链，所以公网地址一定会
   触发 `onReceivedSslError`；上一版无条件 `proceed()`，等于任何证书都接受——中间人可以拿
   走 token 和 OTP cookie。现在只对 `issuer=Let's Encrypt`、`cn=<host>` 放行，其余 `cancel()`
   并弹一个可见的说明。
2. **发现要对上 HMAC**。任何同网段机器都能回 `/whois`，而 App 会把 token 拼到"发现"到的地址
   上——那就是把 token 送给冒充者。现在 `/whois?n=<nonce>` 必须回
   `PI-EINK <port> <HMAC-SHA256(token, nonce)[:32]>`，验证不过就不用这个地址。
3. **token 只交给记得住的主机**。没有 token 时不做发现（第一次要手输/推送带 token 的地址）。

换 token 用 `~/bin/eink-rotate-token`：它同时换本机的 token、作废服务端 OTP 会话、清掉设备上
的 prefs（旧 token 和 OTP cookie）并重新拉起 kiosk。

## 出门（公网）

公网这条路要用到的三处，都是上一版会白屏的原因：

1. **证书**：`eink.blueberry.cafe` 的证书链这台 Chromium 30 不认。原厂浏览器会弹"继续"
   让人点，WebView 没有 `onReceivedSslError` 就**静默取消**——请求根本到不了 Mac（nginx
   access.log 里什么都没有）。App 现在直接 `proceed()`。
2. **Basic**：Mac 侧 nginx 现在认两种凭据——`?k=<token>` 或 `pieink` cookie（值就是 token），
   两者都不匹配才退回 Basic。所以设备端不需要用户名密码。
3. **OTP 表单**：它的 POST 不带 token 也不带 cookie，会被 Basic 挡在门外，所以 nginx 对
   `/otp` 单独 `auth_basic off`（这道门本身就是 6 位动态码 + 锁定计数）。

## 出门（公网旧版说明）

公网那条路一直还在（VPS + ssh 隧道 + Mac 上的 TLS/Basic），没有因为 LAN 优先而被去掉。
第一次走公网会弹一次 nginx 的 Basic 用户名/密码（原厂浏览器有缓存凭据，这个壳没有），
输一次就存下来。之后还有 viewer 的 6 位动态码，同样一次。

### 动态码只输一次

OTP cookie 是**按来源**存的：`http://192.168.x.x:8000` 和 `https://eink.blueberry.cafe`
是两个不同的 host，浏览器各存一份。而服务端那边的 OTP 会话只有一个（7 天有效）。

所以 App 把见到的 cookie 值记下来（`otp_cookie`），换来源时用 `CookieManager.setCookie()`
把它种过去。效果：**在哪个来源输一次都行，之后 LAN/公网/换网络都不用再输**——点 ⋮ 也不会。

只有这三种情况会再要一次：7 天到期、重装 App/清数据、或者 `force-stop` 发生在 cookie 落盘
之前（Chromium 4.4 是异步写盘的，正常使用遇不到）。
