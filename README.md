# pi-eink

A Kindle (or any bare-bones browser on the LAN) as a second surface for a
running [pi](https://pi.dev) session: read the conversation, and push a small
fixed set of actions back into the live agent.

## Three pieces, three lifetimes

| Piece | Lives as | Why it cannot be anything else |
|---|---|---|
| `extensions/eink-bridge.ts` | pi extension (loaded inside every pi process) | Injecting a prompt needs the in-process API (`pi.sendUserMessage()`, `ctx.abort()`); an outside process cannot do it |
| `viewer/server.py` | its own process under launchd (KeepAlive) | It only reads session `.jsonl` snapshots, so it survives pi restarts and shows the last known state |
| `~/.pi/eink/` | runtime state + secrets, never in this repo | `token`, `pin`, `allow_ips`, `uas`, `bridge.json`, `bridges.json`, `pending.json`, `answer.json`, logs |

## Layout

```
pi-eink/
├── package.json                  # pi package: extensions/ is discovered automatically
├── extensions/eink-bridge.ts     # HTTP endpoint per session, injects whitelisted actions
└── viewer/
    ├── server.py                 # :8000 page, session list, action proxy
    └── README.md                 # viewer detail: auth chain, endpoints, token rotation
```

## How the two halves find each other

Each pi process binds its own bridge port (`BASE_PORT` 8010 plus an offset
derived from the session id, walking forward on collision) and registers:

```
bridges.json   sessionId -> { port, pid, cwd, sessionFile, updatedAt }   # heartbeat 60s
bridge.json    the most recent heartbeat, for callers that only ask "is any bridge alive"
```

The viewer reads `bridges.json`, so one viewer serves every running session; it
proxies actions to that session's own port.

## Invariants that were bugs once — do not regress them

1. **`session_start` must always finish.** pi awaits it during startup *and*
   during `/reload`. An unbounded `EADDRINUSE` retry there hangs the whole
   reload behind "Reloading keybindings, extensions, skills, ..." with no error
   in any log. Bound it, and resolve `false` instead of throwing.
2. **`pending.json` / `answer.json` are shared by every pi process.** The bridge
   must refuse a confirm parked by another pid, otherwise the Kindle page of
   session A approves a dangerous command from session B.
3. **A busy port must never silently look like success.** Log the reason to
   `~/.pi/eink/bridge.log` and notify the TUI, so "no Kindle buttons" is
   explainable.

## Install

Today this repository is a copy; `~/.pi` still loads the loose files. Wiring it
as a package (step 2 of the migration) is:

```json
// ~/.pi/agent/settings.json
"packages": ["npm:pi-subagents", "npm:pi-web-access", "file:/Users/wangke/Documents/Program/pi-eink"]
```

Pi loads a local path source without copying, so git stays the only truth.
`pi update` only replaces the pi package itself; `pi update --extensions` only
reconciles configured packages — neither rewrites this repository. What *can*
break it is ExtensionAPI drift, so after upgrading pi, check that the extension
still loads and that the bounded-bind path still returns.

## Not in this repo

`safety-gate` (the dangerous-command gate that parks a risky command and waits
for the e-ink verdict) stays with the other pi extensions: it is a generic
guard, coupled to this bridge only through the `pending.json` handshake.
