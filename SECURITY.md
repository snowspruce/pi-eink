# 安全说明（SECURITY.md）

> 本文件描述**这套东西被放到公网之后**的安全模型。默认部署（局域网内 Kindle 看本机 viewer）
> 的风险远低于此，那一档只依赖 `token` 与 UA 白名单。
>
> 所有"实测"条目都是在本仓库的参考部署上跑出来的，命令见「自查」一节。

---

## 一、部署形态与信任边界

```
Kindle ──TLS──► 中转机（境外 VPS）
                  nginx stream：只按 SNI 转发**密文**，不解密、不落盘、无证书
                        │  ssh 反向隧道（Mac 主动连出，VPS 不开放任何业务入站口）
                        ▼
                 Mac 127.0.0.1:8443  nginx：TLS 终止 + Basic Auth
                        ▼
                 Mac 127.0.0.1:8000  viewer —— 能驱动一个有 shell 权限的 coding agent
```

三条必须理解的性质：

1. **中转机看不到内容。** TLS 在 Mac 上终止，私钥不离开这台电脑；中转机只看到密文与 SNI。
2. **Mac 不对外暴露端口。** 所有监听都在 `127.0.0.1`（viewer 需要局域网访问时才绑 `0.0.0.0`）。
3. **最终能力是"驱动 agent"，等价于本机 shell 权限。** 所以任何一层的目标都不是"防止看到页面"，
   而是"防止别人替我按下发送键"。

由此推出：**谁能通过认证，谁就拥有这台电脑的 agent。** 下面每一层都在服务这个结论。

---

## 二、防线与实测判据

| 层 | 机制 | 实测判据（示例值） |
|---|---|---|
| 入口过滤 | nginx stream 按 SNI 白名单转发，其余丢到死端口 | 错 SNI / 无 SNI / 直连 IP **全部拒绝**；`nc -z` 直连 443 后立即被断开 |
| 传输 | TLS 1.2 / 1.3，ECDSA P-256 证书 | `openssl s_client -tls1`、`-tls1_1` **协商失败**；正常握手为 TLSv1.3 |
| 第一因子 | HTTP Basic Auth（12 位随机密码） | 无凭证 401；`//`、`/./`、`/%2e/`、`/..;/`、`/%2F` 全部 401；重复 `Authorization` 头 → 400 |
| 第二因子 | TOTP（6 位，RFC 6238，±1 周期） | 密码通过后只出动态码页；错码 403；会话 cookie 是随机密钥，**不是 token** |
| 应用凭证 | 32 字节 token，由 Mac 侧 nginx 注入 `X-Eink-Token` | URL/书签里**不含任何秘密**；`?k=` 仅在局域网路径使用 |
| 会话 | HttpOnly cookie，7 天滚动续期；TOTP 会话持久化到磁盘 | viewer 重启后已登录设备不掉线（已验证） |
| 失败处理 | 5 次失败锁 10 分钟（按 IP），`fails.json` 跨重启 | 锁定期内只接受完整 token（换 token 可立即自救） |
| 中转机 | ufw 仅放行 22/80/443/隧道端口；公网只听 22/80/443；隧道落点绑 `127.0.0.1` | `ss -tln` 仅上述端口；9080/9443 为回环 |
| SSH 认证 | 仅密钥、禁密码、禁 root、禁空密码、`AllowUsers`、`MaxAuthTries 3`、`LoginGraceTime 20` | `ssh -o PreferredAuthentications=password` → `Permission denied (publickey)`；任意用户名响应一致（无枚举） |
| SSH 授权 | `GatewayPorts no` + `PermitListen 127.0.0.1:<两个端口>`；`MaxStartups 5:30:20` | 客户端乱绑 `-R 127.0.0.1:9999` → `remote port forwarding failed` |
| 账号隔离 | 观测类客户端用独立低权账号 + 受限 key | 该 key 带 `no-port-forwarding,no-agent-forwarding,no-x11-forwarding`，账号无 sudo |
| 主机加固 | 自动安全更新、内核参数（kptr/dmesg/ptrace/protected_*）、auditd、AppArmor、journald 持久化 | 见「自查」脚本 |
| 日志 | nginx/中转访问日志**不记录查询串** | `log_format` 只用 `$uri`，避免 `?k=` 落盘 |

