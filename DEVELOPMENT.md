# 开发记录：怎么长出来的，以及踩过的坑

> 本文件是开发内部记录，**只有中文**，不随 README 做双语。英文 README 在需要避开
> "为什么长成这样"和排查阶梯时仍会链到这里，里面的命令可直接复制执行。

这份文件记两件事：这个项目分几层长出来、每一层为什么这么选；以及**真正花掉时间的坑**
在哪儿 —— 大多数不是"写错了代码"，而是"以为改好了但没生效"和"从症状猜错层"。

规模参考：`viewer/server.py` 约 3300 行、`extensions/eink-bridge.ts` 约 1060 行、
`install.sh` 约 390 行；测试 57 条（Node）+ 252 条（Python，含 HTTP 级用例）。
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

### 13. 会话 id 用文件名尾部猜，猜不中就换个对话

`session_path_by_id()` 曾是 `id[-8:] in path.name`。它假设文件名是 pi 的
`<timestamp>_<id>.jsonl`，但这个假设不总成立：写进已有文件、`--session <path>`、
或老版本留下的 `session.jsonl`，文件名里的 id 和头行的 `id` 都不一样（**本机实测
60 个文件里 14 个不一致**）。

后果不是报错，而是**静默换会话**：`?s=` 解析不出来 → `session_selected()` 返回 False
→ `resolve_view()` 判定"这个会话没了" → 回落到"最新活会话"。用户看到的两个症状是：

- 在会话列表里点任何非运行中的会话，都回到当前会话（那个 id 猜不出来）；
- 电脑上新开一个 agent 干活，Kindle 突然跳到这个新 agent（回落的落点就是"最新"）。

修法：id 一律从**文件头行的 `session` 行**读（`session_index()`）。一次遍历是目录列举 +
每个文件一次 `stat`（header 解析本身按 mtime 缓存）；同一个请求会把同一个 id 解析最多四次，
所以索引只在**一个请求内**缓存（`_INDEX_MEMO`，由 handler 在入口 `begin_request()` 清空），
绝不跨请求——过期的 id 会被读成“会话没了”，那正是换会话的失败。同时：

- 没有 `session` 头的文件（subagent 的 `*_transcript.jsonl`）不是 pi 会话，不进索引、
  不进列表——之前它们会以文件主干名当 id 出现在列表里，点进去同样回落；
- 同一个 id 多个文件时取 mtime 最新的（复制过文件的情形）；
- 运行中的会话即使 jsonl 还没落盘（刚新建那一两秒）也算"在"，否则"新建会话"按钮
  会把自己刚建好的会话判定成 gone。

教训：**凡是把"猜"当解析，猜错的那条路径必须显式报错，而不是回落到"那里有东西就给
你看"**——回落让解析失败变成了另一个功能。

### 14. 一个全局"当前会话"槽位冒充每页的当前会话

`view.json` 只有一个。页面不带 `?s=` 时按它重渲染，于是任何一台设备（或书签、或
`/theme` 跳回裸 `/`）都能改掉别人的页面。前一个修复里那层"首次访问 303 钉住 id"实际
上**已经死了**：`resolve_view()` 总会填上 remembered sid，`if not sid` 永不成立，所以日
常使用中页面从来没被钉住过（测试看不出来，因为测试每次都把 `view.json` 删掉）。

现在的模型一句话说得清：**每个页面钉住自己的会话**。

- 页面解析出的 id 写进它发出的每个请求（轮询 `/data?s=`、按钮、页脚链接），所以轮询不
  会去重新推导"当前是哪个"；
- 裸 `/` 一律 303 到 `/?s=<id>`（不再以"remembered 为空"为前提）；
- 按钮按页面自己的 `?s=` 路由（仅限 `full`；只读网络仍然忽略 `?s=`，按 viewer 渲染的
  那个会话路由——它看到的和它按到的必须一致）；
- `view.json` 退化成"没有 id 的页面落到哪"，不再等于"所有人的当前会话"。

第二个可复用的结论：**分权时不要以"一个全局槽位"为轴**。两份状态（每页 vs 全局）
必须在代码结构上也分开，否则以后任一次"顺手改一下默认值"都会把两者重新粘回去。

### 15. SHELL 模板里的裸 `%`

整页是 `SHELL % {...}` 拼出来的，所以往里面的 JS/CSS 写任何裸 `%` —— 取模、百分比、
甚至**注释里提到 `%`** —— 都会让格式化直接报 TypeError。

症状很斜：**首页 500，但 `/data` 照常 200**（它不经过 SHELL），看起来像"只有首页坏了"；
测试里也正好是 4 条页面断言变红、`/data` 那批全绿（本次实测）。

改法：JS 里避开取模（`if (x >= n) { x = 0; }`），非用不可就写 `%%`；注释里也不要写 `%`。

### 16. "pi 有队列功能，照抄过来就行"

想得很对，但**扩展拿不到 pi 的队列**。`ExtensionContext` 只有 `hasPendingMessages(): boolean`；
队列本体（`_steeringMessages` / `_followUpMessages`）和 `clearQueue()` 都在 `AgentSession` 上，
只绑给 RPC/json 客户端（`clear_queue` 命令），`queue_update` 事件也只发给 `this._eventListeners`
（即 RPC 流），扩展的 `pi.on()` 里没有这个事件。

所以两条路：要么做一个只有数字的“排队中”徽标和一个点进去什么也改不了的页（假控件，本项目
最反对的东西），要么盒子自己当队列的主人。选了后者：

- 忙时发的一切进 `~/.pi/eink/queue.json`，不往 pi 的 followUp 队列里塞，pi 那个队列因此是空的
  （也是为什么电脑端的排队消息“看不到”——真的看不到，不是没做）；
