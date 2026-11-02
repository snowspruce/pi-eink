# 开发记录：怎么长出来的，以及踩过的坑

> 本文件是开发内部记录，**只有中文**，不随 README 做双语。英文 README 在需要避开
> "为什么长成这样"和排查阶梯时仍会链到这里，里面的命令可直接复制执行。

这份文件记两件事：这个项目分几层长出来、每一层为什么这么选；以及**真正花掉时间的坑**
在哪儿 —— 大多数不是"写错了代码"，而是"以为改好了但没生效"和"从症状猜错层"。

规模参考：`viewer/server.py` 约 2160 行、`extensions/eink-bridge.ts` 约 720 行、
`install.sh` 约 370 行；测试 43 条（Node）+ 188 条（Python，含 HTTP 级用例）。
覆盖率：`npm run test:cov` / `npm run test:cov:viewer`，两边都在 90% 行覆盖以上（2026-11 时为
90.8%、91.4%）；数字以命令输出为准，这里不留快照。

---

## 一、它是怎么长出来的

| 阶段 | 做了什么 | 当时的驱动力 |
|---|---|---|
| 1. 只读看板 | `server.py` 读会话 `.jsonl`，单栏 HTML，轮询刷新 | Kindle 当第二屏，"看着 agent 干活就行" |
| 2. 动作桥 | pi 扩展在**进程内**执行动作（`pi.sendUserMessage` / `abort`），固定动作白名单 | 光看不够，要能"继续/停下/状态"；外部进程无法驱动 agent |
| 3. 多会话 | 每个 pi 进程一个端口（8010+hash）、`bridges.json` 注册表、会话选择器、动作代理 | 同时开好几个项目的 pi，一个页面得能切换且按钮作用到对的会话 |
| 4. e-ink 适配 | 字号/对比度/夜间模式/ASCII 旋转符/滚动跟随/节点复用重绘 | 511×573、dpr 2.1 的屏幕，残影和闪烁比"好看"重要得多 |
| 5. 安全模型 | ①身份（UA + token/pin + 5 次锁定）②能力（网络可信 → `full`/`readonly`）③信任绑定"解锁时那个网络" | 要能在家里、办公室、咖啡馆都用，同时"被打字"等于"能执行 shell" |
| 6. 打包与回归 | `install.sh` + launchd/systemd 模板 + 两套测试 + 多维度子代理审查 | 换台机器装一次就知道手工步骤太多；改多了必须有回归网 |

几个**定下来的结构性选择**（不是为了好看，是被问题逼的）：

- **桥必须在 pi 进程内**：发消息、中止、压缩上下文都是进程内 API，外部进程做不到。
- **viewer 必须是独立进程**：它只读会话文件，所以 pi 重启/崩溃时它还能显示最后状态。
- **每个会话一个端口**：多 pi 共用一个端口会出现"A 的按钮打到 B 的 agent"。
- **状态目录 `~/.pi/eink/` 与代码分离**：代码在仓库里、可 `git pull`；token/trust/日志不进版本库。
- **身份与能力分开**：cookie 回答"你是谁"，网络回答"你在这能干什么"。这是"咖啡馆只读、家里能打字"唯一说得清的模型。

---

## 二、最大的坑（按花掉的时间排序）

### 1. 改了磁盘 ≠ 生效

扩展要 `/reload` 才重新加载，viewer 要 `launchctl kickstart -k` 才换代码
（`KeepAlive` 只在进程退出时重启，`git pull` 不会让进程退出）。

代价：两次得出"已经修好"的结论后发现跑的是旧模块；有一次线上连续 30 分钟跑着
fix 之前的代码而无人察觉（`server.log` 当时没有版本横幅），导致后续所有验证都被污染。

现在的对策：启动横幅打印 `file:` 与 `rev:`（读 `.git/HEAD`，不依赖 `git` 在 PATH 里），
排查第一步就是 `grep 'listening on' ~/.pi/eink/server.log | tail -1`。

### 2. 同一个症状，三代不同的原因

"点发送没反应"出现过三次，每次根因都不一样：

1. 内联 `submit` 拦截没生效 → 走了原生表单 POST → 跳页后再跳回来（看着像"没反应"）
2. 防连点时 `disabled = true` 之后**从不恢复** → 按钮永久变灰
3. `x.status === 0`（连接被 reset）被当成成功 → 显示"已发送"但消息没出去，且按钮留在禁用态

教训：**症状一样的 bug 不要顺着上一次的记忆修**。这也是后来加 `/diag` beacon
（页面把点击、结果、JS 异常报给服务器）和 POST 日志的原因 —— Kindle 没有控制台，
没有日志通道时这三代问题在设备上无法区分。

### 3. 靠 UA 猜浏览器引擎，还猜错了方向

