# Changelog

All notable changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`+ New session` in the picker** — one tap on a running session's row starts a new
  session in that session's pi process (`POST /act/new` → the extension's `eink:new`
  command) and lands the page on it. Refused while that session is running, so a stray tap
  cannot throw away a turn. The new id is read from `bridges.json` (same pid, new id),
  because the bridge that answers is the instance being replaced.

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
