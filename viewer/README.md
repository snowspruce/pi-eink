# pi eink viewer

E-ink friendly read-only view of the newest pi session, plus a 5-action bridge
into the live agent session.

## Files

| Path | Role |
|---|---|
| `server.py` | HTTP server on :8000. Renders the newest `~/.pi/agent/sessions/**/*.jsonl` as an e-ink page. Serves the action buttons. |
| `~/.pi/agent/extensions/eink-bridge.ts` | pi extension. Owns :8001, injects the whitelisted prompts via `pi.sendUserMessage()`. |
| `~/Library/LaunchAgents/com.wangke.pi-eink.plist` | launchd unit for `server.py` (KeepAlive). |
| `~/Library/LaunchAgents/com.wangke.caffeinate.plist` | launchd unit running `caffeinate -s`. |

## Auth chain (all four must pass)

1. client IP in `EINK_ALLOW_IPS` (default `192.168.3.25,192.168.3.45,127.0.0.1,::1`)
2. User-Agent contains `Kindle Build/` (`EINK_ALLOW_UA`, spoofable - a speed bump only)
3. HttpOnly cookie `pieink` == contents of `token`
4. `?k=<token>` seeds that cookie on first visit

Loopback is exempt from the UA check.

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

- `GET /` and `/data` - viewer (JSON fragment polled by the page)
- `GET /static` - no-JS fallback, 15s meta refresh
- `POST :8001/act/{stop,continue,review,execute,status}` - cookie required, POST only

## Rollback

`git log` / `git checkout <rev> -- server.py`, then `launchctl kickstart -k gui/$(id -u)/com.wangke.pi-eink`.
The extension loads via `/reload` in the TUI.
