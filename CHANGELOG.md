# Changelog

All notable changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **The queue is visible and editable from the page.** While the agent is busy, what the box
  sends waits in the bridge's own queue (`~/.pi/eink/queue.json`, survives `/reload`); the
  footer shows `排队中（3）` and `/queue` lists the messages with 上移 / 下移 / 改 / 删, a 加一条
  box and 清空. One message is delivered per settled run, 停下 drops the queue along with
  aborting (the same thing Escape does in the TUI) and says how many went.
- `+ New session` is one entry at the top of the picker and asks **which project**
  (`/new`): the list is the directories that already run pi, deduped, because
  `ctx.newSession()` inherits the process cwd and no extension API can set one. One tap
  then starts a new session in that project's pi process (`POST /act/new` → the extension's `eink:new`
  command) and lands the page on it. Refused while that session is running, so a stray tap
  cannot throw away a turn. The new id is read from `bridges.json` (same pid, new id),
  because the bridge that answers is the instance being replaced.
- **开始投递** on the queue page: a queue restored with a *resumed* session is listed but not
  armed, so a plan queued before a crash cannot execute a week later; queuing something or a
  `/reload` arms it, and 开始投递 is the explicit way out of the paused state.
- Bridge `GET /queue` and `POST /act/{qadd,qedit,qdel,qmove,qclear,qrun}`; `/state` also reports
  the count so the footer costs no extra request.

### Fixed

- **A newer viewer driving an older bridge** (the normal state right after an upgrade:
  the viewer is a service restart, a bridge only by `/reload` inside pi) used to advertise
  a queue the bridge could not serve - `排队中（0）` in the footer, and tapping
  `＋ New session` answered "unknown action". The footer link now appears only when the
  bridge reports the field, the queue page distinguishes "older bridge" from "session not
  running", and a 404 from `new`/`q*` says "run /reload in pi".
- The dollar total is gone from the status line (`↑255k ↓330k 55%`): a number to look up
  on the computer, not something to act on from the Kindle, and the line has to share
  9 px with the footer links.
- The `排队中（N）` footer link no longer hides itself when the agent is idle with an empty
  queue: that is exactly when a reader opens the page to look at it.

- **A horizontal scrollbar that was always there.** Markdown list items render outside
  `.body`, which is where `word-wrap` lives, so a long token in a bullet (a path, a
  `key=value`, a URL) pushed the row 9 CSS px past the viewport. `/diag` now also reports
  `dw` (document scrollWidth) and `who` (the deepest element that overflows, measured after
  the first paint), which is what named the `<li>` on the device.

- **Queue delivery was inconsistent with what the page said, in four ways found on review:**
  `加一条` while idle replied "queued" but sent immediately; `session_start` ignored its
  reason, so a resumed session auto-ran leftovers from days ago; queued text was delivered
  without `expandPromptTemplates`, so a queued `/skill:` reached the model as chat; and an
  empty `qadd` answered 500 instead of 400. Also: a missing timestamp rendered as 08:00, and
  the page had no way to refresh (no meta refresh on purpose - it would wipe the add box),
  and "Removed #5" reported the internal id of the second row instead of its position.
  The 400 ms retry that keeps a swallowed message from stalling the queue is now gated on the
  `agent_start` event instead of on the clock: with a slow run start it used to pipeline two
  queued messages into one run.

### Changed

- A message sent while the agent is busy no longer goes into pi's queue with
  `deliverAs: "followUp"`, and the old "messages are already queued" refusal is gone: the
  box queues several messages and delivers them run by run.
- **Limitation, stated in the README:** pi's own queue cannot be read or edited from an
  extension (`ctx.hasPendingMessages()` is a boolean), so messages typed in the terminal
  during a run are not shown on the queue page. The page says so when pi reports pending
  messages of its own.

### Fixed

- **Tapping a session in the picker showed the running conversation again.** Session ids
  were resolved by guessing from the file name (`id[-8:] in name`), which is wrong for
  every session written into an existing file; the miss was read as "that session is
  gone", and the page fell back to the newest live session.
- **The page jumped to another conversation when a second agent started.** Same root cause
  plus one global "current session" slot: a page that did not name a session re-derived it
  on every poll. Each page now pins the id it resolved into every request it makes, a bare
  `/` always answers `303 /?s=<id>` (the old pin only fired when `view.json` was empty, so
  it never fired in daily use), and buttons are routed by the id the page names.
- The picker is built from session files that actually have a `session` header, so subagent
  `*_transcript.jsonl` artifacts no longer appear as sessions.
- The id→path index is cached for one request instead of four walks per poll (a dead session
  cost 4 directory walks every 1.2 s; a live one still costs none).
- A session created seconds ago counts as live before its `.jsonl` exists, instead of being
  reported as "gone".

## [1.0.0] — 2026-11-01

First tagged release: a Kindle (or any barely-rendering browser) as a second screen
for a running pi session, with five buttons that push actions back into the agent.

### Added

- **Viewer** (`viewer/server.py`) — single-column page for a 511×573 e-ink screen:
  polls `/data` (1.2 s while the agent works, 3 s idle), replaces message nodes in
  place so the screen does not flicker, `/static` fallback without JavaScript,
  day/night themes, session picker.
- **Action bridge** (`extensions/eink-bridge.ts`) — one port per pi session, registered
  in `bridges.json`; `stop` / `continue` / `review` / `execute` / `status`, free text
  and approval answers, all executed through the in-process pi API.
- **Network-scoped capability** — `full` on loopback and on networks unlocked from
  here, `readonly` everywhere else; a network is unlocked once by entering the PIN
  from it.
- **Approval handshake** with [pi-safety-gate](https://github.com/snowspruce/pi-safety-gate)
  over `pending.json` / `answer.json`, both carrying a `pid`, so a page can never
  answer a command parked by another session.
- **Bilingual UI** — Chinese (default) and English, per device via the footer link or
  `/lang?l=`, or service-wide via `EINK_LANG`.
- **One-command install** (`install.sh`) — secrets, trust seeding, pi package
  registration, launchd/systemd unit, and a self-check that also reports whether
  safety-gate was found.
- **Session pinning** — the first visit pins the session id into the URL and
  `view.json` records what was rendered, so the page no longer hops between running
  sessions (including on a read-only network, where `?s=` is deliberately ignored).
- Docs: bilingual README, the safety-gate contract, and `DEVELOPMENT.md` (why the
  code is shaped this way, the traps that cost the most time, and the debugging
  ladder).
- Tests: 43 (bridge) + 188 (viewer), 90.8 % / 91.4 % line coverage, with mutation
  checks confirming the new tests fail when the guarded code is removed.

### Security

- Identity (token/PIN cookie plus UA allowlist) and capability (network) answer
  different questions: a stolen cookie does not by itself promote an unknown network
  to `full`.
- Five wrong guesses from one IP cost a ten-minute lockout, persisted in `fails.json`
  so a restart or `kickstart -k` does not clear it; a correct credential always
  recovers.
- Read-only peers can still read the current conversation and press 停下 — that is
  deliberate, documented, and every action lands in `actions.log`.

### Notes

- The page is plain HTTP: the Kindle browser cannot complete a modern TLS handshake.
  On a network you do not control, prefer a phone hotspot.
- Not verified for this release: a physical Kindle (the suite simulates its user
  agent and viewport) and the systemd branch (developed on macOS).
- `safety-gate` lives in its own repository. Without it everything works except the
  允许/拒绝 buttons, which then fall back to terminal confirmation.