- 投递点是 `agent_settled`：每一轮结束投一条。`agent_end` 不行，它后面还可能有自动重试 /
  自动压缩 / 继续，`agent_settled` 才是“pi 不会自己再跑了”；
- 投递不是 `await` 完就算完：`pi.sendUserMessage()` 返回 void，运行开始是异步的。第一版用
  “400ms 后再查一次忙闲”，复审时探针打出来它的另一面：运行起得慢时会把第二条并进同一轮
  （README 承诺的“一条一轮”就不成立了）。现在以 `agent_start` 事件为准——只有确实没起运行
  （被 hook 吃掉）才在 400ms 后投下一条；
- 停下 连带清空队列（并报告丢了几条）。理由不是设计偏好：pi 自己的客户端拦截 Esc 的做法就是
  先 `clear_queue` 再 `abort`，而 pi 的 abort 会继续投递队列——对一个“把活儿全停掉”的按钮来说
  这是反直觉的；
- 文件而不是内存：`/reload` 会重建扩展实例（同一个进程新实例），放内存里就丢消息，而 pi 自己
  的队列能扛过 `/reload`，这是不能倒退的行为。

教训：**先确认平台给了什么，再设计 UI**。“这些是 pi 的正常功能”里的“正常功能”指的是终端里
的能力，不等于扩展能看到的数据；一个只靠 `hasPendingMessages()` 的“队列页面”会是另一个假控件。

### 17. 复审挖出来的四个坑（同一个功能的四个面）

写完自认为想清楚了，复审时用真模块写了四条探针，四条全中：

1. **空闲时点“加到排队”，回复说“已排队（第 1 条）”，其实它已经发出去了**——`qadd` 无条件
   `enqueue` 再 `void deliverNext()`。提示与实际不一致，比不提示更糟。现在空闲时直接发、回“已发送”。
2. **`session_start` 不区分 reason**：崩溃前排队的三条消息，一周后 `-r` 恢复会话时会自己跑起来。
   探针里把 `at` 设成 5 天前，`reason: "resume"` 一样触发投递。现在投递要**上弦**：本进程排过东西、
   或 `reason === "reload"` 才上弦；恢复回来的队列只列出来，页面上多一个 `开始投递`。
3. **排队文本不展开 prompt/skill**：空闲直发用的是 `expandPromptTemplates: true`，从队列投递用的是
   裸 `sendUserMessage`，于是排队的 `/skill:foo` 会变成聊天文本发给模型。现在投递也展开
   （steer 那条补救路径不行——pi 只在空闲发送时展开，这点写进注释）。
4. **空文本走 `qadd` 得到 500**（“失败：没有输入内容”），而 `say` 是 400。状态码错得不多，但它
   意味着“服务端出错”，会让读者去查根本没坏的东西。

另外两个只能算体验坑：`at` 缺失时渲染成 `08:00`（epoch 0 的本地时间，一个凭空造的时间戳），
以及 `/queue` 没有任何刷新手段。后者**不能**用 meta refresh 解决——它和“加一条”输入框同页，
刷新会冲掉正在输入的内容；改成一个 `刷新` 链接。

教训（补第 10 条“同一个症状多个入口”）：**自己写的功能也要用探针跑一遍四条边界**——空闲/忙、
reload/恢复、正常文本/命令文本、缺字段/坏字段。上面的 2 和 3 都是“两条看起来等价的路径其实不等价”，
这类 bug 单元测试不会自己长出来，得先把两条路径并排写出来看。

---

### 18. 顶部黑带：浏览器 quick-controls 的"半应用"状态

症状：地址栏已隐藏，但页面顶部有一条满宽黑带，看起来像把内容挡住了。

**先量，别猜。** 真机截屏（`adb shell screencap -p /sdcard/x.png` + `adb pull`）逐像素量：
状态栏白色 0..43，黑带 44..132（89 px @ dpr 2.1 = 42 CSS px），页面内容从 133 开始。
再读 View 层级（`adb shell dumpsys activity top`）：

```
# 坏状态（有黑带）
fixed_titlebar_container  0,0-1072,0        ← 地址栏容器高度被压成 0
NavigationBarPhone        0,0-1072,109      ← 地址栏控件本身还在，仍然绘制
main_content              0,89-1072,1312    ← 页面区从 89 开始，上方 89px 是窗口背景（黑）
BrowserWebView            0,0-1072,1223     ← 页面比可用高度少 42 CSS px

# 好状态（重启后）
fixed_titlebar_container  0,0-1072,20
taburlbar                 0,0-0,0           ← 真的没了
main_content              0,-20-1072,1312   ← 页面区回到顶端
BrowserWebView            0,0-1072,1332     ← 页面拿到全部高度
```

结论：**黑带在 WebView 外面**——浏览器把地址栏容器压成 0 高度，却没把页面区上移，中间
89px 露的是窗口背景（黑）。页面从头到尾没参与：`#wrap` 的 16px 内边距在文档顶部，滚动后
根本不在视野里，`body` 日间是白底。

修法（只有这一条）：用 `enable_quick_controls=true` **干净重启**浏览器。设备上就是
`~/bin/eink-addr off`（`am force-stop` → 改 prefs → `am start`）。重启后 `main_content`
回到 0，页面多出 42 CSS px；重启会回到 `https://eink.blueberry.cafe/` 并再弹一次证书警告，
点"继续"即可。

走过的弯路，值得记下来：

- 先量了仓库截图和 CSS，确认页面顶部没有任何满宽黑块——这一步是对的，但**不够**：接着凭
  截图推出"内容钻到带子底下"，于是加了 `EINK_TOP_PAD` / `?top=<px>` 旋钮和 `/diag` 的 `bt`
  字段。真机 `dumpsys` 一看，页面根本没被盖住：那个旋钮解决的是一个不存在的问题，已 revert
  （79d8560）。`bt`（`body.getBoundingClientRect().top`）也测不出这类问题——WebView 整体被
  挪下去时，页面内部坐标依然是 0。