很长一段时间按 "Chrome 11 / WebKit 534"（没有 `display:flex`）设计布局。实际在用
的是 `Android 4.4.2 Kindle Build/4.0-rc2` —— 那是 **Chromium 30**，早就支持 unprefixed flex；
真正不带 flex 的是同一台设备上另一个浏览器（`Chrome/11.0.696.34`）。
两个 UA 都从同一台 Kindle 出现过，所以"按 UA 判断引擎"本身不可靠。

现在的对策：`/diag` 上报真实几何（实测 `w=511 h=573 sw=511 dpr=2.1`），并排布局用
`display:-webkit-box`（2009 版 flexbox，老引擎认）+ `display:flex`（Chromium 30 认）双写，
不用 `gap`（两者都不支持）而用 margin；`-webkit-box-sizing` 与 `box-sizing` 也双写。
教训的另一半：**不要因为一个引擎上的推断就去改另一个引擎上的体验** —— 曾经为了让
"老浏览器"不裁按钮，把输入框和按钮改成上下两行，结果在实际使用的引擎上白白丢了并排。

### 4. 测试测的是"判据"，不是"落点"

曾经 38 个用例全绿，但把 `do_POST` 里唯一那行 `if not action_allowed(...)` 删掉，
**依然全绿** —— 因为测试只调 `caps_for()` / `action_allowed()` 这些纯函数，没有一条走
HTTP 处理器。同样的坑在 bridge 侧重复过（删掉处理器里的 IP 门仍 16/16 绿）。

对策：新增 `viewer/test_http.py`，真起 viewer + 假 bridge，断言"只读下 `say` 被拒
**且 bridge 一次都没被调用**"；并用突变实验反向验证（删强制点 → 必须有测试变红）。

### 5. 自己造成的安全回归

两例，都是"加严"加过头：

- 把"错误 cookie 也计入 5 次锁定"之后，**换 token 会把自己锁死**：旧 cookie 每 1.2s
  轮询各计一次，6 秒触发 10 分钟锁定；而锁定判定在凭证校验**之前**，正确的 `?k=`
  也被一起挡住。
- `install.sh` 的模板注释里写了 `EINK_UA_STRICT` 字样，而判定是
  `grep -q "EINK_UA_STRICT" 整个文件` → 恒命中 → 每次重跑都强制开启 loopback UA 门，
  `--no-ua-strict` 永远无效。

对策：凭证校验先于锁定判定（正确凭证会清掉该 IP 的计数）；同一 stale cookie 每分钟
最多计一次；判定改结构化（`plutil -extract` / `sed`），不再对整文件做子串 grep。

### 6. 把 systemd 的写法套到 launchd 上

"保留旧 unit 里的 `EINK_*`"用的是 `grep 'EINK_[A-Z_]*=[^<>"]*'` —— plist 里是
`<key>NAME</key><string>VALUE</string>`，没有 `=`，所以 macOS 上这段逻辑**从未生效**，
每次重跑都静默丢掉 `EINK_BIND` 之类的设置。回填代码还写错了层级（插到根 dict 而不是
`EnvironmentVariables`），`plutil -lint` 照样通过、launchd 静默忽略。

对策：按平台两套采集，plist 用 `plistlib` 读改写，注入后 `plutil -extract` 断言位置。

### 7. 原子写的 tmp 名只用线程 id

`"<file>.<thread_ident>.tmp"` 在单进程内并发是干净的（实测 40×40 无损坏），但
**两个 viewer 进程的主线程 ident 相同**（实测都是 `8476458880`），于是它们互相顶掉：
`os.replace` 抛 `FileNotFoundError`，被 `except: pass` 吞掉 → 静默丢写
（3 进程压测：82k 次读里 47k 次非法 JSON）。

对策：tmp 名带 `os.getpid()`，写后 `fsync` 再 rename；同类问题在 `bridges.json` 也有。

### 8. 信任键只用裸 `/24`

"确认一次 = 永久 confirmed"意味着：家里解锁过 `192.168.3.0/24`，任何**恰好用同一网段**
的咖啡馆/酒店网络都直接拿到可打字权限（可打字 = 能驱动有 shell 的 agent）。

对策：确认绑定"解锁时的那个网络"，宿主网段一变就回 `readonly`；另有可选
`EINK_TRUST_HOURS` 让确认过期。残余：同网段同路由器仍无法区分，真要解决得上网关 MAC。

### 9. 文档凭记忆写

README 曾有 5 处与代码不符：`PI_SESSION_FILE` 写成"优先"（实际是最后兜底）、
`EINK_LOG_UA` 写进 `ua_seen.log`（实际进 `server.log`）、"both processes read them"
（bridge 根本不读 service unit 的环境）、`ua_seen.log` 的用途、same-subnet 的两端默认值相反。
每一处都让人按文档操作后困惑一次。

