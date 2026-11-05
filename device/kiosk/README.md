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

## 构建与安装

```sh
# 需要 JDK 17+ 和一个带 build-tools/34.0.0 + platforms/android-19 的 SDK
ANDROID_SDK=/path/to/sdk JAVA_HOME=/path/to/jdk ./build.sh
adb install -r out/kiosk.apk
```

APK 约 12 KB，装在 `/data`（不是系统应用），`adb uninstall ink.eink.kiosk` 即可回退。

## 指向哪个地址

在家优先走 **LAN 直连**（`~/bin/eink-kiosk` 会算好地址）：

```
http://<Mac 的 LAN IP>:8000/?k=<token>
```

比起公网域名，这条路省掉跨境隧道（RTT 从 250ms 降到 ~15ms）和 nginx 的 Basic Auth。
`trust.json` 里已有家里的 /24，所以 LAN 过来的请求是 `full` 权限；token 仍然需要。

第一次进入会要求输入一次 6 位动态码（TOTP），之后 cookie 留在 WebView 里。