- 真正管用的是三个数：**截图里黑带的像素范围**、`main_content` 起始 y、`BrowserWebView` 高度。
  放在一起，"页面被盖住"和"页面被挤下去"一眼分得清。
- `adb shell input swipe` 默认时长（300ms）会被当成**长按选中文本**，量到的"黑带"其实是选中
  工具栏。要么给 `swipe` 加时长参数（<100ms），要么改用 `input keyevent`。这一条害我白读了
  一张截图。

教训：**"看起来盖住了"和"真的盖住了"是两件事**，只有层级几何能区分。

### 19. 横向滚动条：`<li>` 没继承 word-wrap

症状：页面底部总有一条横向滚动条，滚动条滑块几乎通长（说明只超几像素）。

量法（不需要 devtools，设备就在 adb 上）：`/diag` 的几何 beacon 增加两个字段——
`dw`（`documentElement.scrollWidth`，和 `iw` 比就知道超没超）和 `who`（把每个元素按
`rect.right - iw` 与 `scrollWidth - clientWidth` 里较大的那个排序，取**最深**的三个，
每次页面加载跑一次）。真机读数：`iw=511 dw=520 who=li:21,ol:21,div.msg:21`。

结论：Markdown 列表渲染成 `<ul>/<ol><li>`，是 `.body` 的**兄弟**，而 `word-wrap: break-word`
只写在 `.body` 上。于是 `li` 里的长 token（路径、`key=value`、URL）不能断行，把 `li` 撑出
容器 21 设备 px ≈ 9 CSS px，正是那条滚动条。修法是给 `li` 补上 `word-wrap/overflow-wrap`。

两个附带教训：

- **beacon 的时机**：第一版把几何 beacon 放在页面加载时（IIFE），那时 `#content` 还是空的，
  `dw == iw`，把要找的溢出藏掉了。改到**第一帧 `paint()` 之后**才量。凡是"量布局"的探针，
  都要等被量的东西存在。
- **量错对象也会骗人**：`who` 一开始只报"盒比视口宽"的元素，结果一个都没报（盒都是 511 宽），
  因为溢出的是**行内内容**。加上 `scrollWidth > clientWidth` 才抓到；再加"取最深"才从
  `div#content` 收敛到 `li`。
- **探针别拷贝时间敏感的状态**：为了绕开 OTP，我习惯把 `~/.pi/eink/` 拷到临时 HOME 再起一份
  viewer。但 `bridges.json` 的 `updatedAt` 靠心跳推进，拷过来就冻结了——`live_bridges()` 按
  "600 秒内的心跳"过滤，冻结的副本在几分钟后整批失效，于是渲染出"所有项目都是旧 bridge"
  这种假结论，还差点让我去改一段本来正确的代码。要验时间敏感的数据，就用真 HOME 直接调函数
  （`python3 -c "import server; server.new_session_html()"`），别拷。

### 20. `esc()` 不转义引号，属性值就不是属性值了

`esc()` 一直是 `html.escape(..., quote=False)`：转 `& < >`，**不转引号**。文本节点里这没问题，
但属性值里引号就是定界符：

```
<input type="hidden" name="dir" value=".../hey" onmouseover="alert(1)">
```

一个叫 `hey" onmouseover="alert(1)` 的目录（克隆一个别人给的仓库就能造出来）就能往页面里塞属性。
`/new` 的目录名、OTP 页的 `?s=` 都踩在这条上。改法是把 `esc()` 改成 `quote=True`：文本节点里
`&quot;` 渲染出来还是 `"`，所以没有副作用，而所有属性站点一次封死。

教训：**转义函数要按"将被用在哪里"分档**（文本 / 属性 / URL / JS），只写一个 `esc()` 迟早
会在属性里出一次事。这个项目现在只有一种，所以把最严的那档当默认。

### 21. 新会话必须是新进程，不是"换个目录的同一个进程"

`ctx.newSession()` 结束当前会话，`switchSession(header-cwd 的空会话)` 把这个进程搬到另一个目录。
两者都会毁掉读者正在看的东西，所以都不对。pi 是全屏 TUI、没有 `--cwd`（cwd 来自进程），因此唯一
干净的做法是**开一个新窗口**跑 `cd <dir> && pi`：

```applescript
tell application "iTerm" to create window with default profile  -- 再 write text "cd ... && pi"
```

命令通过 osascript 的 argv 传（`on run argv`），不拼进 AppleScript 源码——目录名是文件系统输入，
`x'; rm -rf ~; '` 是合法名字。`shellQuote()` 负责单引号转义，这两层缺一层就是一个注入点。

代价：需要一次 iTerm 自动化授权；新进程要几秒才注册，所以 viewer 侧按"该目录出现了没见过的会话 id"
轮询 bridges.json（`wait_for_new_session_in`），而不是等 bridge 的回复——回复在窗口起来之前就写了。

### 22. 黑带是 `fullscreen` 偏好，不是 `quick_controls`

只设 `enable_quick_controls=true` 时，浏览器把标题栏压成 0 高却给 WebView 留了陈旧的 89px 偏移：

```
fixed_titlebar_container  0,0-1072,0      ← 高度 0
main_content              0,89-1072,1312  ← 却从 89 开始 → 顶部 42 CSS px 黑带
```

而且它在运行中会复现（重启也照样）。`fullscreen=true` 一起设才有干净布局：

```
content / main_content / BrowserWebView   0,0-1072,1356   ← 连系统状态栏那 44px 也收回来了
```

