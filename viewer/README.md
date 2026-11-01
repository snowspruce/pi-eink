# pi eink viewer

E-ink friendly view of a pi session plus an action bridge into the live agent
session. One viewer process, one bridge per running pi session.

## Files

| Path | Role |
|---|---|
| `server.py` | HTTP server on :8000. Renders a `~/.pi/agent/sessions/**/*.jsonl` as an e-ink page, lists sessions, proxies actions. |
| `~/.pi/agent/extensions/eink-bridge.ts` | pi extension. Owns one port per session (base :8010, offset by a hash of the session id, walks forward on collision) and registers itself in `bridges.json`. Injects prompts via `pi.sendUserMessage()`. |
| `bridges.json` | session id -> `{port, pid, cwd, sessionFile, updatedAt}`; heartbeat every 60s, entries older than 10min are ignored. |
| `bridge.json` | the most recently heartbeating bridge; compatibility entry for callers that only need "is any bridge alive". |
| `pending.json` / `answer.json` | safety-gate handshake: a parked risky command and the e-ink page's verdict. |
| `pin` | short code usable in place of the long token (Kindle keyboard); `?k=<pin>`. |
| `uas` / `allow_ips` | one UA substring / one IP or CIDR per line, `#` comments. Files win over the env defaults. |
| `~/Library/LaunchAgents/com.wangke.pi-eink.plist` | launchd unit for `server.py` (KeepAlive). |
| `~/Library/LaunchAgents/com.wangke.caffeinate.plist` | launchd unit running `caffeinate -s`. |

## Auth chain

1. client IP is loopback, listed in `allow_ips`, or in the same /24 as this machine when `EINK_ALLOW_SAME_SUBNET=1`
2. User-Agent contains a `uas` entry (skipped for loopback; `EINK_UA_STRICT=1` applies it there too). Unknown UAs are appended to `ua_seen.log` for review
3. HttpOnly cookie `pieink` == contents of `token`
4. `?k=<token|pin>` seeds that cookie on first visit
5. five wrong token guesses from one IP block it for 10 minutes

The bridge applies the same IP/UA/token checks. Free text (`/act/say`) is refused
from anywhere but loopback: the viewer proxies from 127.0.0.1, so the Kindle
still works while a LAN client cannot reach the agent's bash tool directly.

## Token

`token` holds the secret, `masked_secrets` holds retired ones. Both are re-read on
every request, so rotation needs no restart:

```sh
OLD=$(cat token)
printf '%s\n' "$OLD" >> masked_secrets
python3 -c "import secrets,pathlib,os;p=pathlib.Path('token');p.write_text(secrets.token_urlsafe(24));os.chmod(p,0o600)"
```

`mask_text()` in `server.py` replaces any known token with `abcd…xyz` in rendered
output, because the page is itself a leak surface (tokens end up in chat transcripts).

## Endpoints

Viewer (:8000):

- `GET /` - page shell; `/data` - JSON fragment polled every 1.2s while busy, 3s idle
- `GET /static` - no-JS fallback, 15s meta refresh
- `GET /sessions` - running sessions first, then the 25 most recent session files
- `GET /theme?d=0|1` - day/night cookie (`piedark`)
- `GET /favicon.png`, `/glyphs`, `/diag` - icon, font-coverage check, one-shot geometry beacon
- `POST /act/{stop,continue,review,execute,status,say,approve,deny}[?s=<session id>]` - cookie required, POST only, forwarded to that session's bridge

Bridge (:8010+): `POST /act/...` with `Accept: application/json` returns
`{ok, msg}`; refusals are 4xx so the viewer cannot mistake them for success.

## Rollback

`git log` / `git checkout <rev> -- server.py`, then `launchctl kickstart -k gui/$(id -u)/com.wangke.pi-eink`.
The extension loads via `/reload` in the TUI - edits on disk do nothing until then.
