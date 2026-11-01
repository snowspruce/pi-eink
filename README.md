# pi-eink

Use a Kindle — or any browser that barely renders HTML — as a second screen for a
running [pi](https://pi.dev) session. The page shows the live conversation; five
whitelisted buttons push actions back into the agent.

Built for an e-ink device on the same LAN: one column, no JavaScript required,
1.2 s polling while the agent is busy (3 s when idle).

## How it fits together

| Piece | Lives as | Why not something else |
|---|---|---|
| `extensions/eink-bridge.ts` | a pi extension (loaded inside every pi process) | Sending a prompt needs the in-process API (`pi.sendUserMessage()`, `ctx.abort()`); an outside process cannot do it |
| `viewer/server.py` | its own process (launchd / systemd) | It only reads session `.jsonl` snapshots, so it survives pi restarts and keeps showing the last state |
| `~/.pi/eink/` | runtime state + secrets, **never committed** | `token`, `pin`, `allow_ips`, `uas`, `bridge.json`, `bridges.json`, `pending.json`, `answer.json`, logs |

Each pi process binds **its own** bridge port (base `8010`, offset by a hash of the
session id, walking forward on collision) and registers itself in
`~/.pi/eink/bridges.json`. One viewer therefore serves every running session and
proxies each button to that session's own port.

```
Kindle ──HTTP──▶ viewer :8000 ──proxy──▶ bridge :8010+N ──pi.sendUserMessage()──▶ agent
                      │                        │
                      └──── ~/.pi/eink/*.json ─┘
```

## Requirements

- pi installed and working (`pi --version`)
- the `safety-gate` pi extension loaded in the same pi process: it parks risky
  commands in `~/.pi/eink/pending.json`, the only source of the 允许/拒绝 buttons.
  Without it the page simply never shows a pending command — no error anywhere.
  `install.sh` reports whether it found one
- Python 3 with the standard library only (no `pip install`; tested on 3.14)
- macOS (launchd) or Linux (systemd) for the viewer service — any always-on host
  on the same LAN works, it does not have to be the machine running pi
- a device with a browser: the page is plain HTML + one small inline stylesheet

Reading order if you are new to this: this README for what it does and how to run
it, then [DEVELOPMENT.md](DEVELOPMENT.md) for why it is shaped this way and for the
debugging ladder to use when a device cannot open the page.

## Install

On the machine that will serve the page (it does not have to be the machine
running pi, only something always on and on the same LAN):

```sh
git clone https://github.com/<you>/pi-eink ~/pi-eink
~/pi-eink/install.sh
```

That one command does everything the manual steps below describe:

- creates `~/.pi/eink` (mode 700) with a fresh `token` and `pin`, default `uas`
  and `allow_ips`, and a `trust.json` seeded with the network you are on
- registers the extension with `pi install` (and disables a stray loose copy in
  `~/.pi/agent/extensions/`, which would otherwise load twice)
- installs and starts the service unit — launchd on macOS, `systemd --user` on
  Linux — with the absolute paths and port filled in
- checks that the viewer answers, then prints the two URLs to open once:
  `?k=<token>` on the computer, `?k=<pin>` on the Kindle

Re-running it is safe: an existing `token`/`pin` is never regenerated (that would
log every device out) and an existing `EINK_UA_STRICT` setting is preserved.

| Flag | Meaning |
|---|---|
| `--port N` | viewer port (default `8000`) |
| `--label NAME` | service label (default `com.$(whoami).pi-eink`) |
| `--no-service` | everything except the service unit |
| `--no-pi` | skip `pi install` |
| `--ua-strict` / `--no-ua-strict` | force the loopback UA gate on/off |
| `--uninstall` | stop and remove the unit, unregister the package, keep state |
| `--keep-pi` | with `--uninstall`: leave the pi package registered |

Afterwards: `/reload` in pi so the bridge extension loads, then refresh the Kindle
page.

### Manual install

```sh
mkdir -p ~/.pi/eink
openssl rand -hex 16 > ~/.pi/eink/token   # the long token, usable from scripts
chmod 600 ~/.pi/eink/token
```

Optional but strongly recommended on e-ink, where typing a 32-char token is
painful — a short code that stands in for it:

```sh
printf '482913\n' > ~/.pi/eink/pin && chmod 600 ~/.pi/eink/pin   # 6 digits
```

Then decide **which peers may talk to the service at all**. Without this file only
loopback is allowed, which means your Kindle gets HTTP 403:

```sh
cat > ~/.pi/eink/allow_ips <<'EOF'
# One IP or CIDR per line. "#" starts a comment. Loopback is always allowed.
127.0.0.1
::1
192.168.0.0/16
EOF
```

Both the viewer and the bridge read this same file, so they always agree on who
may connect (`EINK_ALLOW_IPS` is only the fallback when the file is absent).

Then decide **where you may type**: write `~/.pi/eink/trust.json` with the network
you are on, otherwise the first visit is read-only until you enter the pin:

```json
{ "confirmed": ["192.168.1.0/24"], "peers": ["192.168.1.0/24"], "readonly": false }
```

`install.sh` seeds this for you from the current network.

#### Install the pi extension

Recommended — let pi manage it from this repository (a local source is loaded in
place, nothing is copied, so `git pull` is the update mechanism):

```sh
pi install /path/to/pi-eink     # writes the entry into ~/.pi/agent/settings.json
pi list                         # should list /path/to/pi-eink and its resolved path
```

`pi install` records the path **relative to the settings file**
(`../../Documents/...` style); that is expected. Do not also keep a copy of
`eink-bridge.ts` in `~/.pi/agent/extensions/` — both load, and one session would
then bind two bridge ports and answer the same button twice.

**Local directory or `git:` source?** Both work for the extension, but only a
local clone can also host the viewer service: `git:github.com/you/pi-eink` is
cloned into `~/.pi/agent/git/...`, a tree pi owns and rewrites on update
(`git reset --hard`, `git clean -fdx`, path keyed by ref). Pointing launchd or
systemd there means the unit breaks on the next `pi update --extensions`. Use one
clone you control, and let `git pull` + a service restart be the update path.

To confirm the extension is live: start pi, then look at
`~/.pi/eink/bridges.json` — a running session should appear there with its own
port. If it does not, `~/.pi/eink/bridge.log` says why.

#### Run the viewer

Try it in the foreground first:

```sh
EINK_PORT=8000 python3 /path/to/pi-eink/viewer/server.py
```

Open `http://<host>:8000/?k=$(cat ~/.pi/eink/token)` once — that seeds the
`pieink` cookie, after which plain `http://<host>:8000/` works. On the Kindle you
can use `?k=<pin>` instead of the long token.

Keep it running with launchd (macOS): copy `service/com.example.pi-eink.plist.example`
to `~/Library/LaunchAgents/`, replace every `/path/to/pi-eink` and the python path,
then:

```sh
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.$(whoami).pi-eink.plist
```

On Linux there is `service/pi-eink.service.example` (`systemctl --user enable --now
pi-eink`). Either way: `git pull` does **not** restart the viewer — the unit keeps a
running process alive, it does not reload code. After an update:

```sh
launchctl kickstart -k "gui/$(id -u)/com.$(whoami).pi-eink"   # macOS, as install.sh labels it
systemctl --user restart pi-eink                          # Linux
```

E-ink note: if the host sleeps, the page goes dark. `caffeinate -s` in a second
launchd unit keeps the machine awake while it is on AC power.

## Develop

```sh
npm test             # extension: stubbed pi API, temp HOME, real HTTP handler
npm run test:viewer  # viewer: unit + HTTP-level enforcement (56 tests, ~1.2s)
```

The tests bind a fixed port (`18019`) so they never collide with a real bridge,
and never touch `~/.pi/eink`. They cover the approval handshake (including the
409 on a second tap and the cross-session pid refusal), `/act/say` and its command
mapping, and `ipAllowed()` against the `allow_ips` file. The viewer tests drive
`caps_for()` directly: an unlocked network, an unknown one, a peer routed in from
another subnet, and the 只读 switch.

## Configuration

Environment. Viewer variables go in the service unit; bridge variables must be in
the environment that starts `pi` (the bridge is an in-process extension and never
sees the unit's environment):

| Variable | Process | Default | Meaning |
|---|---|---|---|
| `EINK_PORT` | both | `8000` | viewer port; the bridge also uses it to redirect its `/` |
| `EINK_BIND` | both | `0.0.0.0` | interface to listen on; set the LAN address on a box that also has VPN/docker interfaces |
| `EINK_BRIDGE_PORT` | both | unset | force one bridge port instead of the per-session hash |
| `EINK_ALLOW_IPS` | both | unset | fallback peer allowlist when `allow_ips` is missing (comma separated, IP or CIDR) |
| `EINK_ALLOW_UA` | both | `Kindle Build/` | fallback User-Agent allowlist |
| `EINK_UA_STRICT` | viewer | off | `1` also enforces the UA check for loopback |
| `EINK_VIEWER_IPS` | bridge | unset | comma-separated viewer hosts allowed to proxy free text/approvals when the viewer runs on another machine; empty = loopback only |
| `EINK_ALLOW_SAME_SUBNET` | both | on | peers in the host's own /24 are allowed; `0` disables (then only `allow_ips` counts). Both the viewer and the bridge read it, with the same default |
| `EINK_LOG_UA` | viewer | off | `1` logs each (peer, User-Agent) pair once to `server.log`; `ua_seen.log` is written only for UAs the gate rejected |
| `PI_SESSION_FILE` | viewer | unset | last-resort fallback: only used when no live bridge and no `bridge.json` name a session |
| `EINK_TRUST_HOURS` | viewer | `0` | `N` expires a network confirmation after N hours (0 = until the network changes) |

State files under `~/.pi/eink/` (all ignored by git):

| File | Written by | Purpose |
|---|---|---|
| `token`, `pin`, `masked_secrets` | you | credentials (retired tokens are masked so they never leak into page output) |
| `allow_ips`, `uas` | you | peer / browser allowlists |
| `trust.json` | viewer | which networks may type (`confirmed` = host subnets, `peers` = peer subnets, `readonly` = global switch); written when a network is unlocked with the pin |
| `fails.json` | viewer | failed-guess counters, so a restart or `kickstart -k` does not clear a lockout |
| `install-meta` | `install.sh` | label/port/os of the installed unit, read back by `--uninstall` |
| `actions.log` | viewer | one line per action: time, peer IP, capability, action, result |
| `bridge.json` | bridge | most recent heartbeat — "is any bridge alive" |
| `bridges.json` | bridge | session id → `{port, pid, cwd, sessionFile, updatedAt}`, heartbeat 60 s, entries older than 10 min ignored |
| `pending.json`, `answer.json` | safety-gate ↔ bridge | a parked risky command and the page's verdict |
| `bridge.log` | bridge | why a session is not serving the page (busy port, etc.) |
| `diag.log`, `ua_seen.log` | viewer | page beacons and rejected UAs; both rotate to `.1` at 256 KB |
| `server.log` | service unit | stdout/stderr (POST lines, errors, tracebacks); not rotated by the code — `: > ~/.pi/eink/server.log` when it grows |

## Endpoints

Viewer (`:8000`): `/` page shell · `/data` JSON fragment · `/static` no-JS fallback
(a form POST from a browser without JS lands here, since `/` needs script to fill
the message list) ·
`/sessions` picker · `/theme?d=0|1` day/night cookie · `/diag` · `/glyphs` ·
`/favicon.png` · `/done/<key>` (relayed from the bridge's confirmation page) ·
`POST /act/<action>` proxied to the session's bridge, plus two
viewer-local ones: `/act/unlock` (enter the pin to unlock this network) and
`/act/readonly` (drop back to read-only).

Bridge (`:8010`+, or `EINK_BRIDGE_PORT`): `GET /`, `/state`,
`POST /act/{stop,continue,review,execute,status,say,approve,deny}`. Only `stop` and
`status` are accepted from a non-loopback peer: everything else — free text,
approvals, prompts — must come through the viewer's proxy.

## Security model

Two different questions, answered separately: **who are you** (the cookie) and
**what may you do here** (the network).

**Identity**

1. User-Agent matches a `uas` entry — spoofable, so it is only a speed bump
2. HttpOnly cookie `pieink` equals `token`; `?k=<token|pin>` seeds it
3. The cookie lives 7 days and is re-issued on use, so a Kindle left in a bag
   stops working on its own
4. five wrong guesses from one IP block it for 10 minutes; the counters live in
   `fails.json`, so restarting the viewer does not clear them

**Capability** (`full` or `readonly`)

The peer address cannot tell home from a café — in both cases it is just "the LAN
this laptop is on". So the capability follows the network the *host* is attached
to, and each network is unlocked once by entering the **pin** from it. The token
does not unlock a network: the cookie's value is the token, so accepting it here
would let anyone who sniffed a cookie promote an unknown network to `full`.

| Situation | Result |
|---|---|
| loopback (you, on the computer) | `full` |
| a network already unlocked | `full` |
| a network the viewer has never served on | `readonly`: read + 停下/状态 only |
| peer routed in from a subnet you never unlocked | `readonly` |
| you tapped 只读 | `readonly` everywhere except loopback, until the pin is entered |

`readonly` means: no free text, no 继续/审查/执行, no session picker (that list
exposes every project on the machine) and `?s=` is ignored, so only the default
session can be read. Stopping a runaway agent is kept available on purpose — it is
the one action that is safe without a secret. `readonly` decisions, pin unlocks and
every proxied action land in `actions.log`.

What read-only still allows is reading the *current* conversation. On a network you
do not control, that is a real exposure: treat the page as "assume the person next
to you can read it" rather than "assume it is private".

**What this does not fix**

The page is plain HTTP. On a network you do not control, anyone who can capture
traffic can steal the cookie and get `full` if that network was unlocked, or
`readonly` if it was not. The Kindle's browser is too old for a modern TLS
handshake (TLS 1.2+ with AEAD suites), so "add TLS" mostly means "downgrade to
TLS 1.0/CBC" — for café use, prefer a phone hotspot, which makes the whole
exposure your own WPA2 network. Arriving on any new network, tap 只读 before you
do anything else if you are unsure.

Keep the token out of chat transcripts and screenshots — the page itself is a leak
surface. Rotate by writing a new `token`/`pin`; both are re-read on every request,
so no restart is needed.

## Troubleshooting

**`/reload` hangs on "Reloading keybindings, extensions, skills…"** — pi *awaits*
`session_start`, and the bridge binds a port there. An unbounded `EADDRINUSE`
retry blocks the whole reload with no error anywhere. The retry is bounded on
purpose: keep it that way.

**The page is read-only and the text box is gone** — that network has not been
unlocked. Enter the pin in the box under the buttons once (`trust.json` remembers
both the host subnet and the peer subnet, which is what split-subnet offices need).
Tapping 只读 puts it back to read-only on purpose; loopback is never affected.

**No 允许/拒绝 block ever appears** — that block is fed by `~/.pi/eink/pending.json`,
which only the `safety-gate` extension writes. Check that `safety-gate` is loaded
(`~/.pi/agent/extensions/safety-gate`); with a gate loaded but no risky call pending,
an empty block is the correct, silent state. `install.sh` prints what it found.

**The page works but every button is 403** — the bridge could not match the peer.
It reads the same `allow_ips` as the viewer; check the file, then look at
`~/.pi/eink/bridge.log`.

**No buttons at all in one session, fine in another** — that session is not
serving a bridge. `bridge.log` says why (port exhausted, bind refused).

**The Kindle page approved a command the other pi session was waiting on** — that
is the cross-session bug the `pending.pid` check prevents: `pending.json` is
shared by every pi process, so a bridge must refuse a confirm parked by another
pid. Never remove that check.

## Uninstall

```sh
./install.sh --uninstall            # stop and remove the unit, unregister the package
./install.sh --uninstall --keep-pi  # ... but leave the package registered with pi
```

It reads the label and port recorded in `~/.pi/eink/install-meta`, so a custom
`--label` needs no repetition, and it puts back a loose `eink-bridge.ts` that the
installer had moved aside. State is kept; `rm -rf ~/.pi/eink` deletes the token and
the history config. Nothing else to revoke — it never left the machine.