`~/bin/eink-addr off` 现在同时写这两个键；`~/bin/eink-heal`（launchd `com.wangke.eink-heal`，
每 2 分钟）在黑带回来时重启浏览器——判据是隔 20 秒读两次 `main_content` 的 y，两次都 >0 才算，
因为页面加载过程中它会短暂跳到 89 再落回 0（一次读数会误判）。10 分钟内最多修一次，防止循环重启。

### 23. TLS 层的错误不在 viewer 日志里

`ERR_SSL_PROTOCOL_ERROR`、`ERR_CONNECTION_RESET` 这类报错意味着**握手就没成**，请求根本到不了
viewer，所以 `~/.pi/eink/server.log` 里一个字都不会有——去哪里找都是白找。公网访问时 TLS 由另一层
终止（见 `~/Library/Application Support/pi-eink-tls/RUNBOOK.md`：VPS 只搬字节，Mac 上 nginx 才解密），
失败记录留在这三处：

| 位置 | 能看到什么 |
|---|---|
| 中转机 nginx stream 日志 | 每次 TCP 连接一行；`status=502` / `sent=0` = 上游（反向隧道）不在，此刻页面必然打不开 |
| 中转机 nginx error.log | `connect() failed (111: Connection refused) while proxying`，按小时聚簇 = 隧道在反复掉 |
| 本机隧道日志 + `launchctl print` | ssh 自己的退场原因（`remote port forwarding failed for listen port …`、`Timeout`）和重启次数、退出码 |

诊断顺序：`curl -o /dev/null -w '%{http_code}' https://<域名>/` → `000` 是 TLS/隧道，
`401`/`403` 是链路通、只是认证没过，有数字才去 viewer 日志里找。

链路自身的健康度用 `~/bin/eink-link`（launchd 每 2 分钟，日志 `~/.pi/eink/link.log`）衡量：
一行 `code=401 t=0.2s` 就是那一刻链路是通的，`code=000` 就是那一刻断了。没有这一行行的历史，
"间歇性出错"永远只能靠猜。

一条教训：`ExitOnForwardFailure=yes` 的隧道里**不要混装不同用途的转发**。ACME 的 9080 只是因为
上一个死会话还占着端口，就让整条隧道退出——连带把 9443（正在看的页面）带走。拆成两条隧道后，
续期那条爱掉掉。

### 24. 轮询的代价是字节，不是请求数

`/data` 每次回整段 HTML（实测 13.7 KB 平均、峰值 26.5 KB）。1.2 秒一轮 = 1.4 MB/分钟，穿过
250ms、丢包 12–25% 的隧道时，这才是"页面卡"的来源——不是请求频率。改法很小：客户端把自己手里的
digest 带上，服务端 digest 没变就只回 88 字节：

```
首次            19723 字节
digest 相同        88 字节     {"v":"6185fb9a1abe","same":true,"busy":true,…}
digest 过期     19723 字节
```

`digest` 覆盖 fragment + status，所以令牌数/队列数一变 digest 就变，状态行不会漏更新。`busy`
决定轮询间隔，因此 `POLL_MS` 的赋值放在 paint 的提前返回**之前**。

顺手记下一条判断教训：`limit_req` 那 599 条不是我们撞的，是一次 18 秒的扫描器（`GET /api/`、
`/html/`、`/favicon.ico`，我们根本没有这些端点），zone 是 10r/s，我们实测 0.83r/s。**看到限流日志
先确认被限的是谁的请求，再决定要不要调阈值**——调大只会把扫描器一起放进来。

### 25. 黑带和键盘都是原厂浏览器自己的 UI，不是页面

三条独立的实测，指向同一个结论——别在页面上找这个问题的解：

```
原厂浏览器 + quick_controls：  main_content 0,89-1072,1356     ← 89px 黑带（容器高度 0，兄弟没重算）
本 App 全屏：                  WebView 0,0-1072,1356  点输入框 → mInputShown=true，高度不变
本 App 非全屏 + adjustResize：  WebView 0,0-1072,1312  点输入框 → 0,0-1072,621
```

第二行是关键：**全屏窗口在 Android 上不会为软键盘 resize**，只会被盖住。所以"输入框顶到键盘
上面"和"没有黑带"不能靠同一个全屏开关同时满足——除非不用那个浏览器。

于是显示端换成 `device/kiosk`（120 行，12 KB，装在 /data，不是系统应用）。它是这个页面唯一
需要的 UI：没有工具栏可折叠，就没有黑带；`adjustResize` 让窗口随键盘缩小，页面自己的 resize
处理把输入框滚进来。代价：44px 系统状态栏。

顺带修掉一个 auth bug：`?k=<token>` 在需要 OTP 的网络上永远登不上——OTP 页把 `_authorized`
已经决定的 `Set-Cookie` 丢了，token 只出现在被那一页吃掉的请求上，之后的 `/` 没有凭据。

### 26. 关掉 Basic 之后，403 才是"链路正常"的信号

公网入口没有凭据时：Basic 在时是 `401`，Basic 关掉后变成 `403`（viewer 的 token 门）。
链路自检的判据写死了 `401`，于是把正常的 403 当成"断了"，连续两次就去重启隧道——一个
自己造出来的告警环。判据要按"TLS 与 HTTP 都完成了"来写，而不是按某个状态码：
`2xx/3xx/401/403` 都算通，`000` 与 `5xx` 才算断。

### 27. 一个环境变量被两个进程共用，就是把权限借出去

viewer 的 launchd 里 `EINK_BIND=0.0.0.0`（为了 Kindle 走 LAN 直连）。bridge 读的是**同一个**
变量名，于是每个 pi 会话的 bridge 也监听在所有网卡上（`node *:8013` `*:8014` …）。而 OTP 那层
只在 viewer 里——同网段的人只要拿到 token，就可以绕过第二因子直接打 bridge。

