# pi-eink

[English](README.md) · **简体中文**

![手握 Kindle，显示 pi-eink 页面](docs/hero.jpg)

把 Kindle，或者任何勉强能渲染 HTML 的浏览器，当作正在运行的 [pi](https://pi.dev) 会话的第二块屏。
页面显示实时对话，五个白名单按钮把动作送回 agent。

![真实 511×573 电子墨水屏上的页面](docs/kindle-page.png)

状态行里是花费与上下文占用，下面是对话，再往下是 停下 / 继续 / 审查 / 执行 / 状态 和一个输入框。
阅读不需要 JavaScript；轮询就地替换消息列表，屏幕不会整页闪烁。agent 干活时 1.2 秒轮询一次，
空闲时 3 秒。

定位：个人自用，一个 viewer、几个会话、自己掌控的网络。它不是多用户服务，也不是沙箱：这个页面
能驱动一个带 shell 的 agent。

本文件与英文版 [README.md](README.md) 同步维护，两者不一致时以英文版为准。

---

## 安装

前置条件：

- pi 已装好可用（`pi --version`）
- Python 3，只用标准库（不需要 `pip install`，在 3.14 上测过）
- macOS（launchd）或 Linux（systemd）承载 viewer 服务。同一局域网里任何常开的机器都行，不必是跑
  pi 的那台
- 一台带浏览器的设备：页面是纯 HTML 加一小段内联样式
- 可选，[pi-safety-gate](https://github.com/snowspruce/pi-safety-gate) 扩展。它是独立仓库，装在
  同一个 pi 进程里，把危险命令停在 `~/.pi/eink/pending.json`，那是 允许/拒绝 块的唯一来源。不装
  也能用：危险命令只在本机终端确认，页面永远不出现待确认块。不要装两份
  （`~/.pi/agent/extensions/` 和 `pi install` 各一份），否则同一条命令会问两次

  ```sh
  git clone https://github.com/snowspruce/pi-safety-gate.git ~/.local/share/pi-safety-gate
  ln -sfn ~/.local/share/pi-safety-gate/extensions/safety-gate ~/.pi/agent/extensions/safety-gate
  ```

在将要提供页面服务的那台机器上：

```sh
git clone https://github.com/snowspruce/pi-eink ~/pi-eink
~/pi-eink/install.sh
```

它建 `~/.pi/eink`（权限 700），生成新的 `token` 与 `pin`、默认的 `uas` 与 `allow_ips`，并按当前
网络写一份 `trust.json`。它用 `pi install` 注册扩展，并禁用一个会重复加载的松散副本。它装好并启动服务单元（macOS 上
launchd，Linux 上 `systemd --user`），路径填成绝对路径，检查 viewer 是否有响应，最后打印两个只
开一次的 URL：电脑上 `?k=<token>`，Kindle 上 `?k=<pin>`。重跑是安全的：已有的 `token`/`pin` 不会重新生成，已有的
`EINK_UA_STRICT` 设置会保留。

| 参数 | 含义 |
|---|---|
| `--port N` | viewer 端口，默认 `8000` |
| `--label NAME` | 服务 label，默认 `com.$(whoami).pi-eink` |
| `--no-service` | 除服务单元之外全部执行 |
| `--no-pi` | 跳过 `pi install` |
| `--ua-strict` / `--no-ua-strict` | 强制打开或关闭 loopback 的 UA 门禁 |
| `--uninstall` | 停掉并删除单元、注销包，保留状态 |
| `--keep-pi` | 配合 `--uninstall`，保留 pi 包注册 |

然后在 pi 里 `/reload` 让 bridge 扩展加载，刷新 Kindle 页面。

### 验证

三项检查，按顺序做，各自证明一层：

```sh
pi list                                              # 包已注册
cat ~/.pi/eink/bridges.json                          # 本会话在里面，带着自己的端口
grep 'listening on' ~/.pi/eink/server.log | tail -1   # 服务在跑；rev 应是你的 HEAD
```

在电脑上打开 `http://<host>:8000/?k=<token>`：对话应能渲染，按钮应可用。Kindle 用
`http://<host>:8000/?k=<pin>`，设备与宿主必须在同一局域网（iPhone 热点要开"最大兼容性"，
Voyage 只支持 2.4 GHz）。

页面能开但少一个按钮，说明那个会话没有在提供 bridge，`~/.pi/eink/bridge.log` 会说原因。输入框
不见了，说明网络还没解锁，输一次 pin 即可。这两种都不是鉴权失败。

不用 `install.sh` 的手动装法见 [DEVELOPMENT.md](DEVELOPMENT.md)。

### 卸载

```sh
./install.sh --uninstall            # 停掉并删除单元、注销包
./install.sh --uninstall --keep-pi  # 同上，但保留 pi 包注册
```

它读 `~/.pi/eink/install-meta` 里记的 label 与端口，所以自定义过 `--label` 也不用重复；它还会把
安装器移走的松散 `eink-bridge.ts` 放回去。状态保留，`rm -rf ~/.pi/eink` 会删掉 token 与历史配置。

---

## 工作原理

| 部件 | 形态 | 为什么不是别的 |
|---|---|---|
| `extensions/eink-bridge.ts` | pi 扩展，加载在每个 pi 进程里 | 发提示词要用进程内 API（`pi.sendUserMessage()`、`ctx.abort()`），外部进程做不到 |
| `viewer/server.py` | 独立进程（launchd / systemd） | 它只读会话 `.jsonl` 快照，所以 pi 重启后仍在，继续显示最后状态 |
| `~/.pi/eink/` | 运行状态与密钥，永不入库 | `token`、`pin`、`allow_ips`、`uas`、`bridge.json`、`bridges.json`、`pending.json`、`answer.json`、`view.json`、日志 |

每个 pi 进程绑自己的 bridge 端口（基数 `8010`，按会话 id 的哈希偏移，撞了往后走），并把自己注册
进 `~/.pi/eink/bridges.json`。所以一个 viewer 服务所有运行中的会话，把每个按钮代理到该会话自己的
端口。

```
Kindle ──HTTP──▶ viewer :8000 ──proxy──▶ bridge :8010+N ──pi.sendUserMessage()──▶ agent
                      │                        │
                      └──── ~/.pi/eink/*.json ─┘
```

允许/拒绝 块不在这里实现。它是与 `safety-gate` 的一份握手，走 `~/.pi/eink/` 里的两个文件：

| 文件 | 写入方 | 读取方 |
|---|---|---|
| `pending.json` | safety-gate 把危险工具调用停在这里，**带它的 `pid`** | viewer（渲染块）、bridge（回答它） |
| `answer.json` | bridge 写入裁决，**带同一个 `pid`** | safety-gate（放行调用，或阻断它） |

两侧都校验归属，两个检查都必须保留。viewer 只显示 `pid` 与当前渲染会话相符的块
（`pending_owned_by()`），所以页面永远不会引用别的项目的命令行；bridge 拒绝回答 `pid` 不是自己的
块（HTTP 409，`eink-bridge.ts:394`），因为 `pending.json` 被所有 pi 进程共享。

---

## 配置

viewer 的变量写进服务单元。bridge 的变量必须在启动 `pi` 的那个环境里：bridge 是进程内扩展，看不到
单元的环境。

| 变量 | 进程 | 默认 | 含义 |
|---|---|---|---|
| `EINK_PORT` | 两者 | `8000` | viewer 端口；bridge 也用它重定向自己的 `/` |
| `EINK_BIND` | 两者 | `0.0.0.0` | 监听网卡；同时有 VPN 或 docker 网卡的机器要钉到局域网地址 |
| `EINK_BRIDGE_PORT` | 两者 | 空 | 强制指定一个 bridge 端口，不用按会话哈希 |
| `EINK_ALLOW_IPS` | 两者 | 空 | `allow_ips` 缺失时的对端兜底白名单（逗号分隔，IP 或 CIDR） |
| `EINK_ALLOW_UA` | 两者 | `Kindle Build/` | User-Agent 兜底白名单 |
| `EINK_UA_STRICT` | viewer | 关 | `1` 时对 loopback 也执行 UA 检查 |
| `EINK_VIEWER_IPS` | bridge | 空 | viewer 跑在另一台机器时，允许它代理自由文本与审批的主机列表；空 = 仅 loopback |
| `EINK_ALLOW_SAME_SUBNET` | 两者 | 开 | 宿主自己 /24 内的对端放行；`0` 关掉，只认 `allow_ips` |
| `EINK_LOG_UA` | viewer | 关 | `1` 时把每个（对端，User-Agent）组合记一次到 `server.log`；`ua_seen.log` 只记被门禁拒绝的 UA |
| `PI_SESSION_FILE` | viewer | 空 | 最后兜底，只在没有活 bridge、也没有 `bridge.json` 指认会话时用 |
| `EINK_TRUST_HOURS` | viewer | `0` | `N` 表示网络确认在 N 小时后过期，`0` = 直到网络变化 |
| `EINK_LANG` | viewer | `zh` | 默认界面语言；`en` 让页面说英文，设备仍可在页脚或 `/lang?l=` 覆盖 |

`~/.pi/eink/` 下的状态文件，全部被 git 忽略：

| 文件 | 写入方 | 作用 |
|---|---|---|
| `token`、`pin`、`masked_secrets` | 你 | 凭据；作废的 token 会被掩码，不会漏进页面输出 |
| `allow_ips`、`uas` | 你 | 对端与浏览器白名单 |
| `trust.json` | viewer | 哪些网络可以打字（`confirmed` 宿主子网、`peers` 对端子网、`readonly` 全局开关）；用 pin 解锁网络时写入 |
| `fails.json` | viewer | 失败计数，所以重启或 `kickstart -k` 不会清掉锁定 |
| `install-meta` | `install.sh` | 已装单元的 label、端口、系统，`--uninstall` 读回 |
| `actions.log` | viewer | 每个动作一行：时间、对端 IP、权限、动作、结果 |
| `bridge.json` | bridge | 最近一次心跳，"有没有活着的 bridge" |
| `view.json` | viewer | 页面最后渲染的会话；任何无法自己指认会话的请求（`?s=`、只读网络）都按它重渲染，所以页面不会在会话间乱跳 |
| `bridges.json` | bridge | 会话 id → `{port, pid, cwd, sessionFile, updatedAt}`，60 秒心跳，超过 10 分钟的条目不认 |
| `pending.json`、`answer.json` | safety-gate 与 bridge | 停住等确认的危险命令，以及页面的裁决 |
| `bridge.log` | bridge | 某个会话为什么不提供页面，例如端口被占 |
| `diag.log`、`ua_seen.log` | viewer | 页面 beacon 与被拒的 UA；都在 256 KB 时轮转到 `.1` |
| `server.log` | 服务单元 | stdout 与 stderr，代码不轮转它，涨了就 `: > ~/.pi/eink/server.log` |

### 端点

viewer 在 `:8000`：`/` 页面骨架（首次访问没有 `?s=` 时会被重定向一次到 `/?s=<id>`，轮询因此停在你
打开的那个会话上）、`/data` JSON 片段、`/static` 无 JS 兜底（没有 JS 的浏览器表单 POST 落到这里）、
`/sessions` 选择器、`/theme?d=0|1` 昼夜 cookie、`/lang?l=zh|en`、`/diag`、`/glyphs`、
`/favicon.png`、`/done/<key>`（从 bridge 的确认页转来），以及代理到该会话 bridge 的
`POST /act/<action>`。两个动作是 viewer 本地的：`/act/unlock` 输 pin 解锁当前网络，
`/act/readonly` 退回只读。

bridge 在 `:8010`+ 或 `EINK_BRIDGE_PORT`：`GET /`、`/state`，以及
`POST /act/{stop,continue,review,execute,status,say,approve,deny}`。非 loopback 对端只接受 `stop`
与 `status`；自由文本、审批与提示词必须走 viewer 的代理。

### 界面语言

页面的外壳——六个按钮（五个走 bridge，一个是本机的只读开关）、提示、页脚、动作回执——有中英两套。对话本身永远不翻译。默认中文，英文按
设备选择开启，三种方式：点页脚的 `EN` / `中文` 链接，它种一个一年期的 `pielang` cookie；打开一次
`/lang?l=en`（同一件事，从书签进来更方便）；或在服务单元里设 `EINK_LANG=en`，把它变成所有设备的
默认。安装时不问。

```sh
EINK_LANG=en python3 ~/pi-eink/viewer/server.py    # 前台
# 或写进 launchd / systemd 单元：EnvironmentVariables -> EINK_LANG = en
```

按钮发给 agent 的提示词（`继续` / `对以上优化和修复做一个审查` / `根据建议执行`）故意保持中文。
它们是指令不是界面文字，措辞不与读者的语言绑定。

---

## 安全

两个问题分开答：你是谁，以及你在这里能做什么。

身份：

1. User-Agent 命中 `uas` 里的一条。可伪造，所以只是一道减速带
2. HttpOnly cookie `pieink`。`?k=<token>` 用 token 种它；`?k=<pin>` 用一个随机会话密钥种它，永远
   不是 token 本身，因为 token 同时能给 bridge 和 `install.sh` 认证，且改 pin 后仍然有效
3. cookie 存活 7 天，每次使用续期，所以塞在包里的 Kindle 会自己失效
4. 同一个 IP 错五次锁 10 分钟。锁定对 pin 与过期 cookie 同样生效，锁定期内只认完整 token，所以
   轮换过 token 一定能恢复。计数写在 `fails.json`，重启 viewer 不会清掉

权限分 `full` 与 `readonly`。对端地址分不清家和咖啡馆，两种情况都只是"这台笔记本所在的局域网"。
所以权限跟着**宿主**接入的网络走，每个网络用一次 pin 解锁。token 不解锁网络：cookie 的值就是
token，一旦接受它，嗅到 cookie 的人就能把陌生网络提升成 `full`。

| 情形 | 结果 |
|---|---|
| loopback，你在这台电脑上 | `full` |
| 已解锁的网络 | `full` |
| viewer 从未服务过的网络 | `readonly`，只能读，加 停下 与 状态 |
| 从未解锁的子网路由进来的对端 | `readonly` |
| 你点了 只读 | 除 loopback 外全部 `readonly`，直到输入 pin |

`readonly` 意味着没有自由文本、没有 继续/审查/执行、没有会话选择器（那个列表会暴露机器上每个
项目），`?s=` 被忽略，只能读默认会话。停掉失控的 agent 故意保留可用：那是唯一不需要密钥也安全的
动作。只读判定、pin 解锁与每个被代理的动作都写进 `actions.log`。

丢掉 `?s=` 不会让页面乱跳。viewer 把最后渲染的会话记在 `~/.pi/eink/view.json`，任何无法自己指认
会话的请求都按它重渲染，所以只读页面停在原地，而不是追着最后一个心跳跑。这个文件只由服务端写，
客户端不能用它去够网络没被展示过的会话。

只读仍然允许读**当前**对话。在你无法控制的网络上，这是真实暴露：把页面当成"假定旁边的人都能读"，
而不是"假定它是私密的"。

页面是纯 HTTP。在你无法控制的网络上，任何能抓包的人都能偷走 cookie，若那个网络已解锁就拿到
`full`，没解锁就拿到 `readonly`。Kindle 的浏览器太老，握手不了现代 TLS（TLS 1.2+ 配 AEAD 套件），
所以"加 TLS"基本等于"降级到 TLS 1.0/CBC"。咖啡馆场景优先用手机热点，那样暴露面就只剩你自己的
WPA2 网络。到达任何新网络时，不确定就先点 只读。

token 不要留在聊天记录与截图里，页面本身就是泄漏面。轮换只需写新的 `token` 或 `pin`，两者每次
请求都重新读取，不用重启。

---

## 排查

**`/reload` 卡在 "Reloading keybindings, extensions, skills…"** pi 会 await `session_start`，
而 bridge 在那里绑端口。无上限的 `EADDRINUSE` 重试会把整个 reload 阻塞住，且任何地方都没有报错。
重试是有意设上限的，不要拆掉。

**页面是只读，输入框不见了** 那个网络还没解锁。在按钮下方的框里输一次 pin，`trust.json` 会同时
记住宿主子网与对端子网，分包间办公需要这个。点 只读 会故意退回只读；loopback 永远不受影响。

**从来不出现 允许/拒绝 块** 那个块由 `~/.pi/eink/pending.json` 喂，只有 `safety-gate` 会写它。
确认它在 `~/.pi/agent/extensions/safety-gate` 里被加载。装了门禁但没有危险调用待确认时，空块是
正确且静默的状态。`install.sh` 会打印它找到了什么。

**页面自己切到了别的对话** 那是个 bug：没有 `?s=` 时页面每次轮询都重新解析"最新的活会话"，于是
第二个 pi 会话的心跳把整页换掉了。两处修复：首次访问钉住 `?s=`；凡是无法指认会话的时候（只读网络、
没带 `?s=` 的书签）viewer 按 `view.json` 里记的会话重渲染，不再重新推导"最新"。

**某个对话没了，页面也这么说** URL 里的 `?s=` 解析不出来了：会话文件被删、被移走，或被 fork 成
新 id。页面退回当前会话，并说明它丢了哪个 id。

**页面能用但每个按钮都 403** bridge 没匹配上对端。它读的 `allow_ips` 和 viewer 是同一个文件，
先查文件，再看 `~/.pi/eink/bridge.log`。

**某个会话里一个按钮都没有，别的会话正常** 那个会话没有在提供 bridge。`bridge.log` 会说原因，
端口耗尽或绑定被拒。

**Kindle 页面批准了另一个 pi 会话在等的命令** 那就是 `pending.pid` 检查防的跨会话 bug。
`pending.json` 被所有 pi 进程共享，所以 bridge 必须拒绝别的 pid 停住的确认。这个检查永远不要删。

---

## 附注

开发、测试与手动安装：[DEVELOPMENT.md](DEVELOPMENT.md)（只有中文）。版本历史：
[CHANGELOG.md](CHANGELOG.md)，tag 从 `v1.0.0` 起跟它走。

MIT，见 [LICENSE](LICENSE)。
