# pi-eink

Use a Kindle — or any browser that barely renders HTML — as a second screen for a
running [pi](https://pi.dev) session. The page shows the live conversation; five
whitelisted buttons push actions back into the agent.

Built for an e-ink device on the same LAN: one column, no JavaScript required,
2 s polling while the agent is busy.

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
- Python 3 with the standard library only (no `pip install`; tested on 3.14)
- macOS (launchd) or Linux (systemd) for the viewer service — any always-on host
  on the same LAN works, it does not have to be the machine running pi
- a device with a browser: the page is plain HTML + one small inline stylesheet

## Install

### 1. State directory and secrets

```sh
mkdir -p ~/.pi/eink
openssl rand -hex 16 > ~/.pi/eink/token   # the long token, usable from scripts
chmod 600 ~/.pi/eink/token
```

Optional but strongly recommended on e-ink, where typing a 32-char token is
painful — a short code that stands in for it:

```sh
printf '4821\n' > ~/.pi/eink/pin && chmod 600 ~/.pi/eink/pin
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

### 2. Install the pi extension

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

To confirm the extension is live: start pi, then look at
`~/.pi/eink/bridges.json` — a running session should appear there with its own
port. If it does not, `~/.pi/eink/bridge.log` says why.

### 3. Run the viewer

Try it in the foreground first:

```sh
EINK_PORT=8000 python3 /path/to/pi-eink/viewer/server.py
```

Open `http://<host>:8000/?k=$(cat ~/.pi/eink/token)` once — that seeds the
`pieink` cookie, after which plain `http://<host>:8000/` works. On the Kindle you
can use `?k=<pin>` instead of the long token.

Keep it running with launchd (macOS). Save as
`~/Library/LaunchAgents/com.example.pi-eink.plist` — replace `/path/to` and the
python path — then `launchctl load -w` it:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key>              <string>com.example.pi-eink</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/path/to/pi-eink/viewer/server.py</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>EINK_PORT</key><string>8000</string>
  </dict>
  <key>RunAtLoad</key>          <true/>
  <key>KeepAlive</key>          <true/>
  <key>ProcessType</key>        <string>Background</string>
  <key>StandardOutPath</key>    <string>/tmp/pi-eink.log</string>
  <key>StandardErrorPath</key>  <string>/tmp/pi-eink.log</string>
</dict></plist>
```

On Linux, the equivalent is a `systemd --user` unit with `ExecStart=` and
`Restart=always`.

E-ink note: if the host sleeps, the page goes dark. `caffeinate -s` in a second
launchd unit keeps the machine awake while it is on AC power.

## Configuration

Environment (set them in the service unit; both processes read them):

| Variable | Default | Meaning |
|---|---|---|
| `EINK_PORT` | `8000` | viewer port |
| `EINK_BRIDGE_PORT` | unset | force one bridge port instead of the per-session hash |
| `EINK_ALLOW_IPS` | unset | fallback peer allowlist when `allow_ips` is missing (comma separated, IP or CIDR) |
| `EINK_ALLOW_UA` | `Kindle Build/` | fallback User-Agent allowlist |
| `EINK_UA_STRICT` | off | `1` also enforces the UA check for loopback |
| `EINK_ALLOW_SAME_SUBNET` | off | `1` also allows any peer in the host's own /24 |
| `EINK_LOG_UA` | off | `1` logs every User-Agent to `ua_seen.log` |
| `PI_SESSION_FILE` | unset | pin the page to one session file instead of following live bridges |

State files under `~/.pi/eink/` (all ignored by git):

| File | Written by | Purpose |
|---|---|---|
| `token`, `pin`, `masked_secrets` | you | credentials (retired tokens are masked so they never leak into page output) |
| `allow_ips`, `uas` | you | peer / browser allowlists |
| `bridge.json` | bridge | most recent heartbeat — "is any bridge alive" |
| `bridges.json` | bridge | session id → `{port, pid, cwd, sessionFile, updatedAt}`, heartbeat 60 s, entries older than 10 min ignored |
| `pending.json`, `answer.json` | safety-gate ↔ bridge | a parked risky command and the page's verdict |
| `bridge.log` | bridge | why a session is not serving the page (busy port, etc.) |
| `server.log`, `diag.log`, `ua_seen.log` | viewer | service log, one-shot geometry beacon, unknown User-Agents |

## Endpoints

Viewer (`:8000`): `/` page shell · `/data` JSON fragment · `/static` no-JS fallback ·
`/sessions` picker · `/theme?d=0|1` day/night cookie · `/diag` · `/glyphs` ·
`/favicon.png` · `/act/<action>` proxied to the session's bridge.

Bridge (`:8010`+, or `EINK_BRIDGE_PORT`): `GET /`, `/state`,
`POST /act/{stop,continue,review,execute,status,say,approve,deny}`. Free text
(`say`) is accepted from loopback only, so it works through the viewer's proxy but
not directly from the LAN.

## Security model

Four gates, all must pass:

1. peer IP is loopback, in `allow_ips`, or (opt-in) in the host's /24
2. User-Agent matches a `uas` entry — spoofable, so it is only a speed bump
3. HttpOnly cookie `pieink` equals `token` (or `pin` seeds it via `?k=`)
4. five wrong guesses from one IP block it for 10 minutes

There is no free-text channel reachable from the LAN on purpose: the agent has
shell access, so a remote free-text endpoint would be remote code execution.
Actions are a fixed list, which is what makes putting this on a LAN defensible at
all. Keep the token out of chat transcripts and screenshots — the page itself is a
leak surface.

Rotate by writing a new `token`/`pin`; both files are re-read on every request, so
no restart is needed.

## Troubleshooting

**`/reload` hangs on "Reloading keybindings, extensions, skills…"** — pi *awaits*
`session_start`, and the bridge binds a port there. An unbounded `EADDRINUSE`
retry blocks the whole reload with no error anywhere. The retry is bounded on
purpose: keep it that way.

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

- remove the `file:` entry from `~/.pi/agent/settings.json` (or delete the copied
  `eink-bridge.ts`)
- `launchctl unload ~/Library/LaunchAgents/com.example.pi-eink.plist` and delete it
- `rm -rf ~/.pi/eink` (this deletes the token — revoke nothing else, it never left
  the machine)