viewer 监听 0.0.0.0 是**有理由的**（门是 token + OTP，出行时 LAN 直连是唯一好用的路），
bridge 不是：设备只跟 viewer 说话，viewer 再代理到本机。现在 bridge 用 `EINK_BRIDGE_BIND`，
默认 `127.0.0.1`。

这一条的教训不是"绑回环"本身，而是：**共用配置名等于共用决定**。加变量时先问"谁会继承它"。

### 28. 安全姿态要能被核对，不能靠记忆

这套东西的安全结论散在五处：token 文件、viewer 监听、每个 bridge 的监听、nginx 配置、设备端
行为。靠"我记得是这么配的"核对必然漏——今天就漏了两处（nginx 硬编码注入 token、bridge 继承
`EINK_BIND` 上了 LAN），两处都是"看起来有门，其实是开着的"。

`~/bin/eink-audit` 把它们变成断言：全过退出 0，任何一项不符合退出 1 并打印修法。launchd
`com.wangke.eink-audit` 每天 09:10 跑一次 `--quiet`，结果进 `~/.pi/eink/audit.log`——只有出错
才会写东西，所以这行日志出现即是告警。

写脚本时踩的一个坑：`set -u` 下 **`$code（` 这种"变量紧跟中文标点"会被当成变量名的一部分**
（报 `code）：unbound variable`）。变量后面直接跟非 ASCII 就加花括号：`${code}（`。

### 29. 审计要分清"门坏了"和"路断了"

`eink-audit` 第一版把公网入口的 `000` 也算失败——于是跨境链路抖一下（实测 25 秒超时）就会报
"不符合预期"，而那时门其实是好的。判据要各归各位：**`200` 才是安全问题**（nginx 又在注入
token），`401/403` 正常，`000` 是可用性问题、交给 `eink-link` 跟踪，记一行 note 就好。

同一个脚本还有个低级 bug 值得记：`code=$(curl ... || echo 000)` 在 curl 失败时会输出
`000000`（`-w` 自己已经打了 `000`，`||` 又追加一个）。改成
`code=$(curl ...) || code=000; code=${code:-000}`。

### 30. 看门狗不能和用户的主动操作抢方向盘

kiosk 变成 HOME 之后，"不在前台"有两种含义：**崩了**，或者**读者自己按 ⋮→桌面 走了**。
按前台判的看门狗分不出来，会把第二种当成第一种，每分钟把人拽回页面。

判据要换成"**进程还在不在**"（`ps | grep`）：进程在就什么都不做，进程没了才拉起。这台 ROM
没有 `pidof`，`ps` 的进程名也截断到 15 字符——`ink.eink.kiosk` 刚好 15，所以能匹配上；换个更
长的包名就得另想办法。

同一轮的另一个小坑：`ipconfig` 在 `/usr/sbin`，而脚本里的 PATH 只补了 homebrew。launchd 的
默认 PATH 恰好包含它，`env -i` 手动跑就没有——测试环境和生产环境不一致时，先把 PATH 写死。

### 31. 这块屏上 2 CSS px 是最细的可读笔画

试过把按钮/输入框的边框从 2px 收到 1px（dpr 2.1，物理上还有 2 个像素，理论上"更精致"）。
真机结论：**看不清**，整排控件像没画框。e-ink 的灰阶渲染会把细线摊成浅灰，视觉上比 LCD 上的
1px 弱得多。

所以：控件边框 2px、表格线 1px（表格是背景线，不需要抢注意力），引用/代码块的左边线 5px。
这条别再试了——它不是审美偏好，是这块面板的分辨率下限。

### 32. 别把静态页面挂在会动的壳里

`/home` 第一版是塞进会话页那套 SHELL 里渲染的，于是它继承了会话页的轮询 JS——其中
`paint()` 里"贴底就滚到底"的逻辑让列表一打开就是滚到中间的状态。看起来像"主页被写坏了"，
其实是别人的代码在替我滚动。

静态页面要自己的文档：不引轮询、不带状态栏。判断标准很简单——**这个页面需要定时刷新吗？**
不需要就别让它进那个壳。

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

开机自启（把设备当第二屏，不想每次手点书签）：

- `/system/etc/init.d/` 在这台 ROM 上是**死目录**：`grep init.d /init*.rc` 一条不命中，
  里面那个 `99SuperSUDaemon` 从来不会被执行。别信目录存在。
- 真正会被执行的钩子是 SuperSU 的 `/system/etc/install-recovery-2.sh`
  （`install-recovery.sh` 结尾显式调它，而 `/init.rc:634` 的 `service flash_recovery` 跑
  install-recovery.sh）。
- 写进去的脚本必须**自己后台等 `sys.boot_completed` 再 `am start`，并且立刻 exit**：
  `flash_recovery` 是 oneshot 且会等它返回，卡住会拖慢整台机的开机（实测 `time sh` = 0.02s）。
- `am start -a VIEW -d <url>` 在浏览器**已在运行时会被忽略**，只把 task 提到前台
  （输出 `Warning: Activity not started, its current task has been brought to the front`）。
  改完 URL 要验证必须先 `am force-stop com.android.browser`。
- 那个几何 beacon 不能当容器宽度用：首次加载阶段会报 `w=0 h=0 sw=28`（早于布局），
  正常加载后 `window.innerWidth` 会报 **980**（默认布局宽度）而 `iw`/`sw` 是 511。

同一轮踩到的三个坑：

- **`enable_hardware_accel_skia=true` 会毁掉布局**：viewport 被撑成 980 CSS px，正文右
  侧整片被切。它确实能让 `dumpsys gfxinfo` 出现 DisplayList，但页面没法看，所以别开。
- **不要把重要信息固定在页面顶部**。浏览器工具栏显隐时 WebView 会整体
  上下平移，`position:fixed; top:0` 的元素会随之被切掉一半或整条跑出屏幕，而且偏移量
  不是常数 —— 补 10px 露出半截，补 24px 整条消失，没有一个 padding 能同时适配。
  所以状态行放在**文档末尾**（页脚上方）：在滚动流里，几何是确定的。