---

## 三、已知取舍（不是漏洞，是选择）

1. **Basic Auth 层没有失败锁定**，只有 `limit_req`（10 r/s）。12 位以上随机密码在 10 r/s 下不可爆破；
   **若把它改成短口令，这一层会立刻变弱**。
2. **设备丢失 = 短期可用**：Kindle 浏览器缓存了 Basic Auth 与 7 天 TOTP 会话。
   处置：删除 `~/.pi/eink/otp_sessions.json` + 轮换 Basic Auth 密码，即可让丢失设备立即失效。
3. **Mac 是单点**：睡眠/断网/中转发故障都会中断远程访问（局域网不受影响）。launchd 负责自动重连。
4. **viewer 需要局域网访问时绑 `0.0.0.0`**，因此本机任意进程/本机 docker 容器可直连 8000
   （绕过 Basic Auth，但仍需 token + TOTP）。这是本地信任边界，收紧收益很低。
5. **部署账号拥有 `NOPASSWD: ALL`**（自动化依赖它）。风险由"谁能登该账号"控制，
   因此**管理密钥只有一把，且不进任何第三方**。

---

## 四、变更原则（这两条是踩出来的）

1. **动入口之前先确保有第二条验证通道。** 本仓库历史上两次自锁都源于此：
   - `fail2ban` 阈值收到 `maxretry=3` 且未加 `ignoreip`，把我们自己的出口 IP 封了 1 小时；
     封禁动作还会掐断已建立的隧道。
   - 给 SSH 加算法白名单（`HostKeyAlgorithms` 未包含机器实际 host key 类型），
     sshd 连自身握手都无法完成，**重启也无效**（配置问题），只能靠控制台修复。
2. **改完必须用外部视角复测，而不是"看起来没问题"。** 每次收紧后应跑一遍「自查」，
   并至少验证一次"错误凭证被拒 / 正确凭证可用"两个方向。

---

## 五、自查（可在本机一次跑完）

```sh
H=eink.example.com          # 你的域名
V=203.0.113.10              # 中转机 IP
BASIC='user:pass'           # Basic Auth 凭证

# 1) 入口过滤：错 SNI / 无 SNI 必须被拒
openssl s_client -servername wrong.example.com -connect $V:443 </dev/null 2>&1 | grep -qi 'handshake failure\|alert' && echo "SNI 白名单 OK"
openssl s_client -noservername -connect $V:443 </dev/null 2>&1 | grep -qi 'handshake failure\|alert' && echo "无 SNI 被拒 OK"

# 2) TLS 下限：1.0/1.1 必须失败
for v in tls1 tls1_1; do openssl s_client -$v -servername $H -connect $H:443 </dev/null 2>&1 | grep -q 'Protocol *: TLS' && echo "警告：$v 仍可用" || echo "$v 已拒绝"; done

# 3) 认证两向
curl -s -o /dev/null -w '匿名 %{http_code}\n' https://$H/                    # 期望 401
curl -sk -o /dev/null -u "$BASIC" -w '已认证 %{http_code}\n' https://$H/     # 期望 200（动态码页）

# 4) 路径规范化绕过
for p in // /./ /%2e/ /..\;/ /%2F; do curl -s -o /dev/null -w "$p %{http_code}\n" "https://$H$p"; done   # 期望全 401

# 5) 中转机端口与 SSH 策略
ssh -p 2222 <user>@$V 'ss -tln | awk "{print \$4}" | sort -u; sshd -T | grep -E "^(passwordauthentication|permitrootlogin|allowusers|permitlisten|maxstartups)"'
```

---

## 六、报告安全问题

私密渠道联系维护者（仓库 owner）。请勿在公开 issue 中提交可直接利用的细节。