对策：改代码时同步 grep 文档；文档里每条命令/变量都注明属于哪个进程（viewer / bridge / both）。

### 10. 网络层问题伪装成鉴权问题

咖啡馆"打不开"的第一反应是 token/UA/信任，实际是 **Kindle 没连上热点**：
`arp -an` 里除了网关只有本机，`ua_seen.log` 无记录、`diag.log` 无 beacon —— 请求一条都没到。
后来又出现"两个 IP 都打不开"，因为其中一个网段（家里 192.168.3.x）本机根本不在。

对策：固定排查阶梯，**先证明"请求到了没有"，再怀疑鉴权**。

### 11. 依赖没声明 → 功能静默消失

`允许/拒绝` 的数据源是**另一个扩展** `safety-gate` 写的 `pending.json`。仓库里没有它、
`install.sh` 不查它、README 的 Requirements 不提它 —— 换台机器装完，这个块永远不出现，
`pending_html()` 直接返回空串，日志里一个字都没有。

更难发现的是：**这个功能的"缺失"和"空闲"在页面上长得一模一样**（没有待确认命令时
本来就该是空的）。对策：Requirements 写明它是前置依赖，`install.sh` 装完就报
"找到/没找到"并给出 clone + 软链命令，Troubleshooting 加一条"块不出现先看 gate
加载了没有"。gate 后来单独公开为
[snowspruce/pi-safety-gate](https://github.com/snowspruce/pi-safety-gate)。
判据是：凡是"空输出等于正常"的通道，都必须另有一条通道证明它不是坏的。

### 12. 默认视图绑了一个会变的目标

页面在 URL 没有 `?s=` 时，含义是"跟随最新活跃的会话"——而"最新"每次轮询都重算。
两个 pi 会话并行时，bridge 心跳（每 60s）谁落在后面谁就变成"最新"，整页换会话，
用户看到的是"页面自己跳了"。

根因在**默认值的选择**："最新"是个随时间变的目标，却被当成了"当前"。修法有两层：

1. 第一次解析到的 id 用 303 固化进 URL，轮询以后自带 `?s=`；
2. 凡是请求自己报不出会话（只读网络丢弃 `?s=`、书签里没有 `?s=`），就用服务端
   记录的 `~/.pi/eink/view.json`——**由服务端写、客户端读不到也写不了**，所以只读网络
   依旧不能"选"别的会话，但页面不会自己跳了。

第 2 层是关键取舍：只读策略原本拒绕 `?s=`，于是页面只能跟随"最新"。要同时满足"不能选"
和"不要跳"，就必须有一个客户端不能控制的状态源——cookie 不行（可伪造），文件可以。

两个附带教训：

- 同一个症状藏着第二、三个入口：只读网络会主动丢弃 `?s=`；`?s=` 指向的会话被删除/
  移走/ fork 后 `newest_session()` 静默回落到"最新"。三处都是"看起来正常地显示了
  另一个会话"，所以要一起修。
- 测试被环境变量污染：`pi` 会给子进程导出 `PI_SESSION_FILE`，而它是 `newest_session()`
  的兜底之一，于是测试读的是开发者真实的会话。跑在 agent 里的测试必须把这类变量
  （HOME 同理）先 pop 掉，否则"全部通过"测的不是你以为的东西。

---

## 三、排查阶梯（设备打不开时按顺序）

```sh
# 1. 服务活着吗、跑的是哪一版
lsof -nP -iTCP:8000 -sTCP:LISTEN
grep 'listening on' ~/.pi/eink/server.log | tail -1      # 看 rev: 是否等于 git rev-parse --short HEAD

# 2. 设备真的在同一个网络上吗（最常见的问题）
ipconfig getifaddr en0                                  # 本机地址，告诉 Kindle 该访问哪个
arp -an | grep "$(ipconfig getifaddr en0 | cut -d. -f1-3)"   # 设备是否出现在同网段
ping -c1 <kindle-ip>

# 3. 请求到了吗（页面每次加载都会打一条 beacon）
grep "<kindle-ip>" ~/.pi/eink/diag.log | tail -3
grep "<kindle-ip>" ~/.pi/eink/ua_seen.log | tail -3      # 有记录 = 被 UA 门拦了
grep -E 'POST /act|DENY' ~/.pi/eink/server.log | tail -5

# 4. 只读还是完整
cat ~/.pi/eink/trust.json                               # confirmed/peers/stamp/readonly
```

判断法：

- 第 2 步看不到设备 → 不是本项目的问题（热点/隔离/频段，见下）
- 第 3 步有 `ua_seen.log` 记录 → 把那条 UA 加进 `~/.pi/eink/uas`
- 第 3 步 `diag.log` 有记录但页面还是空 → 看 `/data` 是否 500（`server.log` 的 ERROR 行）
- 前两步都正常但仍是受保护页 → 该网络未解锁，页面上输 PIN

### iPhone 热点的两个坑

- 默认只发 **5GHz**，而 Kindle Voyage 只支持 2.4GHz → 必须打开
  「设置 → 个人热点 → 最大兼容性」。
- 网段是 **172.20.10.0/28**（只有 `.1`–`.14` 十四个地址），不是 /24；本项目的
  白名单/信任按 /24 聚合，兼容这个更小的网段，但手工写 CIDR 时别写成 `/24` 之外的值。

### 设备侧能测什么、上限在哪（2026-11 实测，CrackDroid / Voyage）

`adb` 可用（`product:kindlemod model:Kindle device:kindlemod`），但 adbd 是 Android 4.4
的版本，两个常用能力**不支持**，报错都是同一句 `adb: error: closed`：

- `adb exec-out screencap -p > x.png` → 产出 **0 字节**文件（这个坑会静默成功）
- `adb reverse tcp:8000 tcp:8000` → 失败，所以**USB 不能当网络通道**，页面只能走 WiFi

截屏必须写文件再拉：`adb shell screencap -p /data/local/tmp/k.png && adb pull /data/local/tmp/k.png`
（实测 1072×1448 PNG）。`adb logcat -d` 可用 —— 「Kindle 没有控制台」这个前提只在
adbd 不可用时成立。

量到的上限（决定了「要不要为这块屏重写前端」）：

| 项 | 实测 |
|---|---|
| 面板刷新节拍 | `dumpsys SurfaceFlinger` 里 `refresh=58823529` ns = **58.8ms/帧，约 17Hz** |
| 滑动时帧间隔 | min 58.8 / p50 58.8 / p90 1176 / max 4941 ms（46 帧） |
| 渲染路径 | `dumpsys gfxinfo` 无 Profile data、`DisplayList: 0.00 kB` → **软件渲染**，无硬件加速 |
| 内存 | MemTotal 396MB（MemFree 31MB / Cached 182MB） |
| EPDC 控制面 | `/proc/eink/panel/{id,bcd,waveform,working_buffer,readonly,data}` **只读面板信息，没有刷新触发** |
| 面板波形 | `320_G144_AC6202_ED060TC1C1_TC`（Voyage 的 ED060TC1） |

p90 那 1.18s 不能全算卡顿：这组间隔混入了「没内容变化就不重绘」的空闲段。能确定的只有
「贴得住 58.8ms 节拍的时候能跟上，跟不上时会停顿」。

结论，用来挡掉重复投资：

- **帧率天花板是面板节拍（17Hz）**，换前端框架/换语言都突破不了。
- 浏览器是**软件渲染**，这是长尾卡顿的来源；改它只能上原生，而原生唯一的收益是
  **`MXCFB_SEND_UPDATE` 的 A2 局部刷新**（消整屏黑闪、减残影），**不是变流畅**。
  做它需要 NDK + 与 SurfaceFlinger 共存，且 `/proc/eink` 这条路走不通。
- 前端**已经**做过三层优化，不要再做一遍：`paint()` 首行 `if (j.v === V) return` 短路、
  行级节点复用（`NODES[k]` + `__h` 比对）、忙碌 spinner 限频到 4.8s。
- 硬件侧无法通过开发改善：**无蓝牙**、**单个 USB 口**、6 寸触屏软键盘。

---

## 四、仍然开着的

- **同网段同路由器的信任撞车**：需要网关 MAC 才能区分，代价是换路由器要重新输 PIN，暂不做。
- **bridge 侧 `gate()` 的调用点没有测试**：前台子代理无法伪造源 IP，起不了"非 loopback 请求"；
  `gate()` 本身有单测，取舍是不为测试在生产代码里开后门。
- **`/act/status` 与 `/state` 的语义差异**：前者给持 token 的 LAN 对端，后者仅本机/受信代理，
  已写进 README 而不改行为。
- **`#top` 状态行溢出**：已在文档里说明优先级，未重排布局。

## 五、这套东西的可复用经验

1. **先建诊断通道，再修交互问题**。设备没有控制台时，"点不动/没反应/白屏"这三种症状
   在服务端看起来一模一样。
2. **测试要落在强制点**。只测纯函数会产生"全绿但能删代码"的假安全感；用突变实验
   反向验证测试的拦截力。
3. **改完先确认生效**（reload / kickstart / 版本号），再谈效果。
4. **安全加严要成对检查副作用**：加锁定要考虑恢复路径，加 grep 判定要考虑注释。
5. **跨平台不做字符串假设**：plist、systemd、shell 各有各的语法，判定走各自的解析器。