- **页面的缩放倍数按 URL 记忆**：同一个地址被放大过之后，清 app 数据、重写 prefs、
  重启都不复位（`browser2.db` 里没有 zoom 列，`app_webview/` 只有 Cookies/Web Data，
  没找到它存在哪儿）。换一个带新参数的 URL 立刻恢复正常 —— 所以排查渲染异常时，
  先换 URL 试一次，那是 3 秒的事。
- **`pm clear` 会把 prefs 清得只剩你手动设的几项**，缺失的键回落到代码默认值
  （`autofit_pages` / `default_zoom` 缺了就渲染异常）。要重置就写一份完整的 prefs，
  不要只 patch 想改的那几个键。
- 字体又踩一次：`\u22ef`（⋯，U+22EF）**不在字体里**，渲染成空方块。展开按钮最后用
  `\u25bc` / `\u25b2`（实测可用）。
- **`immersive.navigation` 对这套 ROM 不生效**（导航条照旧显示），而 `immersive.full`
  会把 WebView 撑破。所以底部导航条（返回/主页/多任务/前进/刷新）藏不掉——也没有必要藏：
  它的返回键目前是"退出浏览器回桌面"的唯一入口。Kindle 的电源键是息屏，不是 HOME。
- 状态行与页脚**合并成一行**：`sid8 · 路径(分支) · ↑in ↓out $cost pct% · 会话 重绘 夜间 EN`。
  砍掉了 cacheRead、cache-hit%、cacheWrite、context 窗口绝对值——都不改变决策，而这一行
  只有 9px 还要装链接。两个 span 之间用 `margin-left` 分隔，不是拼字符（`33%会话` 就是拼出来的）。

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

---

## 六、测试

Node 22+ 跑扩展测试，Python 3 跑 viewer（只是打开页面不需要这两样）。

```sh
npm test                 # 扩展：桩化 pi API、临时 HOME、真实 HTTP handler
npm run test:viewer      # viewer：单元 + HTTP 级强制点（252 条，约 10s）
npm run test:cov         # 扩展覆盖率（node 自带 reporter）
npm run test:cov:viewer  # viewer 覆盖率（需 `pip install coverage`）
```

两套都在 90% 行覆盖以上，数字以命令输出为准。

测试固定绑 `18019` 端口，不会和真 bridge 撞；永远不碰 `~/.pi/eink`：HOME 指向临时目录，并先
清掉 `PI_SESSION_FILE`（pi 会导出这个变量，它的优先级高于临时会话目录）。`test_http.py` 对着
假 bridge 起真 viewer，在传输层断言：只读对端即使请求到达 handler 也不能打字、动作落在页面正在
显示的那个会话上、请求不能选中网络没被展示过的会话。`test_render.py` 覆盖不到 socket 的部分：
markdown → html、一行 → 一个消息块、审批块、bridge 注册表过滤（复用的 pid 不能把死条目续命）、
会话解析。`test_server.py` 驱动纯判据：按网络的 capability、UA、锁定、信任。

## 七、手动安装（不用 install.sh）

```sh
mkdir -p ~/.pi/eink
openssl rand -hex 16 > ~/.pi/eink/token   # 长 token，可给脚本用
chmod 600 ~/.pi/eink/token
```

e-ink 上敲 32 位 token 很痛苦，建议再写一个短码代替它：

```sh
printf '482913\n' > ~/.pi/eink/pin && chmod 600 ~/.pi/eink/pin   # 6 位数字
```

然后决定**哪些对端可以连接服务**。没有这个文件时只允许 loopback，也就是 Kindle 会拿到 403：

```sh
cat > ~/.pi/eink/allow_ips <<'EOF'
# 每行一个 IP 或 CIDR。"#" 开头是注释。loopback 永远允许。
127.0.0.1
::1
192.168.0.0/16
EOF
```

viewer 和 bridge 读同一个文件，所以两边永远一致（`EINK_ALLOW_IPS` 只是文件缺失时的兜底）。

再决定**在哪儿可以打字**：把当前网络写进 `~/.pi/eink/trust.json`，否则首次访问是只读，直到输入
pin：

```json
{ "confirmed": ["192.168.1.0/24"], "peers": ["192.168.1.0/24"], "readonly": false }
```

`install.sh` 会按当前网络替你生成这份文件。

### 装 pi 扩展

推荐让 pi 管理这个仓库。本地源就地加载，不复制任何东西，`git pull` 就是更新方式：

```sh
pi install ~/pi-eink          # 把条目写进 ~/.pi/agent/settings.json
pi list                       # 应列出 ~/pi-eink 及其解析后的路径
```

`pi install` 记录的是**相对 settings 文件**的路径（`../../Documents/...` 这种），这是正常的。
不要在 `~/.pi/agent/extensions/` 再放一份 `eink-bridge.ts`：两份都会加载，一个会话会绑两个
bridge 端口并重复响应同一个按钮。

**本地目录还是 `git:` 源？** 对扩展来说都行，但只有本地克隆能同时承载 viewer 服务：
`git:github.com/you/pi-eink` 会被克隆到 `~/.pi/agent/git/...`，那棵树由 pi 拥有并在更新时重写
（`git reset --hard`、`git clean -fdx`，路径按 ref 生成）。把 launchd 或 systemd 指过去，下次
`pi update --extensions` 单元就坏了。用一个自己控制的克隆，把 `git pull` + 重启服务当作更新路径。

确认扩展已生效：启动 pi，然后看 `~/.pi/eink/bridges.json`——运行中的会话应带着自己的端口出现在
里面。没有的话，`~/.pi/eink/bridge.log` 会说原因。

