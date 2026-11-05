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
- `+ New session` opens a **new terminal window** with a fresh pi in the chosen directory
  instead of hijacking the session being read (which is what `ctx.newSession()` /
  `switchSession` did). The outcome note no longer sticks above the new page's input box,
  and the two in-process commands (`eink:new`, `eink:new-in`) are gone.
- The black band at the top is the browser's `fullscreen` preference, not `quick_controls`:
  with only quick-controls the titlebar collapses to zero height while the WebView keeps a
  stale 89px offset. `eink-addr off` now sets both, and `~/bin/eink-heal` (launchd, every
  2 min) relaunches the browser if the band ever comes back.
- `+ New session` is one entry at the top of the picker and asks **which directory**
  (`/new`): the sub-directories of `EINK_PROJECT_ROOTS` (default `~/Documents/Program`)
  plus anywhere pi already runs, recently-touched first, capped with the count of what is
  not shown. Directories outside the roots are refused by the bridge.
- **开始投递** on the queue page: a queue restored with a *resumed* session is listed but not
  armed, so a plan queued before a crash cannot execute a week later; queuing something or a
  `/reload` arms it, and 开始投递 is the explicit way out of the paused state.
- Bridge `GET /queue` and `POST /act/{qadd,qedit,qdel,qmove,qclear,qrun}`; `/state` also reports
  the count so the footer costs no extra request.

### Security

- `~/bin/eink-audit`: asserts the whole posture in one command (token file, viewer bind, **every
  bridge's bind**, the public entry rejecting credentials-free requests, nginx not injecting or
  hardcoding the token, `/whois` carrying its HMAC proof, the kiosk being in front). Runs daily
  under launchd and only writes to `~/.pi/eink/audit.log` when something is wrong.

- `~/bin/eink-rotate-token`: one command to rotate the token, invalidate the server-side OTP
  sessions, restart the viewer, wipe the device's copy and bring the kiosk back up - then
  verify the viewer answers and the public entry accepts the new token. nginx no longer
  holds the token at all, so there is nothing else to edit.

- The public entry no longer has nginx inject the viewer token into every request: that
  made "anyone who can reach the VPS" already authenticated, with Basic as the only real
  gate (`curl https://eink.blueberry.cafe/` returned 200 the moment Basic was off). nginx
  now forwards the *client's* token and the viewer decides; Basic is off because the
  Kindle cannot answer it.
- The kiosk app stops accepting any certificate (Chromium 30 rejects the current
  Let's Encrypt chain; unconditionally proceeding let anyone on the path harvest the token
  and the OTP cookie) - only a Let's Encrypt-issued certificate for the expected host is
  allowed through.
- LAN discovery now has to *prove* it knows the token (`/whois?n=<nonce>` →
  HMAC-SHA256(token, nonce)); before, any host on the same subnet could answer and receive
  the token, which the app appends to whatever address it discovers.

- The OTP form was asked for again per origin: the LAN address and the public address are
  different cookie jars, while the server keeps a single 7-day OTP session. The app now
  remembers the value it sees and plants it on whichever origin it loads next, so the code
  is entered once - including after pressing ⋮.

- **Remote access works again from the kiosk app**, which is what the black-band fix had
  quietly broken: the app had no `onReceivedSslError` (the stock browser shows a "continue"
  warning, a bare WebView cancels silently, so the request never left the device), no Basic
  handler, and it dropped the token when a URL arrived through an intent. The public entry
  now accepts either the token (`?k=`) or the `pieink` cookie instead of Basic, `/otp` is
  exempt from Basic (its POST carries neither), and the viewer hands out the token cookie
  when the token arrives in the `X-Eink-Token` header.
- Address discovery no longer needs anything on the router: broadcast, then a scan of the
  local /24 for the viewer's `GET /whois`, with the token remembered separately so a change
  of network only changes the address.

- **The Kindle now runs a 12 KB WebView shell instead of the stock browser** (`device/kiosk`).
  The black band and the input box hiding behind the keyboard were both the stock AOSP
  Browser's own UI: it keeps a stale 89px content offset after collapsing its toolbar, and a
  fullscreen window is not resized for the IME. Measured on the device: fullscreen kept the
  WebView at 1356px with the keyboard shown; non-fullscreen + `adjustResize` shrinks it to
  621px and the page scrolls its box up.
- `?k=<token>` could never authenticate on a network that needs the OTP form: the OTP page
  dropped the `Set-Cookie` it was given, so the token arrived on the one request that was
  answered by that page and nowhere after it.

- A poll that changes nothing no longer ships the transcript: the page sends the digest
  it already has, and `/data` answers `{"same":true,…}` (88 bytes instead of ~20 KB -
  measured 99.6% less traffic on the live viewer).

- **Attribute injection through a directory name.** `esc()` escaped `& < >` but not
  quotes, and `/new` puts a directory into `value="..."` (the OTP page puts `?s=` into one
  too): a folder called `x" onmouseover="alert(1)` injected a live attribute. `esc()` now
  escapes quotes as well - `&quot;` still renders as `"` in text nodes.
- Starting a session in another directory left an **empty session file** behind when a
  `session_before_switch` handler cancelled it (or the switch threw): it is now removed
  again, instead of showing up in every session list as a blank conversation.
- `/act/new` while messages are still queued silently stranded them in the queue file of
  the session being left; it now refuses with the count (clear or deliver first).

- **A newer viewer driving an older bridge** (the normal state right after an upgrade:
  the viewer is a service restart, a bridge only by `/reload` inside pi) used to advertise
  a queue the bridge could not serve - `排队中（0）` in the footer, and tapping
  `＋ New session` answered "unknown action". The footer link now appears only when the
  bridge reports the field, the queue page distinguishes "older bridge" from "session not
  running", and a 404 from `new`/`q*` says "run /reload in pi".
- The dollar total is gone from the status line (`↑255k ↓330k 55%`): a number to look up
  on the computer, not something to act on from the Kindle, and the line has to share
  9 px with the footer links.
- `/new` (the project picker for `+ New session`) was readable from a read-only network.
  It lists the same directories `/sessions` is refused for, so it now gets the same 403;
  and a project whose bridge predates the feature shows *why* instead of a button that
  can only answer "unknown action" - probing the sessions in that directory newest first
  until one of them has the feature, so a stale newest sibling cannot hide a reloaded one.
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

### Changed

- The kiosk app has the pi logo as its launcher icon (`pi.dev/logo-auto.svg`, trimmed and
  padded so it fills the launcher tile like the other apps). The build now compiles
  resources with `aapt2`; it skipped that step while there were none.

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