### 跑 viewer

先在前台试：

```sh
EINK_PORT=8000 python3 ~/pi-eink/viewer/server.py
```

打开一次 `http://<host>:8000/?k=$(cat ~/.pi/eink/token)` 种下 `pieink` cookie，之后
`http://<host>:8000/` 就能直接用。Kindle 上可以用 `?k=<pin>` 代替长 token。

常驻用 launchd（macOS）：把 `service/com.example.pi-eink.plist.example` 拷到
`~/Library/LaunchAgents/`，替换所有 `/path/to/pi-eink` 和 python 路径，然后：

```sh
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.$(whoami).pi-eink.plist
```

Linux 用 `service/pi-eink.service.example`（`systemctl --user enable --now pi-eink`）。两种都注意：
`git pull` **不会**重启 viewer——单元只保证进程活着，不重载代码。更新后要：

```sh
launchctl kickstart -k "gui/$(id -u)/com.$(whoami).pi-eink"   # macOS，与 install.sh 的 label 一致
systemctl --user restart pi-eink                              # Linux
```

e-ink 注意：宿主睡眠时页面会变黑。第二个 launchd 单元里跑 `caffeinate -s`，接电源时保持唤醒。

## 八、会话退出之后怎么接回来

`/act/resume` 的链路，以及为什么它必须借道另一个 bridge：

1. Kindle 上那个会话没有自己的 bridge（进程没了），所以 `bridge_port_for(sid)` 是 0。
   但"开窗口"这个动作本来是某个 pi 扩展执行的 —— 只能借一个**还活着**的 bridge
   （`newest_bridge_port()`：`updatedAt` 最新的那个）。
2. viewer 只传 `file=`，不传目录。`pi --session <file>` 的 cwd 由会话头决定；让页面指定目录
   等于把一个可写参数交给客户端，而 header 是 pi 自己写的。
3. bridge 侧 `sessionFileFor()` 先 realpath、再要求 `startsWith(sessions 目录 + sep)`、要求
   `.jsonl`、要求是普通文件。**只校验后缀是不够的**：测试里种了一个 `/tmp/xxx/evil.jsonl`，
   头里写着 `cwd: /tmp`，去掉 realpath 校验后它会让 bridge 在任意目录开窗口。
4. 会话 id 不变（`--session` 继续同一段对话），所以不需要"发现新会话"那一套：
   `wait_for_bridge(sid)` 轮询 `bridge_port_for(sid)`，最长 30 秒。
5. 实测（2026-11-06）：靶子 `01a1ac6d`（13.8 小时前退出）→ POST `/act/resume` → 1 秒后
   注册到 8015 → Kindle 页面出现输入框与「停下/继续」。

顺带修的同源问题：`/reload` 之后注册表条目会被旧实例删掉（同 pid），以及心跳的临时文件
名固定导致互相覆盖 —— 见 `16076f7`。

## 九、短页面与只读页的两个排版坑

1. **`#acts` 是一行按钮条**（`white-space: nowrap; line-height: 1; overflow: hidden`），
   它是为「停下/继续/输入框」那一行写的。把一句完整的话直接丢进去，就会继承这三条：
   行距 1 让 23px 的字叠在一起，`nowrap` 让句子不换行，`overflow: hidden` 再把它裁掉。
   只读页的「在新窗口继续」现在有自己的 `.resume` / `.rhint` 盒子，字号 12px、行距 1.45。
1b. **空盒子会一直占地方**：发过一条命令后页面创建 `#result`（"已发送"那行），5 秒后清空**文字**
   但留着**元素** —— `min-height:20` + `padding:3+3` = 26 CSS px，在这块屏上就是一整行空白，
   而且留到页面下次重载。修法是 `#result:empty { display: none }` + 清理改成 `removeChild()`。
   **设备确认（2026-11-07 晚）**：发一条命令后那行空白 5 秒消失。探针的数字也记一下：
   `gap=6 acts=602 res=0` —— 输入行到状态行的正常间隔就是 `#wrap` 的 6px padding。

2. **页脚 `#bar` 在文档流里**（当初为了躲 WebView 顶部偏移，见二、31 那条记录），所以内容
   短于一屏时它会浮在中间。用 CSS 补不回来：`100vh` 在这个 WebView 里报得比可视区域小，
   `calc(100vh - …)` 直接没效果。最后的做法是 `fitToScreen()`——量页脚的 `offsetTop`，
   差多少就给 `#content` 补多少 `min-height`。
   顺带一个实测：`document.documentElement.scrollHeight` 和 `document.body.scrollHeight`
   **都被夹到视口高度**（内容 347px 时两者都报 625），所以"文档到底多高"只能从最后一个
   元素的 `offsetTop + offsetHeight` 读。

## 十、adb 掉线时先分清是哪一层

`eink-heal` 原来只有一句「adb 看不到设备（USB 掉了或设备休眠）」，把三件完全不同的事混在
一起：线掉了、设备睡了、adb server 卡住。2026-11-07 的一次掉线里，设备没重启（uptime
1天19小时、adbd 是开机时那个 pid）、Mac 没睡（pmset 那段时间只有断言活动，AC 供电）、
页面也没断（kiosk 的 window id 前后相同），但 adb 有 8 分钟看不到设备 —— 日志无法回答
"是哪一层断了"。

现在：
- 掉线时先看 `ioreg -p IOUSB` 里有没有 Kindle（USB 层在不在）。
- USB 在 → 重启 adb server 再试一次（不动设备上的任何东西）。
- 日志分两句：USB 上也没有设备（线/口/供电/睡眠，物理问题，脚本动不了）／ adb 不通但
  USB 里有设备（adbd 或 host 的 server 卡住）。
两条分支都用伪造的 `adb`（和伪造的 `ioreg`）跑过，不是只写在代码里。

## 十一、判断"通不通"的四个错判据（2026-11-07 一天踩全）

这一天的三次故障，症状都是"Kindle 上一屏白"，而我先后用错了四个判据。记下来，下次先看这张表。

1. **`ping` 100% 丢包 ≠ 不可达。** 这台 VPS 的 UFW 是 `policy DROP` + 只放行 22/80/443，ICMP 从来不回。
   当时 ping 全丢的同时 SSH 22/2222 正常。**唯一可信的判据是公网 URL 的 HTTP 状态码**
   （没有凭据时 403 就说明链路和 nginx 都在）。
2. **本机 9443 没有 LISTEN ≠ 隧道断了。** `ssh -R 9443:127.0.0.1:8443` 是在 **VPS 那一侧**监听，
   Mac 本机根本不会出现这个套接字。判隧道死活看两件事：到 VPS 的 ssh 是否 ESTABLISHED、
   公网 URL 是否应答。
3. **证书签发者不是 Let's Encrypt = 有人在中间。** 星巴克那次 `issuer = Aruba networks`：
   热点的登录拦截页在冒充所有 HTTPS。那不是"证书坏了"，而是**这台设备还没登录这个网络**；
   登录前任何 https 都打不开，kiosk 拒绝加载是**正确行为**（否则 token 交给热点运营方）。
4. **`adb devices` 空 ≠ 设备坏了。** 那天 USB 链路断过四次，最长一次 8 分钟自己恢复：
   设备 uptime 连续、`adbd` 还是开机那个 pid、kiosk 窗口 id 前后相同、Mac 也没睡。
   `eink-heal` 现在会分开报"USB 上没有设备（线/口/供电）"和"USB 在但 adb 不通（重启 adb server）"。

补一个自己造的坑：给"卡住"加提示页时，阈值定成 7 秒 —— 而这台设备走公网时 TLS 握手 + 40KB
页面经常要 5–8 秒。`loadDataWithBaseURL` 会**替换**正在进行的加载，所以提示页一出现，那次
本来会成功的加载就被杀掉了，而且当时没有重试 → 页面永远停在 Connecting。现在阈值 20 秒，
并且提示页本身也挂在退避阶梯上（提示 → 20 秒后重试那个地址）。实测：正常加载不再被截断；
一个必然失败的地址在 75 秒内请求 2 次（0s、40s），不会变成死页。

顺带一条产品侧的教训：**这些失败在屏幕上必须能区分**。现在 kiosk 分三种页面 ——
`连接中 <host>`（7 秒还没画完）、`连不上 <host> <原因>`（引擎放弃，20 秒→1 分→5 分退避重试）、
`证书不是这台机器的`（有人中间劫持，带"打开登录页"按钮）。

## 十二、那次原生崩溃到底崩在哪（tombstone 说话）

11:22 那次 `Fatal signal 11` 的墓碑还在设备上（`/data/tombstones/tombstone_05`，需要 root 读）：

```
signal 11 (SIGSEGV), code 1 (SEGV_MAPERR), fault addr 5e52e000
backtrace:
  #00 libskia.so          SkCanvas::drawRect(SkRect const&, SkPaint const&)+109
  #01 libskia.so          SkPicturePlayback::draw(SkCanvas&, SkDrawPictureCallback*)+966
  #02 libwebviewchromium.so  …
```

所以是 **WebView 的软件光栅（Skia）在回放这一页的绘制指令时崩的**，不是我们的 JS、也不是内存耗尽
（当时设备 396MB 总内存、220MB 空闲，kiosk PSS 只有 15.5MB）。

复现尝试 12 次（6 次冷启动 + 6 次不复位进程、主页↔那个 3.7MB 会话来回切）：**一次都没崩**，
pid 全程不变。结论：这是这颗 ROM 里 Chromium 30 的偶发原生 bug，与"数据量"没有稳定关系，
不去猜、也不为它改渲染。若再出现，取证路径是固定的：`su -c 'cat /data/tombstones/tombstone_NN'`
（`adb logcat -b crash` 在这台设备上不存在，`/dev/log/crash` 没有）。

## 十三、待机（"锁屏"）画面是 ROM 自己的机制，不是 Android 的

这台设备**没有** Keyguard（`mShowingLockscreen` 一直是 false，唤醒直接进 kiosk），也没有
Kindle 那套屏保服务（没有 `lab126` 包、没有 `/mnt/us`）。按住电源睡下的那张图来自别处：

```
/system/bin/sleepanimation        ← 二进制里硬编码 /data/firmware/wallpaper/1.png（这条路没生效）
/system/app/Eink.apk（Eink 设置） ← 显示 → 壁纸 → 安装：只有它写的那份才被用上
/data/firmware/wallpaper/image*.png  ← 实际生效的待机图片
```

正确做法（不要手写文件，走它自己的门）：

1. 把 PNG/JPG 放进 `/sdcard/wallpaper/`（≤10 张，**目录里别留 .bak**，它会把所有文件都搬过去）
2. Eink 设置 → 显示 → 壁纸 → 安装
3. 校验：`/data/firmware/wallpaper/image1.png` 的大小/哈希与你放进去的一致

我们那张图由 `device/screensaver/render.swift` 渲染（CoreGraphics，1072×1448 灰阶，和 viewer 的
`/idle` 页面同一套几何）：极简边框 + 四角刻度 + `PI / EINK … TERMINAL` + 点阵 + pi 标记
（实黑 / 45° 网纹 / 描边三块，因为这块面板没有彩色）+ 底部双线。

顺带两条实测结论：
- **睡着时面板保持上一帧**是不可能的 —— 这个 ROM 会自己画待机图，所以"睡下前先画 /idle"没用；
- 因此 idle 屏（5 分钟无触摸）+ 待机图是**两条独立的路**，都要有。
