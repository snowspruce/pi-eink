#!/usr/bin/env python3
"""E-ink friendly viewer for a pi session jsonl.

GET /       -> shell page (ES5 JS, polls /data, only repaints when content changes)
GET /data   -> {"v": "<hash>", "html": "<fragment>"}
GET /static -> plain page with meta refresh (JS-less fallback)

Env: PI_SESSION_FILE (default: newest *.jsonl under ~/.pi/agent/sessions)
"""

import base64
import hashlib
import hmac
import html
import ipaddress
import datetime
import json
import os
import re
import secrets
import socket
import subprocess
import struct
import threading
import time
import urllib.error
import zlib
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlparse

PORT = int(os.environ.get("EINK_PORT", "8000"))
# Bind one interface when the box has more (utun, docker bridges): 0.0.0.0 exposes
# the page on every one of them.
BIND = os.environ.get("EINK_BIND", "0.0.0.0")
# Public ingress (reverse tunnel / TLS terminator) must not accept the 6-digit pin:
# it is a LAN convenience, and on an internet-facing endpoint the 32-char token is
# the only credential strong enough to guard a shell-capable agent.
NO_PIN = os.environ.get("EINK_NO_PIN") == "1"
BRIDGE_PORT = int(os.environ.get("EINK_BRIDGE_PORT", "8001"))
SESSIONS = Path.home() / ".pi/agent/sessions"
TOKEN_FILE = Path.home() / ".pi/eink/token"
ALLOW_FILE = Path.home() / ".pi/eink/allow_ips"
# 7 days, refreshed on use. A month-long cookie on a Kindle carried between home,
# office and cafés is a long time to have someone else's token in your pocket.
COOKIE_MAX_AGE = 7 * 24 * 3600
TRUST_FILE = Path.home() / ".pi/eink/trust.json"
FAILS_FILE = Path.home() / ".pi/eink/fails.json"
ACTION_LOG = Path.home() / ".pi/eink/actions.log"
# short code for the Kindle's awful keyboard; the long token still works for scripts
PIN_FILE = Path.home() / ".pi/eink/pin"
PENDING_FILE = Path.home() / ".pi/eink/pending.json"
UA_FILE = Path.home() / ".pi/eink/uas"
UA_SEEN = Path.home() / ".pi/eink/ua_seen.log"
DIAG_LOG = Path.home() / ".pi/eink/diag.log"
MASK_FILE = Path.home() / ".pi/eink/masked_secrets"
# the session the viewer last rendered; server-written, so a read-only client
# cannot use it to reach a session it was not already shown
VIEW_FILE = Path.home() / ".pi/eink/view.json"
# written by the pi extension: which session and port actually accept actions
STATE_FILE = Path.home() / ".pi/eink/bridge.json"
BRIDGES_FILE = Path.home() / ".pi/eink/bridges.json"  # sessionId -> {port, pid, cwd, sessionFile, updatedAt}
HISTORY_FILE = Path.home() / ".pi/eink/history.json"  # sessionId -> commands sent from the box
COOKIE = "pieink"
LANG_COOKIE = "pielang"
# Page furniture only (~35 strings). Conversation content is never translated.
# Default is Chinese: that is what the reader uses daily; English is opt-in with
# ?lang or /lang?l=en, because an English README reader should still be able to
# read the buttons.
STRINGS = {
    "stop": ("\u505c\u4e0b", "Stop"),
    "continue": ("\u7ee7\u7eed", "Continue"),
    "review": ("\u5ba1\u67e5", "Review"),
    "execute": ("\u6267\u884c", "Run"),
    "status": ("\u72b6\u6001", "Status"),
    "readonly": ("\u53ea\u8bfb", "Read-only"),
    "unlock": ("\u89e3\u9501", "Unlock"),
    "send": ("\u53d1\u9001", "Send"),
    "allow": ("\u5141\u8bb8", "Allow"),
    "deny": ("\u62d2\u7edd", "Deny"),
    "say_placeholder": ("\u8f93\u5165\u6307\u4ee4\uff0c\u56de\u8f66\u53d1\u9001", "Type a command, Enter to send"),
    "awaiting": ("\u7b49\u5f85\u786e\u8ba4", "Waiting for approval"),
    "theme_day": ("\u65e5\u95f4", "Day"),
    "theme_night": ("\u591c\u95f4", "Night"),
    "redraw": ("\u91cd\u7ed8", "Redraw"),
    "sessions": ("\u4f1a\u8bdd\u5217\u8868", "Sessions"),
    "sessions_short": ("\u4f1a\u8bdd", "Sessions"),
    "lang_switch": ("EN", "\u4e2d\u6587"),
    "bridge_stale": (" / bridge \u65e7\u7248\uff08\u4f1a\u8bdd\u672a\u4e0a\u62a5\uff09", " / bridge out of date"),
    "bridge_off": (" / bridge \u672a\u6fc0\u6d3b", " / bridge not running"),
    "pending_note": (" / \u5f85\u786e\u8ba4", " / awaiting approval"),
    "no_bridge_view": ("\u8be5\u4f1a\u8bdd\u672a\u5728\u8fd0\u884c\uff0c\u53ea\u80fd\u67e5\u770b\uff08\u6ca1\u6709\u53ef\u7528\u7684\u52a8\u4f5c\u6309\u94ae\uff09",
                       "This session is not running - nothing to press"),
    "readonly_prompt": ("\u6b64\u7f51\u7edc\u4e3a\u53ea\u8bfb\u3002\u8f93\u5165 PIN \u624d\u80fd\u6253\u5b57\u6216\u6267\u884c\uff1a",
                        "Read-only network. Enter the PIN to type or run:"),
    "readonly_hint": ("\u8be5\u7f51\u7edc\u53ea\u8bfb\uff1a\u53ea\u80fd\u67e5\u770b\u4e0e\u505c\u4e0b\u3002",
                      "Read-only network: view and stop only."),
    "gone_notice": ("\u4f1a\u8bdd %s \u5df2\u4e0d\u53ef\u7528\uff08\u6587\u4ef6\u4e0d\u5728\u6216\u5df2\u79fb\u8d70\uff09\uff0c\u5df2\u5207\u5230\u5f53\u524d\u4f1a\u8bdd",
                    "Session %s is gone (file missing or moved) - showing the current session"),
    "note_label": ("\u63d0\u793a", "Note"),
    "compacted": ("\u4e0a\u4e0b\u6587\u5df2\u538b\u7f29", "context compacted"),
    "compacted_before": ("\uff08\u6b64\u524d %s tokens\uff09", " (%s tokens before)"),
    "no_sessions": ("(\u6ca1\u6709\u4f1a\u8bdd)", "(no sessions)"),
    "empty_value": ("(\u7a7a)", "(empty)"),
    "running_at": ("%s \u8fd0\u884c\u4e2d :%s", "%s running :%s"),
    "new_session": ("\uff0b \u65b0\u4f1a\u8bdd", "+ New session"),
    "new_session_failed": ("\u65b0\u5efa\u4f1a\u8bdd\u672a\u51fa\u73b0\uff08bridge \u6ca1\u4e0a\u62a5\uff09",
                           "The new session did not appear (no bridge reported it)"),
    "queue": ("\u6392\u961f\u4e2d\uff08%d\uff09", "Queued (%d)"),
    "queue_title": ("\u6392\u961f\u4e2d\uff08%d \u6761\uff09", "Queued (%d)"),
    "queue_empty": ("\u961f\u5217\u7a7a\u7740\u3002\u4f1a\u8bdd\u8fd0\u884c\u4e2d\u65f6\uff0c\u8f93\u5165\u6846\u91cc\u53d1\u7684\u8bdd\u4f1a\u6392\u5728\u8fd9\u91cc\u3002",
                     "Nothing queued. While the agent is busy, what you type waits here."),
    "queue_external": ("\u7535\u8111\u7aef\u8fd8\u6392\u7740\u6d88\u606f\uff0c\u8fd9\u91cc\u770b\u4e0d\u5230\u4e5f\u6539\u4e0d\u4e86",
                       "pi also has queued messages from the terminal - not shown or editable here"),
    "queue_add": ("\u52a0\u5230\u6392\u961f", "Add to queue"),
    "queue_clear": ("\u6e05\u7a7a\uff08%d\uff09", "Clear (%d)"),
    "queue_up": ("\u4e0a\u79fb", "Up"),
    "queue_down": ("\u4e0b\u79fb", "Down"),
    "queue_edit": ("\u6539", "Edit"),
    "queue_del": ("\u5220", "Delete"),
    "queue_save": ("\u4fdd\u5b58", "Save"),
    "queue_cancel": ("\u53d6\u6d88", "Cancel"),
    "queue_edit_title": ("\u6539\u6392\u961f\u7b2c %d \u6761", "Edit queued #%d"),
    "queue_back": ("\u8fd4\u56de\u6392\u961f", "Back to the queue"),
    "queue_refresh": ("\u5237\u65b0", "Refresh"),
    "queue_run": ("\u5f00\u59cb\u6295\u9012", "Deliver now"),
    "queue_paused": ("\u961f\u5217\u4ece\u4e0a\u6b21\u4f1a\u8bdd\u6062\u590d\uff0c\u4e0d\u4f1a\u81ea\u5df1\u8dd1\uff1a\u70b9 \u5f00\u59cb\u6295\u9012 \u624d\u4f1a\u53d1",
                     "This queue was restored with the session and will not run by itself: tap Deliver now"),
    "queue_no_bridge": ("\u8be5\u4f1a\u8bdd\u672a\u5728\u8fd0\u884c\uff0c\u6ca1\u6709\u961f\u5217",
                        "This session is not running, so there is no queue"),
    "queue_stale": ("\u8be5\u4f1a\u8bdd\u7684 bridge \u662f\u65e7\u7248\uff1a\u6392\u961f\u8981\u5728 pi \u91cc /reload \u4e4b\u540e\u624d\u6709",
                    "This session's bridge is an older build - queueing needs a /reload in pi"),
    "bridge_old": ("\u8be5\u4f1a\u8bdd\u7684 bridge \u662f\u65e7\u7248\uff08\u5728 pi \u91cc /reload \u540e\u53ef\u7528\uff09",
                   "This session's bridge is an older build (run /reload in pi)"),
    "truncated": ("\u2026 \u5df2\u622a\u65ad\uff08\u5171 %d \u5b57\u7b26\uff09", "\u2026 clipped (%d chars)"),
    "ua_not_allowed": ("ua not allowed\uff08\u5df2\u8bb0\u5f55\uff0c\u53ef\u52a0\u5165 ~/.pi/eink/uas\uff09",
                       "ua not allowed (logged; add it to ~/.pi/eink/uas)"),
    "too_many": ("\u9519\u8bef\u6b21\u6570\u8fc7\u591a\uff0c%d \u5206\u949f\u540e\u518d\u8bd5", "Too many attempts, retry in %d min"),
    "bad_pin": ("PIN \u4e0d\u5bf9", "Wrong PIN"),
    "unlocked": ("\u5df2\u5728\u8fd9\u4e2a\u7f51\u7edc\u89e3\u9501\uff0c\u53ef\u4ee5\u6253\u5b57\u4e86", "Network unlocked - typing enabled"),
    "now_readonly": ("\u5df2\u5207\u4e3a\u53ea\u8bfb", "Switched to read-only"),
    "readonly_need_pin": ("\u672c\u7f51\u7edc\u53ea\u8bfb\uff08\u672a\u89e3\u9501\uff09\uff1a\u8f93\u5165 PIN \u624d\u80fd\u6253\u5b57\u6216\u6267\u884c",
                          "Read-only network (locked): enter the PIN to type or run"),
    "session_no_bridge": ("\u8be5\u4f1a\u8bdd\u672a\u5728\u8fd0\u884c\uff08\u6ca1\u6709\u53ef\u7528\u7684 bridge\uff09", "This session has no bridge"),
    "unknown_action": ("\u672a\u77e5\u52a8\u4f5c", "Unknown action"),
    "bridge_not_running": ("bridge \u672a\u6fc0\u6d3b\uff1a%s", "bridge not running: %s"),
    "bridge_not_running_short": ("bridge \u672a\u6fc0\u6d3b", "bridge not running"),
    "failed": ("\u5931\u8d25\uff1a", "Failed: "),
    "failed_code": ("\u5931\u8d25\uff08%d\uff09", "Failed (%d)"),
    "sent": ("\u5df2\u53d1\u9001", "Sent"),
    "allowed": ("\u5df2\u5141\u8bb8", "Allowed"),
    "denied": ("\u5df2\u62d2\u7edd", "Denied"),
    "list_locked": ("\u53ea\u8bfb\u7f51\u7edc\uff1a\u4f1a\u8bdd\u5217\u8868\u8981\u5148\u89e3\u9501\uff08\u9875\u9762\u4e0a\u8f93\u5165 PIN\uff09",
                    "Read-only network: unlock first to list sessions"),
    "back_to_session": ("\u8fd4\u56de\u5f53\u524d\u4f1a\u8bdd", "Back to the current session"),
    "glyphs_prompt": ("\u54ea\u4e9b\u884c\u663e\u793a\u6b63\u5e38\uff1a", "Which rows render correctly:"),
    "glyphs_braille": ("\u76f2\u6587 (pi TUI)", "Braille (pi TUI)"),
    "glyphs_quadrant": ("\u56db\u8c61\u9650", "Quadrants"),
    "glyphs_half": ("\u534a\u5706", "Half circles"),
    "glyphs_triangle": ("\u4e09\u89d2\u5f69", "Triangles"),
    "js_timeout": ("\u8d85\u65f6\uff0c\u8bf7\u91cd\u8bd5", "Timed out, try again"),
    "js_net_error": ("\u7f51\u7edc\u9519\u8bef", "network error"),
    "js_no_history": ("\u6ca1\u6709\u5386\u53f2\u6307\u4ee4", "no history yet"),
    "otp_prompt": ("\u8f93\u5165 6 \u4f4d\u52a8\u6001\u7801", "Enter the 6-digit code"),
    "otp_submit": ("\u786e\u8ba4", "Verify"),
    "otp_bad": ("\u52a8\u6001\u7801\u4e0d\u5bf9", "wrong code"),
    "otp_needed": ("\u9700\u8981\u52a8\u6001\u7801", "2FA code required"),
    "readonly_no_pin": ("\u672c\u7f51\u7edc\u53ea\u8bfb\uff0c\u4e14 PIN \u89e3\u9501\u5df2\u5173\u95ed",
                        "read-only, and pin unlock is disabled"),
}
DEFAULT_LANG = "en" if os.environ.get("EINK_LANG", "").lower().startswith("en") else "zh"


def tr(key: str, lang: str = "", *args) -> str:
    zh, en = STRINGS.get(key, (key, key))
    text = en if lang == "en" else zh
    return text % args if args else text


def lang_of(handler=None) -> str:
    """Page language: cookie wins (set by /lang?l=), then EINK_LANG, then Chinese."""
    if handler is not None:
        want = parse_cookies(handler.headers.get("Cookie") or "").get(LANG_COOKIE) or ""
        if want in ("zh", "en"):
            return want
    return DEFAULT_LANG
DEFAULT_ALLOW = "127.0.0.1,::1"
_UA_SEEN = set()
_FAILS = {}    # ip -> (wrong guesses, window start)
_BLOCKED = {}  # ip -> unix time until which auth is refused
_COOKIE_REFRESH = {}  # ip -> unix time the cookie was last re-issued
# A pin login seeds a random secret here, never the token itself: the token
# also authenticates install.sh and the bridge, and it survives a pin change.
_PIN_SESSIONS = {}  # random per-session secret -> unix time it was minted
_PIN_LOCK = threading.Lock()
# Devices allowed to use the page: ~/.pi/eink/uas, one substring per line.
# A UA is trivially spoofable: this is a speed bump in front of the token, not a boundary.


def ua_allow_list() -> list:
    try:
        # same rule as the bridge: '#' starts a comment, so a commented-out UA is
        # not silently treated as an allowed substring
        text = "\n".join(l.split("#", 1)[0] for l in UA_FILE.read_text().splitlines())
        items = [l.strip().lower() for l in text.splitlines() if l.strip()]
        if items:
            return items
    except Exception:
        pass
    return [u.strip().lower() for u in
            os.environ.get("EINK_ALLOW_UA", "Kindle Build/").split(",") if u.strip()]


def append_bounded(path: Path, line: str, limit: int = 256 * 1024) -> None:
    """Append one line, rotating to .1 past `limit`: some of these paths are
    reachable unauthenticated, and an unbounded log is a disk-fill primitive."""
    try:
        with _WRITE_LOCK:
            if path.exists() and path.stat().st_size > limit:
                os.replace(path, str(path) + ".1")
            with path.open("a") as fh:
                fh.write(line + "\n")
    except Exception:
        pass


def note_unknown_ua(ip: str, ua: str) -> None:
    """Record a rejected UA so it can be added to ~/.pi/eink/uas deliberately."""
    try:
        seen = UA_SEEN.read_text(errors="replace") if UA_SEEN.exists() else ""
        if ua not in seen:
            append_bounded(UA_SEEN, "%s\t%s\t%s" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                                      ip, _diag_clean(ua, 200)))
    except Exception:
        pass


def token() -> str:
    try:
        return TOKEN_FILE.read_text().strip()
    except Exception:
        return ""


def pin() -> str:
    try:
        return PIN_FILE.read_text().strip()
    except Exception:
        return ""


def mask_text(fragment: str) -> str:
    """Never render a live or retired token verbatim: the page is the leak surface."""
    values = []
    try:
        values.append(TOKEN_FILE.read_text().strip())
    except Exception:
        pass
    try:
        values.extend(x.strip() for x in MASK_FILE.read_text().split())
    except Exception:
        pass
    for v in values:
        if len(v) >= 8 and v in fragment:
            fragment = fragment.replace(v, "%s\u2026%s" % (v[:4], v[-3:]))
    return fragment


_LIVE_CACHE = [0.0, {}]


def live_bridges() -> dict:
    """Every pi session currently serving a bridge, keyed by session id."""
    if time.time() - _LIVE_CACHE[0] < 1.0:
        return _LIVE_CACHE[1]
    try:
        raw = json.loads(BRIDGES_FILE.read_text())
    except Exception:
        return {}
    out = {}
    now = time.time()
    for sid, info in raw.items():
        if not isinstance(info, dict):
            continue
        try:
            pid = int(info.get("pid") or 0)
        except Exception:
            continue
        if pid <= 0:
            continue  # os.kill(0, 0) succeeds: it signals the whole process group
        try:
            os.kill(pid, 0)
        except Exception:
            continue
        try:
            age = now - datetime.datetime.fromisoformat(
                str(info.get("updatedAt", "")).replace("Z", "+00:00")).timestamp()
        except Exception:
            continue  # unparseable timestamp: cannot prove it is alive
        if age > 600:  # heartbeat is every 60s; older than 10min means dead
            continue
        out[sid] = info
    _LIVE_CACHE[0] = time.time()
    _LIVE_CACHE[1] = out
    return out


def bridge_port_for(session_id: str) -> int:
    """Port of that session's bridge, or 0 when it is not running (read-only view)."""
    info = live_bridges().get(session_id)
    try:
        return int(info["port"]) if info else 0
    except Exception:
        return 0


def wait_for_new_session(pid: int, old_sid: str, timeout: float = 6.0) -> str:
    """The session a pi process reports after it was asked for a new one.

    Read from the registry rather than from the bridge's reply: the instance that
    answers `/act/new` is the one being replaced. It replies, then shuts down (its
    registry entries for this pid are dropped), and the replacement registers itself
    under the same pid with a fresh session id and a fresh port. Waiting for that
    entry is what makes the picker's button land on the new session instead of on
    the conversation it just left.
    """
    if not pid:
        return ""
    end = time.time() + timeout
    while True:
        newest, newest_at = "", ""
        try:
            raw = json.loads(BRIDGES_FILE.read_text())
        except Exception:
            raw = {}
        for sid, info in raw.items():
            if not isinstance(info, dict) or sid == old_sid:
                continue
            try:
                if int(info.get("pid") or 0) != pid:
                    continue
            except Exception:
                continue
            stamp = str(info.get("updatedAt") or "")
            if stamp >= newest_at:
                newest, newest_at = str(sid), stamp
        if newest:
            _LIVE_CACHE[0] = 0.0   # the next request must see the new registration
            return newest
        if time.time() >= end:
            return ""
        time.sleep(0.15)


SESSION_META_CACHE = {}


def session_meta(path: Path) -> dict:
    """cwd + first user line, cached by mtime: the picker lists ~50 files."""
    key = str(path)
    try:
        stamp = path.stat().st_mtime
    except Exception:
        return {}
    cached = SESSION_META_CACHE.get(key)
    if cached and cached[0] == stamp:
        return cached[1]
    meta = {"cwd": "", "first": "", "id": ""}
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i > 60:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get("type") == "session":
                    meta["cwd"] = row.get("cwd") or ""
                    meta["id"] = row.get("id") or ""
                elif row.get("type") == "message" and row.get("message", {}).get("role") == "user":
                    content = row["message"].get("content")
                    text = content if isinstance(content, str) else "".join(
                        b.get("text", "") for b in content if isinstance(b, dict))
                    meta["first"] = " ".join(text.split())[:60]
                    break
    except Exception:
        pass
    SESSION_META_CACHE[key] = (stamp, meta)
    return meta


# One request resolves the same id several times (resolve_view -> session_selected ->
# session_path_by_id, then newest_session -> session_path_by_id again). Each walk is a
# directory listing plus a stat per file, so it is memoized for the duration of ONE
# request - and never longer: a stale index reads as "this session is gone", which is
# the failure that swaps the conversation. Direct calls (tests, main()) get no memo.
_INDEX_MEMO = threading.local()


def begin_request() -> None:
    """Start a fresh per-request memo. Called once at the top of both handlers."""
    _INDEX_MEMO.active = True
    _INDEX_MEMO.value = None


def session_index() -> dict:
    """{id -> path} for every real pi session file on disk.

    The id has to come from the file's own header row, not from the file name. pi
    names a file `<timestamp>_<id>.jsonl` when it creates it, but writing into an
    existing file (or `--session <path>`) keeps the old name, so id and file name
    disagree for real sessions - 14 of 60 on this machine. Looking the id up as
    `id[-8:] in name` missed those, and the picker's `?s=` then did not resolve: the
    viewer read it as "that session is gone" and fell back to the newest live one,
    so tapping a session in the list showed the running conversation again and the
    page jumped to whichever agent had started last. Files with no session header
    are not pi sessions at all (subagent `*_transcript.jsonl`) and stay out.

    Cached for exactly one request (see `_INDEX_MEMO`), never longer. One walk is a
    directory listing plus a `stat` per file (the header parse behind it is cached by
    mtime), and a request resolves the same id up to four times; but a session written
    seconds ago is exactly the one a fresh link points at, and a stale index reads as
    "gone" - the failure that swaps the conversation. So the memo dies with the
    request. Duplicate ids (a copied file) resolve to the newest copy.
    """
    memo = getattr(_INDEX_MEMO, "value", None)
    if memo is not None:
        return memo
    index, seen = {}, {}
    for path in SESSIONS.rglob("*.jsonl"):
        sid = str(session_meta(path).get("id") or "")
        if not sid:
            continue
        try:
            stamp = path.stat().st_mtime
        except Exception:
            continue
        if sid not in seen or stamp > seen[sid]:
            seen[sid] = stamp
            index[sid] = path
    if getattr(_INDEX_MEMO, "active", False):
        _INDEX_MEMO.value = index
    return index


def session_path_by_id(session_id: str):
    if not session_id:
        return None
    return session_index().get(session_id)


def session_selected(session_id: str) -> bool:
    """True when an explicit ?s= still names a session the viewer can read.

    `newest_session()` falls back to the newest live bridge when an id does not
    resolve, which is how a page showing a deleted or forked session silently
    turned into another conversation. The caller has to tell "the reader picked
    this one" apart from "that one is gone".
    """
    if not session_id:
        return True
    if session_id in live_bridges():
        # running counts even before its file lands: a session created from the
        # picker a second ago has a bridge and no jsonl yet, and answering "gone"
        # would bounce the reader back to the session they just left
        return True
    return session_path_by_id(session_id) is not None


def remembered_sid() -> str:
    """The session the viewer last rendered, as written by the server itself.

    A read-only network may not *choose* a session, but without this the page
    followed "the newest live session" and hopped to another conversation whenever
    a second pi heartbeat landed later. Written only server-side: a client cannot
    use it to reach a session the network was not already shown.
    """
    try:
        return str(json.loads(VIEW_FILE.read_text()).get("sid") or "")
    except Exception:
        return ""


def remember_sid(session_id: str) -> None:
    if not session_id or session_id == remembered_sid():
        return
    try:
        atomic_write(VIEW_FILE, json.dumps({"sid": session_id}))
    except Exception:
        pass  # a lost hint must never break the page


def resolve_view(requested_sid: str, caps: str) -> tuple:
    """(path, sid, gone): what this request renders, and the id it lost.

    One function for GET and for the POST routing, so a button can never target a
    session other than the one on screen.
    """
    sid, gone = requested_sid, ""
    if caps != "full" and sid:
        # read-only: the network may not choose another project's history
        sid = ""
    elif sid and not session_selected(sid):
        gone, sid = sid, ""
    if not sid:
        kept = remembered_sid()
        if kept and session_selected(kept):
            sid = kept
    return newest_session(sid), sid, gone


def sessions_html(lang: str = "") -> str:
    """Picker: running bridges first, then the most recently used session files."""
    live = live_bridges()
    rows = []
    for sid, info in sorted(live.items(), key=lambda kv: kv[1].get("updatedAt", ""), reverse=True):
        cwd = info.get("cwd") or ""
        rows.append('<div class="sess"><a href="/?s=%s"><b>%s</b></a>%s'
                    '<div class="mono dim">%s</div></div>'
                    % (quote(str(sid), safe=""), esc(short_path(cwd) or sid[:8]),
                       new_session_form(str(sid), lang),
                       esc(tr("running_at", lang, short_path(cwd), str(info.get("port", ""))))))

    files = sorted(session_index().values(), key=lambda f: f.stat().st_mtime, reverse=True)
    shown = 0
    for path in files:
        meta = session_meta(path)
        sid = str(meta.get("id") or "")
        if not sid or sid in live:
            continue   # a file with no session header is not a session (transcripts)
        when = time.strftime("%m-%d %H:%M", time.localtime(path.stat().st_mtime))
        rows.append('<div class="sess"><a href="/?s=%s"><b>%s</b></a>'
                    '<div class="mono dim">%s \u00b7 %s</div>'
                    '<div class="body">%s</div></div>'
                    % (quote(str(sid), safe=""), esc(when + "  " + (short_path(meta.get("cwd")) or "?")),
                       esc(short_path(meta.get("cwd"))), esc(when),
                       esc(meta.get("first") or tr("empty_value", lang))))
        shown += 1
        if shown >= 25:
            break
    return "".join(rows) or '<div class="body">%s</div>' % esc(tr("no_sessions", lang))


def new_session_form(sid: str, lang: str = "") -> str:
    """"+ new session" for a running pi process.

    Only a live process can create a session: the tap is proxied to that session's
    bridge, which asks its command context for one. That is why the button sits on
    the rows of the running sessions and not in the page header - a file-only
    session has nothing to create a session with.
    """
    return ('<form class="new" method="POST" action="/act/new?s=%s">'
            '<button class="btn" type="submit">%s</button></form>'
            % (quote(str(sid), safe=""), esc(tr("new_session", lang))))


def bridge_state():
    """Session + port of the pi process that owns the action bridge, if it is alive."""
    try:
        state = json.loads(STATE_FILE.read_text())
        pid = int(state.get("pid") or 0)
        if pid <= 0:
            return None   # os.kill(0, 0) succeeds and would fake a live bridge
        os.kill(pid, 0)
    except Exception:
        return None
    if not state.get("sessionFile"):
        return None
    return state


def rendered_session_id() -> str:
    """Id of the session a page without ?s= is showing.

    Actions used to fall back to `bridge.json` (whichever pi wrote it last), while
    the page renders the newest live bridge - with two sessions running, "stop" on
    one page halted the other. Resolving the id makes both sides agree.

    It goes through `resolve_view()` with the read-only path forced, so the routing
    follows the same remembered session the page displays instead of re-deriving
    "newest" (which was the second half of the hopping bug).
    """
    path, sid, _gone = resolve_view("", "readonly")
    if sid:
        return sid
    return str(session_meta(path or Path("/nonexistent")).get("id") or "")


def bridge_port() -> int:
    state = bridge_state()
    try:
        return int(state["port"]) if state else BRIDGE_PORT
    except Exception:
        return BRIDGE_PORT


def allow_entries() -> list:
    """Allowlist from ~/.pi/eink/allow_ips (one IP or CIDR per line), else the env default.
    A file makes "switch WiFi" a one-line edit instead of a code change."""
    try:
        text = "\n".join(l.split("#", 1)[0] for l in ALLOW_FILE.read_text().splitlines())
        items = [x.strip() for x in re.split(r"[\s,]+", text) if x.strip()]
        if items:
            return items
    except Exception:
        pass
    return [x.strip() for x in os.environ.get("EINK_ALLOW_IPS", DEFAULT_ALLOW).split(",") if x.strip()]


def local_ip(handler) -> str:
    try:
        return handler.request.getsockname()[0]
    except Exception:
        return ""


def same_subnet_allowed() -> bool:
    """On by default: peers must live in the host's own /24 unless allow_ips
    lists them. This is connectivity, not security - see caps_for()."""
    return os.environ.get("EINK_ALLOW_SAME_SUBNET", "1") != "0"


def ip_allowed(ip: str, host_ip: str) -> tuple:
    """True when the peer is loopback, matches an allowlist entry, or lives in the
    same /24 as this machine (EINK_ALLOW_SAME_SUBNET=0 disables the last one)."""
    if is_loopback(ip):
        return True, ""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False, "bad peer address"
    for entry in allow_entries():
        try:
            if "/" in entry:
                if addr in ipaddress.ip_network(entry, strict=False):
                    return True, ""
            elif addr == ipaddress.ip_address(entry):
                return True, ""
        except ValueError:
            continue
    if same_subnet_allowed() and host_ip:
        try:
            if addr in ipaddress.ip_network(host_ip + "/24", strict=False):
                return True, ""
        except ValueError:
            pass
    return False, "ip %s not allowed" % ip


# Actions that stay available on a network nobody has unlocked yet. stop is here
# on purpose: cutting a runaway agent loose from an untrusted LAN is the one
# action that is safe without a secret.
READONLY_ACTIONS = {"stop", "status", "unlock", "readonly"}
# The queue lives in the session's bridge; these actions read and mutate it.
QUEUE_ACTIONS = {"qadd", "qdel", "qedit", "qmove", "qclear", "qrun"}
BRIDGE_ACTIONS = {"say", "continue", "review", "execute", "approve", "deny",
                  "stop", "status", "new", "qadd", "qdel", "qedit", "qmove", "qclear",
                  "qrun"}


def action_allowed(caps: str, action: str) -> bool:
    """The enforcement point for read-only networks, kept pure so the gate itself is
    unit-tested and not only the decision that feeds it."""
    return caps == "full" or action in READONLY_ACTIONS


_TRUST_CACHE = [None, {}]
_WRITE_LOCK = threading.Lock()
# Separate from _WRITE_LOCK: auth accounting must never wait on disk IO.
_AUTH_LOCK = threading.Lock()
_LAST_FAIL_COUNTED = {}   # ip -> unix time of the last counted cookie failure


def atomic_write(path: Path, text: str) -> None:
    """tmp+rename with a unique tmp name: a fixed `.tmp` is shared by every
    handler thread, and two concurrent writers leave a broken file behind."""
    # pid AND thread: two viewer processes on the same host have identical
    # main-thread idents, so a thread-only name made them overwrite each other
    tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
    with open(tmp, "w") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


HISTORY_MAX = 10


def _history_all() -> dict:
    try:
        data = json.loads(HISTORY_FILE.read_text())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def history_list(sid: str) -> list:
    """Commands sent from this session's box, newest first.

    Server side on purpose: `localStorage` is a per-app switch in Android WebView
    (`setDomStorageEnabled`) and this browser is not guaranteed to have it, which
    showed up as a history button that did nothing at all.
    """
    items = _history_all().get(sid or "")
    return [str(x) for x in items] if isinstance(items, list) else []


def history_push(sid: str, text: str) -> None:
    text = (text or "").strip()
    if not text or not sid:
        return
    with _WRITE_LOCK:
        data = _history_all()
        items = [str(x) for x in (data.get(sid) or []) if str(x) != text]
        items.insert(0, text)
        data[sid] = items[:HISTORY_MAX]
        if len(data) > 50:      # bounded: one entry per session ever opened
            data = dict(sorted(data.items(), key=lambda kv: len(kv[1]), reverse=True)[:50])
        try:
            atomic_write(HISTORY_FILE, json.dumps(data, indent=2, ensure_ascii=False))
        except Exception:
            pass


def trust_hours() -> float:
    """EINK_TRUST_HOURS=8 also expires a confirmation after eight hours. Off (0) by
    default: the network change check below is what actually matters."""
    try:
        return max(0.0, float(os.environ.get("EINK_TRUST_HOURS") or 0))
    except Exception:
        return 0.0


def trust_stamp(state: dict) -> tuple:
    """(subnet, at) of the network that was unlocked last, and when."""
    stamp = state.get("stamp") or {}
    if not isinstance(stamp, dict):
        return "", 0.0
    subnet = str(stamp.get("subnet") or "")
    try:
        at = float(stamp.get("at") or 0)
    except Exception:
        at = 0.0
    return subnet, at


def trust_state() -> dict:
    """Cached by (mtime, size): this is read on every poll, and the page polls at
    1.2s while the agent works."""
    try:
        st = TRUST_FILE.stat()
        key = (st.st_mtime, st.st_size)
    except Exception:
        return {"confirmed": [], "peers": [], "readonly": False}
    if _TRUST_CACHE[0] == key:
        return _TRUST_CACHE[1]
    try:
        data = json.loads(TRUST_FILE.read_text())
    except Exception:
        return {"confirmed": [], "peers": [], "readonly": False}
    state = {
        "confirmed": [str(x) for x in (data.get("confirmed") or [])],
        "peers": [str(x) for x in (data.get("peers") or [])],
        "readonly": bool(data.get("readonly")),
        "stamp": data.get("stamp") if isinstance(data.get("stamp"), dict) else {},
    }
    _TRUST_CACHE[0] = key
    _TRUST_CACHE[1] = state
    return state


def save_trust(state: dict) -> None:
    try:
        with _WRITE_LOCK:
            atomic_write(TRUST_FILE, json.dumps(state, indent=2))
        _TRUST_CACHE[0] = None   # a same-size write within one mtime tick must not stick
    except Exception:
        pass


def confirm_network(handler) -> None:
    """Remember the host's current /24 and the peer's /24 as unlocked.

    Both are needed: office networks often put the wired host and the Wi-Fi Kindle
    in different subnets, and only the peer address is visible per request.
    """
    host = host_subnet(handler)
    peer = peer_subnet(handler)
    state = trust_state()
    state["readonly"] = False
    if host:
        state["stamp"] = {"subnet": host, "at": time.time()}
    if host and host not in state["confirmed"]:
        state["confirmed"].append(host)
    if peer and peer not in state["peers"]:
        state["peers"].append(peer)
    save_trust(state)


def host_subnet(handler) -> str:
    """The /24 this instance is serving on: the only thing that distinguishes
    "home", "office" and "some café" when the peer is just another LAN client."""
    try:
        return str(ipaddress.ip_network(local_ip(handler) + "/24", strict=False))
    except Exception:
        return ""


def peer_subnet(handler) -> str:
    try:
        return str(ipaddress.ip_network(client_ip(handler) + "/24", strict=False))
    except Exception:
        return ""


def caps_for(handler) -> str:
    """"full" or "readonly" for this peer.

    The peer address cannot separate home from a café - both are simply the LAN
    the laptop is on. So the capability follows the network the host itself is
    attached to, and every network is unlocked once by entering the pin from it.
    A network the viewer has never served on therefore opens read-only, and a peer
    that is routed in from another subnet does not inherit the host's trust.
    """
    if is_loopback(client_ip(handler)):
        return "full"
    state = trust_state()
    if state["readonly"]:
        return "readonly"
    host = host_subnet(handler)
    if not host or host not in state["confirmed"]:
        return "readonly"
    if peer_subnet(handler) not in state["peers"]:
        return "readonly"
    # Confirmed-once used to mean confirmed forever: a café that happens to use the
    # same /24 as home inherited `full` months later. A confirmation now belongs to
    # the network it was given on, and optionally expires.
    stamped, at = trust_stamp(state)
    if not stamped:
        # trust.json from before stamps existed: the network serving this request is
        # the one that was unlocked, so record it (once) instead of dropping to
        # read-only - or, worse, re-deriving a fresh confirmation on every network.
        try:
            at = TRUST_FILE.stat().st_mtime
        except Exception:
            at = time.time()
        stamped = host
        state["stamp"] = {"subnet": host, "at": at}
        save_trust(state)
    if stamped != host:
        return "readonly"
    hours = trust_hours()
    if hours and time.time() - at > hours * 3600:
        return "readonly"
    return "full"


# A small burst, because one tap sends two beacons within a second (submit, then
# done) and dropping the second would hide exactly the signal this is here for.
_DIAG_HITS = {}   # ip -> [hits, window start]
_DIAG_BURST, _DIAG_WINDOW = 8, 10.0
_DIAG_SEEN = None


def _diag_allow(ip: str) -> bool:
    now = time.time()
    hits, start = _DIAG_HITS.get(ip, [0, now])
    if now - start > _DIAG_WINDOW:
        hits, start = 0, now
    hits += 1
    _DIAG_HITS[ip] = [hits, start]
    return hits <= _DIAG_BURST


def _diag_clean(value: str, limit: int) -> str:
    """One log field: no tabs/newlines (they would forge a record) and bounded."""
    return re.sub(r"[\x00-\x1f\x7f\t]", " ", str(value))[:limit]


def _diag_seen() -> set:
    """Read the log once, not once per beacon: a beacon costs a request."""
    global _DIAG_SEEN
    if _DIAG_SEEN is None:
        _DIAG_SEEN = set()
        try:
            for line in DIAG_LOG.read_text(errors="replace").splitlines():
                if "\t" in line:
                    _DIAG_SEEN.add(line.split("\t", 1)[1])
        except Exception:
            pass
    return _DIAG_SEEN


def log_action(ip: str, caps: str, action: str, status: int, sid: str = "") -> None:
    """One line per action: who drove the agent, from which kind of network, on
    which session, and whether it worked. The only record after a stolen cookie."""
    try:
        if ACTION_LOG.exists() and ACTION_LOG.stat().st_size > 1_000_000:
            os.replace(ACTION_LOG, str(ACTION_LOG) + ".1")
        # sid/action come from the request line: control characters would forge
        # records (a %0D%0A in ?s= used to split one action into two lines)
        clean_sid = _diag_clean(sid, 8)
        clean_action = _diag_clean(action, 64)
        with ACTION_LOG.open("a") as fh:
            fh.write("%s\tip=%s\tcaps=%s\tsid=%s\taction=%s\tstatus=%d\n" % (
                time.strftime("%Y-%m-%d %H:%M:%S"), ip, caps, clean_sid, clean_action,
                status))
    except Exception:
        pass


def load_fails() -> None:
    """Restart must not clear the lockout: 5 guesses per 10 minutes only means
    something if a crash or a `kickstart -k` does not reset the counter."""
    try:
        data = json.loads(FAILS_FILE.read_text())
    except Exception:
        return
    now = time.time()
    for ip, row in (data.get("fails") or {}).items():
        try:
            fails, since = int(row[0]), float(row[1])
        except Exception:
            continue
        if now - since <= 600:
            _FAILS[ip] = (fails, since)
    for ip, until in (data.get("blocked") or {}).items():
        try:
            until = float(until)
        except Exception:
            continue
        if until > now:
            _BLOCKED[ip] = until


def save_fails() -> None:
    now = time.time()
    with _WRITE_LOCK:
        payload = {
            "fails": {ip: [f, s] for ip, (f, s) in _FAILS.items() if now - s <= 600},
            "blocked": {ip: until for ip, until in _BLOCKED.items() if until > now},
        }
        try:
            atomic_write(FAILS_FILE, json.dumps(payload))
        except Exception:
            pass


BRANCH_CACHE = {}


def human(n) -> str:
    n = float(n or 0)
    if n >= 1_000_000:
        return "%.1fM" % (n / 1_000_000)
    if n >= 1_000:
        return "%.0fk" % (n / 1_000)
    return "%d" % n


def short_path(path: str) -> str:
    """Last two path segments: the full cwd pushes the status line past 511px."""
    parts = [p for p in path.split("/") if p]
    if not parts:
        return path
    tail = "/".join(parts[-2:])
    home = str(Path.home())
    if path.startswith(home):
        rel = path[len(home):].strip("/")
        return "~/" + rel if "/" not in rel else "…/" + tail
    return tail if len(parts) > 2 else path


def git_branch(cwd: str) -> str:
    """Read .git/HEAD directly: under launchd the service has a minimal PATH and
    spawning `git` is unreliable, while plain file reads always work."""
    cached = BRANCH_CACHE.get(cwd)
    if cached and time.time() - cached[1] < 15:
        return cached[0]

    branch = ""
    try:
        dot = Path(cwd) / ".git"
        head = dot / "HEAD"
        if dot.is_file():  # worktree: .git is a pointer file
            for line in dot.read_text().splitlines():
                if line.startswith("gitdir:"):
                    head = Path(line.split(":", 1)[1].strip()) / "HEAD"
                    break
        ref = head.read_text().strip()
        if ref.startswith("ref:"):
            branch = ref.rsplit("/", 1)[-1]
        elif ref:
            branch = ref[:7]
    except Exception:
        branch = ""

    BRANCH_CACHE[cwd] = (branch, time.time())
    return branch


def as_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def as_float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def usage_totals(rows: list) -> dict:
    total = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "cost": 0.0}
    for row in rows:
        if row.get("type") != "message":
            continue
        msg = row.get("message")
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        u = msg.get("usage") or {}
        if not isinstance(u, dict):
            continue
        for key in ("input", "output", "cacheRead", "cacheWrite"):
            total[key] += as_int(u.get(key))
        cost = u.get("cost")
        if isinstance(cost, dict):
            total["cost"] += as_float(cost.get("total"))
        else:
            total["cost"] += as_float(cost)
    return total


# ASCII box, kept as the default: it is the only set that is certainly present on any
# client. Measured on the device (Voyage / ED060TC1) by rendering each candidate and
# screenshotting it: braille U+2800 comes out as *tofu boxes*, and quarter blocks, half
# circles, quadrant circles and corner arcs (the U+2596, U+25D0, U+25F4, U+259C families)
# render as nothing at all - not even a placeholder, which looks like a broken page.
# What does render: the eight-way arrows, block fills (U+2581-U+2588, U+258F-U+2588),
# up/down triangles but not left/right, circles, dots, and box drawing U+2500-U+2524.
SPIN = ("[-]", "[\\]", "[|]", "[/]")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # never follow a bridge redirect: it would carry the token


def _bridge_opener():
    return urllib.request.build_opener(_NoRedirect)


def bridge_queue(port: int = 0):
    """(items, pi_has_its_own, armed) for this session's bridge queue, or None.

    The bridge owns the queue: pi's own pending messages are a boolean behind
    ctx.hasPendingMessages() and cannot be listed, edited or reordered from an
    extension, so what the page below lists is what the box queued itself.
    """
    if port <= 0:
        return None
    try:
        req = urllib.request.Request("http://127.0.0.1:%d/queue" % port,
                                     headers={"Cookie": "%s=%s" % (COOKIE, token()),
                                              "User-Agent": "viewer"})
        with _bridge_opener().open(req, timeout=2.0) as resp:
            data = json.loads(resp.read())
    except Exception:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return None
    return data["items"], bool(data.get("pending")), bool(data.get("armed", True))


def agent_state(port: int = 0):
    """Ask the bridge whether the agent is running. None when unavailable."""
    if port <= 0:
        return None  # reading a session that has no bridge: never claim it is busy
    try:
        req = urllib.request.Request("http://127.0.0.1:%d/state" % (port or bridge_port()),
                                     headers={"Cookie": "%s=%s" % (COOKIE, token()),
                                              "User-Agent": "viewer"})
        with _bridge_opener().open(req, timeout=1.0) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except Exception:
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True  # exists, owned by someone else
    except Exception:
        return False


def pending_approval():
    """A risky command parked by safety-gate, waiting for yes/no on the e-ink page."""
    try:
        data = json.loads(PENDING_FILE.read_text())
        timeout = int(data.get("timeoutMs") or 120000)
        stale = time.time() * 1000 - int(data.get("at") or 0) > timeout + 5000
        # a pi killed mid-approval leaves the file behind; never show a confirmation
        # that no process is waiting for
        if stale or not pid_alive(data.get("pid")):
            try:
                PENDING_FILE.unlink()
            except Exception:
                pass
            return None
        return data
    except Exception:
        return None


def pending_owned_by(port: int, data: dict) -> bool:
    """True when the parked command belongs to the session this page renders.

    pending.json is shared by every pi process; without this check the command
    text of another session's risky tool call shows up on this page, where the
    buttons cannot answer it.
    """
    if not port:
        return False
    parked = data.get("pid")
    for info in live_bridges().values():
        if info.get("pid") == parked:
            try:
                return int(info.get("port") or 0) == port
            except Exception:
                return False
    return False


def pending_html(sid: str = "", port: int = 0, caps: str = "full", lang: str = "") -> str:
    data = pending_approval()
    if not data or not pending_owned_by(port, data):
        return ""
    if caps != "full":
        # the buttons could only 403, and the block quotes the parked command:
        # an unlocked network must not be shown another session's command text
        return ""
    suffix = ("?s=" + quote(sid, safe="")) if sid else ""
    reason = esc(str(data.get("reason") or ""))
    raw_cmd = str(data.get("command") or "")
    if len(raw_cmd) > 600:
        raw_cmd = raw_cmd[:600] + tr("truncated", lang, len(raw_cmd))
    command = esc(raw_cmd)
    return (
        '<div id="pending"><div class="body"><b>%s</b>\uff1a%s</div>'
        "<pre>%s</pre>"
        '<form method="POST" action="/act/approve%s"><button type="submit">%s</button></form>'
        '<form method="POST" action="/act/deny%s"><button type="submit">%s</button></form>'
        "</div>"
    ) % (esc(tr("awaiting", lang)), reason, command, suffix, esc(tr("allow", lang)),
         suffix, esc(tr("deny", lang)))


def queue_html(items: list, external: bool = False, sid: str = "", caps: str = "full",
               lang: str = "", armed: bool = True) -> str:
    """The queue page: what the box typed while the agent was busy.

    One row per message, with only the controls that can act on that row (no 上移 on
    the first, no 下移 on the last), because a button that does nothing is a fake
    control and this device has room for about four of them.

    `armed` is the bridge saying "this process may deliver by itself". A queue restored
    with a resumed session is not armed: it is listed, and 开始投递 is the only way to
    start it, so a week-old plan cannot execute behind the reader's back.
    """
    suffix = ("?s=" + quote(sid, safe="")) if sid else ""
    can_edit = caps == "full"
    rows = []
    for n, item in enumerate(items):
        try:
            ident = int(item.get("i"))
        except Exception:
            continue
        text = str(item.get("text") or "")
        when = ""
        try:
            at = float(item.get("at") or 0)
            if at > 0:
                when = time.strftime("%H:%M", time.localtime(at / 1000.0))
        except Exception:
            when = ""
        buttons = ""
        if can_edit:
            if n > 0:
                buttons += ('<form method="POST" action="/act/qmove%s">'
                            '<input type="hidden" name="i" value="%d">'
                            '<input type="hidden" name="d" value="-1">'
                            '<button class="btn" type="submit">%s</button></form>'
                            % (suffix, ident, esc(tr("queue_up", lang))))
            if n < len(items) - 1:
                buttons += ('<form method="POST" action="/act/qmove%s">'
                            '<input type="hidden" name="i" value="%d">'
                            '<input type="hidden" name="d" value="1">'
                            '<button class="btn" type="submit">%s</button></form>'
                            % (suffix, ident, esc(tr("queue_down", lang))))
            buttons += ('<a class="btn" href="/queue/edit%s&amp;i=%d">%s</a>'
                        % (suffix, ident, esc(tr("queue_edit", lang))))
            buttons += ('<form method="POST" action="/act/qdel%s">'
                        '<input type="hidden" name="i" value="%d">'
                        '<button class="btn" type="submit">%s</button></form>'
                        % (suffix, ident, esc(tr("queue_del", lang))))
        rows.append('<div class="sess"><div class="mono dim">%s</div>'
                    '<div class="body">%s</div><div class="qacts">%s</div></div>'
                    % (esc(("%d \u00b7 " % (n + 1)) + when if when else str(n + 1)),
                       esc(text[:400]), buttons))
    head = '<div class="body"><b>%s</b></div>' % esc(tr("queue_title", lang, len(items)))
    if external:
        head += '<div class="body dim">%s</div>' % esc(tr("queue_external", lang))
    if items and not armed:
        head += '<div class="body dim">%s</div>' % esc(tr("queue_paused", lang))
    if not items:
        head += '<div class="body dim">%s</div>' % esc(tr("queue_empty", lang))
    if not can_edit:
        return head + "".join(rows) + '<div class="body dim">%s</div>' % esc(tr("readonly_hint", lang))
    run = ""
    if items and not armed:
        run = ('<form method="POST" action="/act/qrun%s">'
               '<button class="btn" type="submit">%s</button></form>'
               % (suffix, esc(tr("queue_run", lang))))
    add = ('<form method="POST" action="/act/qadd%s" id="qadd">'
           '<textarea name="text" rows="2" maxlength="2000" placeholder="%s"></textarea>'
           '<button class="btn" type="submit">%s</button></form>'
           % (suffix, esc(tr("queue_add", lang)), esc(tr("queue_add", lang))))
    clear = ""
    if items:
        clear = ('<form method="POST" action="/act/qclear%s">'
                 '<button class="btn" type="submit">%s</button></form>'
                 % (suffix, esc(tr("queue_clear", lang, len(items)))))
    return head + "".join(rows) + run + add + clear


def queue_edit_html(ident: int, text: str, sid: str = "", lang: str = "", pos: int = 0) -> str:
    """Edit one queued message. Its own page: a textarea per row would not fit.

    `ident` is the item's stable id (what the form posts); `pos` is where it sits now,
    which is what the reader sees - an id that jumps around after a reorder would be
    the only number on the page they cannot match to a row.
    """
    suffix = ("?s=" + quote(sid, safe="")) if sid else ""
    title = tr("queue_edit_title", lang, pos or ident)
    return ('<div class="body"><b>%s</b></div>'
            '<form method="POST" action="/act/qedit%s" id="qadd">'
            '<input type="hidden" name="i" value="%d">'
            '<textarea name="text" rows="6" maxlength="2000">%s</textarea>'
            '<button class="btn" type="submit">%s</button></form>'
            '<div class="body"><a href="/queue%s">%s</a></div>'
            % (esc(title), suffix, ident, esc(text),
               esc(tr("queue_save", lang)), suffix, esc(tr("queue_cancel", lang))))


def bridge_alive() -> bool:
    """The action buttons live on a port owned by the pi extension. When that
    extension is not bound, buttons silently fail; surface it instead."""
    try:
        with socket.create_connection(("127.0.0.1", bridge_port()), timeout=0.4):
            return True
    except OSError:
        return False


def is_loopback(ip: str) -> bool:
    return ip.startswith("127.") or ip in ("::1", "localhost")


def client_ip(handler) -> str:
    ip = handler.client_address[0] if handler.client_address else ""
    return ip[7:] if ip.startswith("::ffff:") else ip


def parse_cookies(header: str) -> dict:
    out = {}
    for part in (header or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def qint(q, name: str, default: int, lo: int, hi: int) -> int:
    """Query int that cannot 500 the handler: ?n=abc used to raise ValueError."""
    try:
        value = int((q.get(name) or [""])[0] or default)
    except Exception:
        return default
    return max(lo, min(hi, value))
ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")

CSS = """
* { box-sizing: border-box; }
body {
  margin: 0; padding: 0;
  background: #fff; color: #000;
  font-family: -apple-system, "Helvetica Neue", Helvetica, Arial, sans-serif;
  font-size: __FS__px; line-height: 1.45;
  /* Android Chromium (the Kindle's WebView) inflates body text for narrow
     viewports and ignores author font sizes unless this is disabled. */
  -webkit-text-size-adjust: none;
  text-size-adjust: none;
}
/* The status line sits at the BOTTOM, above the footer, not pinned to the top.
   Pinned to the top it was unusable: with the browser's quick-controls mode (the
   only way to hide the URL bar on the Kindle) the WebView is offset vertically by
   an amount that changes with the toolbar state, so a fixed top:0 element either
   lost its first half or vanished entirely - and no padding value worked for both.
   In flow at the end of the document it is drawn where the geometry is known. */
#bar {
  background: #fff; border-top: 1px solid #000;
  padding: 1px 8px 2px; font-size: 9px; line-height: 13px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  color: #777;
}
#bar #status { color: #000; }
/* separated by space, not a character: links must not run into the numbers */
#bar #foottxt { margin-left: 10px; }
#wrap { padding: 16px 12px 6px 12px; }   /* clears #top: 18px line + 10px offset + 6px */
.msg { margin: 0 0 26px 0; }
.role {
  font-size: 11px; letter-spacing: 1px; text-transform: uppercase;
  border: 2px solid #000; padding: 0 8px; display: inline-block;
  margin-bottom: 6px;
}
.role.user { background: #000; color: #fff; }
.role.assistant { background: #fff; color: #000; }
.role.tool { background: #fff; color: #000; border-style: dashed; }
.role.sys { border: none; color: #555; padding: 0; }
.body { white-space: pre-wrap; word-wrap: break-word; }
.body + .body, .body + table, .body + pre, .body + ul, .body + ol,
table + .body, pre + .body, ul + .body, ol + .body, .body + blockquote { margin-top: 0.55em; }
.mono {
  font-family: "Courier New", Courier, monospace;
  font-size: 0.78em; line-height: 1.35;
  border-left: 5px solid #000; padding-left: 10px;
}
.dim { color: #444; }
code { font-family: "Courier New", Courier, monospace; font-size: 0.9em; background: #eee; padding: 0 3px; }
pre {
  font-family: "Courier New", Courier, monospace;
  font-size: 0.78em; line-height: 1.35;
  background: #f2f2f2; border-left: 5px solid #000;
  padding: 6px 10px; margin: 8px 0; white-space: pre-wrap; word-wrap: break-word;
}
h1, h2, h3, h4 { font-size: 1.05em; margin: 14px 0 6px; }
ul, ol { margin: 6px 0; padding-left: 26px; }
/* A list item is rendered OUTSIDE .body (which carries word-wrap), so a long token -
   a path, a key=value, a URL - pushed the li wider than the page: 21 CSS px of overflow
   and a horizontal scrollbar on every page that had such a bullet. Measured on the
   Kindle with the /diag beacon (dw 520 vs iw 511), see DEVELOPMENT.md 19. */
li { margin: 2px 0; word-wrap: break-word; overflow-wrap: break-word; }
table { border-collapse: collapse; margin: 10px 0; font-size: 0.82em; width: 100%; table-layout: fixed; }
th, td { border: 1px solid #000; padding: 3px 6px; text-align: left; vertical-align: top; word-wrap: break-word; overflow-wrap: anywhere; }
th { background: #eee; }
blockquote { margin: 10px 0; padding-left: 12px; border-left: 5px solid #888; color: #222; }
hr { border: none; border-top: 2px solid #000; margin: 16px 0; }
.err { border-left-color: #000; background: #eee; }
.toolname { font-weight: bold; font-family: "Courier New", monospace; font-size: 0.8em; }
#pending {
  border: 3px solid #000; padding: 10px; margin: 18px 0 0 0;
  background: #fff;
}
#pending form { display: inline-block; margin: 8px 10px 0 0; line-height: 1; }
#acts {
  margin: 6px 0 0 0; padding: 3px 0 0 0;
  border-top: 1px solid #000;
  line-height: 1; height: auto;
  white-space: nowrap; overflow: hidden;
}
#acts form { display: inline-block; vertical-align: top; margin: 0 6px 0 0; line-height: 1; }
#more { display: none; }
#acts #more.open { display: inline; }
#acts #morebtn { min-width: 0; width: auto; padding: 1px 8px; }
/* One row: field then button. `-webkit-box` is the 2009 flexbox that the older
   Kindle browser understands, `flex` the one Chromium 30 uses; both are written so
   neither engine falls back to stacked block boxes. No `gap`: neither has it, so the
   spacing is a margin on the button. */
#acts #sayform {
  display: -webkit-box; display: flex;
  -webkit-box-align: stretch; align-items: stretch;
  margin-top: 4px; white-space: normal;
  width: 100%; -webkit-box-sizing: border-box; box-sizing: border-box;
}
#acts #sayform textarea, #acts #sayform input {
  display: block;
  -webkit-box-flex: 1; flex: 1 1 auto; min-width: 0;
  -webkit-box-sizing: border-box; box-sizing: border-box;
  font-family: inherit; font-size: 14px; line-height: 1.3;
  border: 2px solid #000; padding: 4px 8px; margin: 0;
  background: #fff; color: #000; resize: none;
}
#acts #sayform button {
  display: block;
  -webkit-box-flex: 0; flex: 0 0 auto;
  width: auto; min-width: 64px; margin: 0 0 0 8px; padding: 4px 12px;
}
/* History recall sits left of the box, so it needs its own rule: the generic button
   rule above forces min-width 64px, which would eat a tenth of a 511px screen. */
#acts #sayform #hist { min-width: 0; width: auto; margin: 0 8px 0 0; padding: 4px 8px; }
#acts button, .btn {
  font-family: inherit; font-size: 12px; min-height: 26px;
  background: #fff; color: #000; border: 2px solid #000;
  padding: 1px 9px; margin: 0;
}
.sess { border-bottom: 1px solid #000; padding: 8px 0; }
.sess a { color: #000; }
/* "+ new session" sits on the running rows: one running pi process per row, and
   only a running process can create a session in its own project. */
.sess form.new { display: inline-block; vertical-align: middle; margin: 0 0 0 8px; }
.sess .btn { font-size: 11px; min-height: 20px; padding: 0 6px; line-height: 16px; }
/* Queue page: one action row per message. `a.btn` has to look like the buttons next
   to it (改 is a link because editing needs its own page), so it carries the same
   border and gets `display:inline-block` - the shared `.btn` rule is written for
   <button>, which is a block-level-ish box in these engines. */
.qacts { margin-top: 6px; }
.qacts form { display: inline-block; margin: 0 6px 0 0; line-height: 1; }
.qacts .btn { font-size: 11px; min-height: 20px; padding: 0 6px; line-height: 16px; }
a.btn { display: inline-block; text-decoration: none; font-size: 11px;
  min-height: 20px; padding: 0 6px; line-height: 16px; }
#qadd { margin-top: 12px; }
#qadd textarea {
  display: block; width: 100%; -webkit-box-sizing: border-box; box-sizing: border-box;
  font-family: inherit; font-size: 14px; line-height: 1.3;
  border: 2px solid #000; padding: 4px 8px; margin: 0 0 6px 0; background: #fff; color: #000;
}
#note { font-size: 20px; font-weight: bold; padding: 6px 0 0; }
#result { padding: 3px 8px; font-size: 14px; font-weight: bold; color: #000; min-height: 20px; }

"""

# Night mode: the Kindle frontlight plus a white page is painful in the dark.
# Inverted, high-contrast rules appended after the base sheet so they win.
DARK_CSS = """
body { background: #000; color: #fff; }
#bar { background: #000; border-top-color: #fff; color: #999; }
#bar #status { color: #fff; }
.role { border-color: #fff; }
.role.user { background: #fff; color: #000; }
.role.assistant, .role.tool { background: #000; color: #fff; }
.role.sys { color: #bbb; }
.mono { border-left-color: #fff; }
.dim { color: #bbb; }
code { background: #222; color: #fff; }
pre { background: #111; border-left-color: #fff; color: #fff; }
th, td { border-color: #fff; }
th { background: #222; color: #fff; }
blockquote { border-left-color: #888; color: #eee; }
hr { border-top-color: #fff; }
.err { border-left-color: #fff; background: #111; }
#pending { border-color: #fff; background: #000; }
#acts { border-top-color: #fff; }
#acts button, .btn { background: #000; color: #fff; border-color: #fff; }
#acts #sayform textarea, #acts #sayform input { background: #000; color: #fff; border-color: #fff; }
/* Chromium 30 (the Kindle) only knows the -webkit- prefixed pseudo element */
#acts #sayform textarea::-webkit-input-placeholder,
#acts #sayform input::-webkit-input-placeholder { color: #eee; }
#acts #sayform textarea::placeholder, #acts #sayform input::placeholder { color: #eee; }
.sess { border-bottom-color: #fff; }
.sess a { color: #fff; }
#qadd textarea { background: #000; color: #fff; border-color: #fff; }
#note, #result { color: #fff; }
a { color: #fff; }
"""

def _make_icon() -> bytes:
    """32x32 white square with a black pi glyph: browsers cache favicons per origin,
    and without one the Kindle keeps showing the icon of whatever served this port."""
    W = H = 32
    px = [[255] * W for _ in range(H)]

    def rect(x0, y0, x1, y1):
        for y in range(max(0, y0), min(H, y1)):
            for x in range(max(0, x0), min(W, x1)):
                px[y][x] = 0

    rect(0, 0, W, 2)            # frame
    rect(0, H - 2, W, H)
    rect(0, 0, 2, H)
    rect(W - 2, 0, W, H)
    rect(7, 9, 25, 12)          # pi: top bar
    rect(10, 12, 14, 24)        # left leg
    rect(20, 12, 24, 24)        # right leg
    rect(9, 23, 25, 25)         # serif foot

    raw = b"".join(b"\x00" + bytes(row) for row in px)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", W, H, 8, 0, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


FAVICON = _make_icon()

SHELL = r"""<!DOCTYPE html>
<html><head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Type" content="text/html; charset=utf-8">
<link rel="icon" type="image/png" href="/favicon.png">
<link rel="apple-touch-icon" href="/favicon.png">
<title>pi / eink</title>
<style>%(css)s</style>
</head><body>
<div id="wrap"><div id="content"></div><div id="acts">%(acts)s</div></div>
<div id="bar"><span id="status">loading</span><span id="foottxt"></span></div>
<script>
var T = %(i18n)s;
var V = "", KEYS = [], NODES = {};
// The Kindle has no console: any JS error or send attempt is reported to /diag
// so it can be read from ~/.pi/eink/diag.log on the computer.
function diag(q) {
  try { var d = new XMLHttpRequest(); d.open("GET", "/diag?" + q, true); d.send(null); } catch (e) {}
}
window.onerror = function (m, src, line) {
  diag("ev=err&m=" + encodeURIComponent(String(m)) + "&ln=" + line);
  return false;
};
// Only follow the tail while the reader is already at the bottom. While the agent
// works the spinner changes every poll; scrolling unconditionally yanked the page
// back down 1.2s after every attempt to scroll up. The threshold stays small: at
// 140px on a 573px-tall viewport a short flick up still counted as "at the
// bottom" and the next repaint dragged the reader back down.
var STICK = true;
function atBottom() {
  var y = window.pageYOffset || document.documentElement.scrollTop || 0;
  var h = document.documentElement.scrollHeight || document.body.scrollHeight;
  var vh = window.innerHeight || document.documentElement.clientHeight;
  return (y + vh) >= (h - 40);
}
window.addEventListener("scroll", function () { STICK = atBottom(); }, false);
function paint(j) {
  setSpin(!!j.busy, j.spin);
  if (j.v === V) { return; }
  V = j.v;
  var stick = STICK;   // decided before the DOM grows
  var content = document.getElementById("content");
  var items = j.items || [], i, k, wanted = {};
  for (i = 0; i < items.length; i++) { wanted[items[i].k] = 1; }

  for (i = 0; i < KEYS.length; i++) {
    k = KEYS[i];
    if (!wanted[k]) {
      if (NODES[k] && NODES[k].parentNode) { NODES[k].parentNode.removeChild(NODES[k]); }
      delete NODES[k];
    }
  }

  var frag = document.createDocumentFragment(), newKeys = [];
  for (i = 0; i < items.length; i++) {
    k = items[i].k;
    var html = items[i].h, node = NODES[k];
    if (!node) {
      node = document.createElement("div");
      node.innerHTML = html;
      NODES[k] = node;
    } else if (node.__h !== html) {
      node.innerHTML = html;
    }
    node.__h = html;
    frag.appendChild(node);
    newKeys.push(k);
  }
  content.appendChild(frag);
  KEYS = newKeys;
  if (!items.length) { content.innerHTML = j.html; }
  POLL_MS = j.busy ? POLL_MS_BUSY : POLL_MS_IDLE;
  document.getElementById("status").innerHTML = j.status;
  document.getElementById("foottxt").innerHTML = j.foot;
  if (stick) {
    window.scrollTo(0, document.body.scrollHeight);
    STICK = true;   // the synthetic scroll event lands after this paint
  }
  if (!GEO) { GEO = true; geoBeacon(); }
}
var POLL_MS_BUSY = 1200, POLL_MS_IDLE = 3000, POLL_MS = POLL_MS_IDLE;
// The working indicator is animated here, not on the server: a server-side frame had to
// travel through /data and rewrite the whole card, so it could not go faster than a few
// seconds. This only touches one span's text, and the glyphs are all the same width, so
// the ghosting is confined to that box.
var SPIN = ["[-]", "[\\]", "[|]", "[/]"], SPIN_MS = 700, SPIN_I = 0, SPIN_T = null;
function spinTick() {
  SPIN_I = SPIN_I + 1;
  if (SPIN_I >= SPIN.length) { SPIN_I = 0; }
  var el = document.getElementsByClassName("spin")[0];
  if (!el) { return; }   // the card was replaced or the turn ended: nothing to animate
  el.innerHTML = SPIN[SPIN_I];
}
function setSpin(busy, list) {
  if (list && list.length) { SPIN = list; }
  if (busy && !SPIN_T) { SPIN_T = setInterval(spinTick, SPIN_MS); }
  if (!busy && SPIN_T) { clearInterval(SPIN_T); SPIN_T = null; SPIN_I = 0; }
}
function fetchData(done) {
  var x = new XMLHttpRequest();
  x.open("GET", "/data%(qs)st=" + (new Date().getTime()), true);
  x.onreadystatechange = function () {
    if (x.readyState !== 4) { return; }
    if (x.status === 200) {
      try { paint(JSON.parse(x.responseText)); }
      catch (e) { document.getElementById("status").innerHTML = "parse error"; }
    } else {
      document.getElementById("status").innerHTML = "offline";
    }
    if (done) { done(); }
  };
  x.send(null);
}
function poll() {
  fetchData(function () { setTimeout(poll, POLL_MS); });
}
poll();
// One-shot geometry beacon so the server side can see the device's real viewport.
// Fired after the FIRST PAINT, not on load: the shell is empty until /data lands, and a
// beacon taken before that measured an empty document (dw == iw) and hid the very
// overflow it was added to find.
var GEO = false;
function geoBeacon() {
  try {
    var x = new XMLHttpRequest();
    // sw/dw vs iw: a horizontal scrollbar means some box is wider than the viewport.
    // `who` names it (tag#id.class + its width), because "the page is 3px too wide" is
    // otherwise unattributable on a device with no devtools.
    var de = document.documentElement, iw = de.clientWidth, who = "";
    try {
      // Two shapes of overflow: a box wider than the viewport, and a box whose CONTENT
      // is wider than the box (long unbreakable text inside a pre-wrap block - the box
      // stays 511 wide, so only scrollWidth gives it away).
      var all = document.getElementsByTagName("*"), hits = [];
      for (var n = 0; n < all.length; n++) {
        var el = all[n], r = el.getBoundingClientRect();
        var over = Math.max(r.right - iw, el.scrollWidth - el.clientWidth);
        if (over > 0.5) {
          var name = el.tagName.toLowerCase() +
            (el.id ? "#" + el.id : "") +
            (el.className ? "." + String(el.className).split(" ")[0] : "");
          var d = 0, up = el;
          while (up.parentNode) { d++; up = up.parentNode; }
          hits.push({ o: over, d: d, s: name + ":" + Math.round(over) });
        }
      }
      // deepest first: the ancestors overflow for the same reason, only the innermost
      // box says which element is actually too wide
      hits.sort(function (a, b) { return (b.d - a.d) || (b.o - a.o); });
      who = hits.slice(0, 3).map(function (h) { return h.s; }).join(",");
    } catch (e) { who = "err"; }
    x.open("GET", "/diag?w=" + window.innerWidth + "&h=" + window.innerHeight +
      "&sw=" + document.body.scrollWidth + "&dpr=" + (window.devicePixelRatio || 1) +
      "&iw=" + iw + "&dw=" + de.scrollWidth + "&who=" + encodeURIComponent(who), true);
    x.send(null);
  } catch (e) {}
}
document.addEventListener("submit", function (e) {
  var form = e.target;
  var action = form.getAttribute("action") || "";
  if (action.indexOf("/act/") !== 0) { return; }
  if (new RegExp("/status$").test(action)) { return; }   // status has its own page
  e.preventDefault();
  var body = "", els = form.querySelectorAll("textarea,input"), i;
  for (i = 0; i < els.length; i++) {
    if (!els[i].name) { continue; }
    body += (body ? "&" : "") + encodeURIComponent(els[i].name) + "=" + encodeURIComponent(els[i].value);
  }
  showResult("…");
  diag("ev=submit&a=" + encodeURIComponent(action));
  // One tap is the decision: without this a second tap on a slow e-ink screen lands
  // after the request was answered. The buttons must come back afterwards — the
  // page shell is never re-rendered by the poll, so a button left disabled stays
  // dead until the reader reloads the page by hand.
  var btns = form.querySelectorAll("button"), bi;
  for (bi = 0; bi < btns.length; bi++) { btns[bi].disabled = true; }
  var release = function () {
    for (bi = 0; bi < btns.length; bi++) { btns[bi].disabled = false; }
  };
  // An e-ink browser on flaky WiFi may never reach readyState 4; without this the
  // button stays disabled and the only way out is the 9px "redraw" link in the footer.
  var guard = setTimeout(function () {
    try { x.abort(); } catch (e) {}
    release();
    showResult(T.timeout);
  }, 8000);
  var x = new XMLHttpRequest();
  x.open("POST", action, true);
  x.setRequestHeader("Content-Type", "application/x-www-form-urlencoded");
  x.setRequestHeader("Accept", "application/json");
  x.onreadystatechange = function () {
    if (x.readyState !== 4) { return; }
    clearTimeout(guard);
    release();
    var j = null;
    try { j = JSON.parse(x.responseText); } catch (err) { j = null; }
    // status 0 = the request never reached the server (WiFi drop, abort);
    // counting it as success made a dead send look delivered
    var ok = x.status >= 200 && x.status < 400;
    var msg = (j && j.msg) ? j.msg : (ok ? T.sent : T.failed + (x.status || T.netError));
    diag("ev=done&a=" + encodeURIComponent(action) + "&st=" + x.status + "&m=" + encodeURIComponent(msg));
    showResult(msg);
    if (ok) {
      var ta = form.querySelector("textarea");
      if (ta) {
        if (/\/act\/say/.test(action)) { histRemember(ta.value); }
        ta.value = "";
      }
    }
    if (ok && /\/act\/(unlock|readonly)/.test(action)) {
      // #acts comes from the shell, not from /data: without a reload the reader
      // keeps seeing the PIN box, and typing a command into it counts as a bad PIN
      location.reload();
      return;
    }
    if (ok && /\/act\/(approve|deny)/.test(action)) {
      var pend = document.getElementById("pending");
      if (pend && pend.parentNode) { pend.parentNode.removeChild(pend); }
    }
    V = "";
    // one extra fetch, not another polling loop
    setTimeout(function () { fetchData(null); }, 400);
  };
  x.send(body);
}, false);

// The on-screen keyboard is the slowest part of this page, so "\u2191" refills the box with a
// previous command instead of retyping it. Deliberately not focused: focusing opens the
// keyboard, which is exactly what this button exists to avoid.
// The list lives on the server, not in localStorage: DOM storage is a per-app switch in
// Android WebView and this browser may not have it on - a dead button was the result.
var HIST = [], HPOS = -1;
function histRender() {
  var box = document.querySelector("#sayform textarea");
  if (!box) { return; }
  if (!HIST.length) { showResult(T.noHistory); return; }   // never a silent no-op
  HPOS = HPOS + 1;
  if (HPOS >= HIST.length) { HPOS = 0; }   // no modulo: SHELL is a formatted template
  box.value = HIST[HPOS];
  try { box.setSelectionRange(box.value.length, box.value.length); } catch (e) {}
}
function histFetch(done) {
  try {
    var x = new XMLHttpRequest();
    x.open("GET", "/hist%(qs)st=" + (new Date().getTime()), true);
    x.onreadystatechange = function () {
      if (x.readyState !== 4) { return; }
      if (x.status === 200) {
        try { HIST = JSON.parse(x.responseText).items || []; } catch (e) { HIST = []; }
      }
      if (done) { done(); }
    };
    x.send(null);
  } catch (e) { if (done) { done(); } }
}
function histRemember(text) {
  text = String(text || "").replace(/^\s+/, "").replace(/\s+$/, "");
  if (!text) { return; }
  var i;
  for (i = 0; i < HIST.length; i++) { if (HIST[i] === text) { HIST.splice(i, 1); break; } }
  HIST.unshift(text);
  if (HIST.length > 10) { HIST.length = 10; }
  HPOS = -1;
}
(function () {
  var b = document.getElementById("hist");
  if (!b) { return; }   // read-only networks get a PIN box instead
  b.onclick = function () { histRender(); };
  histFetch(null);      // fill on load so the list survives a reload
})();

// The hidden action buttons: one tap to show them, one to put them away. No storage -
// the previous attempt to keep state in localStorage failed silently on this WebView.
(function () {
  var b = document.getElementById("morebtn");
  if (!b) { return; }
  b.onclick = function () {
    var m = document.getElementById("more");
    if (!m) { return; }
    var open = m.className.indexOf("open") >= 0;
    m.className = open ? "" : "open";
    b.innerHTML = open ? "\u25bc" : "\u25b2";   // down = more below, up = put away
  };
})();

var resultTimer = null;
function showResult(msg) {
  // Right below the input: that is where the reader's eye is after tapping send.
  var el = document.getElementById("result");
  if (!el) {
    el = document.createElement("div");
    el.id = "result";
    var acts = document.getElementById("acts");
    if (acts && acts.parentNode) { acts.parentNode.insertBefore(el, acts.nextSibling); }
  }
  el.textContent = msg;
  if (resultTimer) { clearTimeout(resultTimer); resultTimer = null; }
  if (msg !== "…") {
    resultTimer = setTimeout(function () {
      var node = document.getElementById("result");
      if (node) { node.textContent = ""; }
      resultTimer = null;
    }, 5000);
  }
}</script>
</body></html>
"""


def newest_session(session_id: str = "") -> Path | None:
    # explicit selection from the picker wins
    if session_id:
        info = live_bridges().get(session_id)
        if info and info.get("sessionFile"):
            # the file may not exist yet (a session created moments ago); render()
            # copes with a missing path, and jumping to "the newest" instead would
            # show a different conversation under the id the page asked for
            return Path(str(info["sessionFile"]))
        target = session_path_by_id(session_id)
        if target:
            return target
    # otherwise follow the most recently active live bridge
    live = live_bridges()
    if live:
        newest = max(live.values(), key=lambda i: i.get("updatedAt", ""))
        if newest.get("sessionFile"):
            target = Path(str(newest["sessionFile"]))
            if target.exists():
                return target
    state = bridge_state()
    if state:
        target = Path(state["sessionFile"])
        if target.exists():
            return target
    env = os.environ.get("PI_SESSION_FILE")
    if env and Path(env).exists():
        return Path(env)
    files = sorted(session_index().values(), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def esc(text: str) -> str:
    return html.escape(ANSI.sub("", text or ""), quote=False)


FENCE = re.compile(r"```[a-zA-Z0-9+#._-]*\n(.*?)```", re.S)
# 1 or 2 backticks, so ``x`` and `` `a` `` both become code instead of leaking ticks
INLINE_CODE = re.compile(r"(`{1,2})(?!`)(.+?)\1(?!`)", re.S)
BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
ITALIC = re.compile(r"(?<![A-Za-z0-9*])\*(?=[^\s*])([^*\n]+?)(?<=[^\s*])\*(?!\*)")
STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
LINK = re.compile(r"\[([^\]]{1,120})\]\(([^)\s]{1,300})\)")
HEAD = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t#]*$")
ULI = re.compile(r"^([ \t]*)[-*+][ \t]+(.+)$")
OLI = re.compile(r"^([ \t]*)\d+[.)][ \t]+(.+)$")
TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
HR = re.compile(r"^\s*([-*_])\s*(\1\s*){2,}$")


def inline(text: str) -> str:
    """Inline markdown -> html. Escapes first, protects code spans from bold/italic."""
    slots = []
    sentinel = "\x00%s\x00" % os.urandom(8).hex()

    def keep(frag: str) -> str:
        slots.append(frag)
        return "%s%d%s" % (sentinel, len(slots) - 1, sentinel)

    text = INLINE_CODE.sub(lambda m: keep("<code>%s</code>" % html.escape(m.group(2), quote=False)), text)
    text = html.escape(text, quote=False)
    text = BOLD.sub(r"<b>\1</b>", text)
    text = ITALIC.sub(r"<i>\1</i>", text)
    text = STRIKE.sub(r"<s>\1</s>", text)
    text = LINK.sub(r"<u>\1</u>", text)
    for i, frag in enumerate(slots):
        text = text.replace("%s%d%s" % (sentinel, i, sentinel), frag)
    return text


def table_cells(line: str) -> list:
    """Split a table row on real pipes only: escaped \\| and pipes inside code
    spans stay part of the cell. A naive split turned `a \\| b` into extra columns,
    which collapsed every cell to a couple of characters."""
    row = line.strip()
    cells, cur, i, in_code = [], [], 0, False
    while i < len(row):
        ch = row[i]
        if ch == "\\" and i + 1 < len(row) and row[i + 1] == "|":
            cur.append("|")
            i += 2
            continue
        if ch == "`":
            in_code = not in_code
        elif ch == "|" and not in_code:
            cells.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(ch)
        i += 1
    cells.append("".join(cur))
    if cells and not cells[0].strip():
        cells.pop(0)
    if cells and not cells[-1].strip():
        cells.pop()
    return [c.strip() for c in cells]


INLINE_FENCE = re.compile(r"```([^`\n]{1,400}?)```")


def md(text) -> str:
    """Small markdown subset -> html: fences, tables, headings, lists, quotes,
    rules, bold/italic/strike/code/links. Input is raw text.

    `text` is typed loosely on purpose: a session row may carry a JSON object where a
    string was expected (a tool that logs structured output), and `(text or "").replace`
    raised AttributeError -> every page of that session returned 500 until the file was
    edited by hand. Coerce instead of trusting the transcript.
    """
    if text is None:
        text = ""
    elif not isinstance(text, str):
        try:
            text = json.dumps(text, ensure_ascii=False)
        except Exception:
            text = str(text)
    # normalise line endings: CRLF left a stray \r inside <li> and pre blocks
    raw = ANSI.sub("", (text or "").replace("\r\n", "\n").replace("\r", "\n"))
    # a single-line ```x``` is a code block, not three stray backticks
    raw = INLINE_FENCE.sub(lambda m: "```\n%s\n```" % m.group(1), raw)
    blocks = []
    pos = 0
    for m in FENCE.finditer(raw):
        blocks.append(("text", raw[pos:m.start()]))
        blocks.append(("code", m.group(1)))
        pos = m.end()
    blocks.append(("text", raw[pos:]))

    pieces = []
    for kind, chunk in blocks:
        if kind == "code":
            pieces.append("<pre>%s</pre>" % html.escape(chunk.strip("\n"), quote=False))
            continue

        lines = chunk.split("\n")
        i = 0
        para = []

        def flush_para():
            if para:
                pieces.append('<div class="body">%s</div>' % "\n".join(inline(x) for x in para))
                del para[:]

        while i < len(lines):
            line = lines[i]
            stripped = line.strip()

            # table: header row + |---| separator
            if stripped.startswith("|") and i + 1 < len(lines) and TABLE_SEP.match(lines[i + 1]):
                flush_para()
                start = i
                head = table_cells(line)
                i += 2
                rows = []
                while i < len(lines) and lines[i].strip().startswith("|"):
                    rows.append(table_cells(lines[i]))
                    i += 1
                ragged = any(len(r) != len(head) for r in rows) or not 1 <= len(head) <= 6
                if ragged:
                    # Not a real table: show the raw lines instead of a broken grid.
                    raw_block = "\n".join(lines[start:i])
                    pieces.append("<pre>%s</pre>" % html.escape(raw_block, quote=False))
                    continue
                buf = ["<table><thead><tr>"]
                buf += ["<th>%s</th>" % inline(c) for c in head]
                buf.append("</tr></thead><tbody>")
                for r in rows:
                    buf.append("<tr>" + "".join("<td>%s</td>" % inline(c) for c in r) + "</tr>")
                buf.append("</tbody></table>")
                pieces.append("".join(buf))
                continue

            m_head = HEAD.match(stripped)
            if m_head:
                flush_para()
                level = min(len(m_head.group(1)), 3)
                pieces.append("<h%d>%s</h%d>" % (level + 1, inline(m_head.group(2)), level + 1))
                i += 1
                continue

            if HR.match(stripped):
                flush_para()
                pieces.append("<hr>")
                i += 1
                continue

            if stripped.startswith(">"):
                flush_para()
                quote = []
                while i < len(lines) and lines[i].strip().startswith(">"):
                    quote.append(lines[i].strip()[1:].lstrip())
                    i += 1
                pieces.append("<blockquote>%s</blockquote>" % "<br>".join(inline(q) for q in quote))
                continue

            m_ul = ULI.match(line)
            m_ol = OLI.match(line)
            if m_ul or m_ol:
                flush_para()
                tag = "ul" if m_ul else "ol"
                items = []
                while i < len(lines):
                    mu = ULI.match(lines[i])
                    mo = OLI.match(lines[i])
                    if mu and tag == "ul":
                        items.append(mu.group(2))
                    elif mo and tag == "ol":
                        items.append(mo.group(2))
                    elif mu or mo:
                        break
                    else:
                        break
                    i += 1
                pieces.append("<%s>%s</%s>" % (tag, "".join("<li>%s</li>" % inline(x) for x in items), tag))
                continue

            if not stripped:
                flush_para()
                i += 1
                continue

            para.append(line)
            i += 1

        flush_para()

    return "".join(pieces) or '<div class="body"></div>'


def block_text(block: dict) -> tuple[str, str]:
    """Return (kind, text) for one content block."""
    t = block.get("type")
    if t == "text":
        # tool output is not always a string (a dict/number showed up in a
        # session once); md()/esc() only accept str, so coerce instead of 500ing
        # every page until the row scrolls out of the file
        return "text", as_text(block.get("text", ""))
    if t == "thinking":
        return "thinking", as_text(block.get("thinking") or block.get("text", ""))
    if t == "toolCall":
        name = block.get("name") or block.get("toolName") or "tool"
        args = block.get("arguments") or block.get("args") or block.get("input") or {}
        try:
            flat = json.dumps(args, ensure_ascii=False)
        except Exception:
            flat = str(args)
        return "toolcall", f"{name} {flat}"
    return t or "?", json.dumps(block, ensure_ascii=False)[:200]


def clip(text: str, limit: int) -> str:
    if limit and len(text) > limit:
        return text[:limit] + "\n... [%d chars clipped]" % (len(text) - limit)
    return text


def as_text(value) -> str:
    """A session row can carry a non-string where text is expected (a tool that
    returned a JSON object): render it as JSON instead of crashing the page."""
    if isinstance(value, str):
        return value
    if not value:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)


ROWS_CACHE = [None, [], {}]   # (path, mtime, size) -> rows, usage totals
ROW_HTML = {}                 # (path, row id, timestamp, flags) -> (key, html) or False


def load_rows(path: Path):
    """Parsed rows + usage totals, cached by (path, mtime, size). Session files
    are append-only; the page re-renders on every poll, and a 12MB session costs
    ~150ms to walk from scratch."""
    try:
        st = path.stat()
    except Exception:
        return [], usage_totals([])
    key = (str(path), st.st_mtime, st.st_size)
    if ROWS_CACHE[0] == key:
        return ROWS_CACHE[1], ROWS_CACHE[2]
    rows = []
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if isinstance(row, dict):
                rows.append(row)
    totals = usage_totals(rows)
    # one slice assignment, not three writes: a reader in another thread must not
    # see key of X with the rows of Y
    ROWS_CACHE[:] = [key, rows, totals]
    return rows, totals


def row_html(path_key: str, row: dict, index: int, show_thinking: bool, show_tools: bool, version=(),
             lang: str = ""):
    """(key, html) for one row, or False when the row renders nothing.

    Cached per row: rows are append-only, so only the tail is new on each poll. The
    (mtime, size) of the file is part of the key, otherwise a file rewritten in
    place keeps serving the HTML of its previous content, digest included.
    """
    # the index is part of the key: rows without an id would otherwise collide
    ck = (path_key, version, row.get("id") or index, row.get("timestamp"), show_thinking, show_tools, lang)
    if ck in ROW_HTML:
        return ROW_HTML[ck]
    if len(ROW_HTML) > 20000:
        ROW_HTML.clear()  # one long-lived viewer across many sessions
    res = build_row(row, index, show_thinking, show_tools, lang)
    ROW_HTML[ck] = res
    return res


def build_row(row: dict, index: int, show_thinking: bool, show_tools: bool, lang: str = ""):
    if row.get("type") == "compaction":
        # raw history stays in the file after /compact, so mark the boundary:
        # without this the page silently keeps showing pre-compaction messages
        before = row.get("tokensBefore")
        note = tr("compacted", lang)
        if isinstance(before, int) and before > 0:
            note += tr("compacted_before", lang, human(before))
        return ("cmp-" + str(row.get("id") or index),
                '<div class="msg mono dim">\u2014 %s \u2014</div>' % note)
    if row.get("type") != "message":
        return False
    mm = row.get("message")
    if not isinstance(mm, dict):
        return False
    role = mm.get("role") or "?"
    if role == "system":
        return False
    if role == "toolResult" and not show_tools:
        return False
    content = mm.get("content")
    blocks = content if isinstance(content, list) else [{"type": "text", "text": content or ""}]

    parts = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        kind, text = block_text(b)
        if not text or not str(text).strip():
            continue
        if kind == "thinking":
            if not show_thinking:
                continue
            parts.append('<div class="mono dim">[thinking] %s</div>' % esc(clip(text, 400)))
        elif kind == "toolcall":
            if not show_tools:
                continue
            parts.append('<div class="mono dim"><span class="toolname">%s</span></div>'
                         % esc(clip(text, 240)))
        else:
            parts.append(md(clip(text, 4000)))

    if not parts:
        return False

    if role == "toolResult":
        name = mm.get("toolName") or "result"
        cls = "err" if mm.get("isError") else ""
        return (str(row.get("id") or index),
                '<div class="msg"><span class="role tool">%s</span>'
                '<div class="mono %s">%s</div></div>'
                % (esc(name), cls, esc(clip("\n".join(
                    block_text(b)[1] for b in blocks
                    if isinstance(b, dict) and block_text(b)[0] == "text"), 900))))

    label = {"user": "you", "assistant": "agent"}.get(role, role)
    return (str(row.get("id") or index),
            '<div class="msg"><span class="role %s">%s</span>%s</div>'
            % ("user" if role == "user" else "assistant", esc(label), "".join(parts)))


def render(path: Path, limit: int, show_thinking: bool, show_tools: bool, port: int = 0, dark: bool = False,
           sid: str = "", caps: str = "full", note: str = "", lang: str = ""):
    path_key = str(path) if path else ""
    if path and path.exists():
        rows, total = load_rows(path)
    else:
        rows = []
        total = usage_totals(rows)

    try:
        st = path.stat() if path else None
        version = (st.st_mtime, st.st_size) if st else ()
    except Exception:
        version = ()

    out = []  # list of (key, html)
    for idx, row in enumerate(rows):
        res = row_html(path_key, row, idx, show_thinking, show_tools, version, lang)
        if res:
            out.append(res)

    if limit > 0:
        out = out[-limit:]

    state = agent_state(port)
    busy = bool(state) and state.get("idle") is False
    if busy:
        # The frame is NOT baked into the HTML any more: it used to be part of the page
        # digest, so every tick rewrote the whole card (server round trip + row
        # innerHTML). The client animates a dedicated span locally instead, which keeps
        # the digest stable while the agent works and makes the rate a client constant.
        spin = '<span class="spin">%s</span>' % esc(SPIN[0])
        # only the current turn's card may spin: everything above the last user
        # message belongs to earlier turns
        last_user = -1
        for idx, (_key, html) in enumerate(out):
            if 'class="role user"' in html:
                last_user = idx
        target = -1
        for idx in range(len(out) - 1, last_user, -1):
            if 'class="role assistant">agent</span>' in out[idx][1]:
                target = idx
                break
        if target >= 0:
            key, html = out[target]
            out[target] = (key, html.replace(
                'class="role assistant">agent</span>',
                'class="role assistant">agent %s</span>' % spin, 1))
        else:
            # agent has not produced anything yet this turn: show a fresh card
            out.append(("working", '<div class="msg"><span class="role assistant">agent %s</span></div>'
                        % spin))
    pend = pending_html(sid, port, caps, lang)
    if note:
        # inside the fragment, before the approval block: the reader must see that
        # the conversation changed before they see a button that targets the new one
        out.append(("gone", '<div class="msg"><span class="role tool">%s</span>'
                            '<div class="body dim">%s</div></div>'
                            % (esc(tr("note_label", lang)), esc(note))))
    if pend:
        # end of the list: the page auto-scrolls to the bottom
        out.append(("pending", pend))
    items = [(key, mask_text(html)) for key, html in out]
    frag = "".join(html for _, html in items) or '<div class="body">(no messages yet)</div>'

    cwd = ""
    for row in rows[:1]:
        cwd = row.get("cwd") or ""
    line1 = esc(short_path(cwd)) if cwd else ""
    branch = git_branch(cwd) if cwd else ""
    if branch:
        line1 += " (%s)" % esc(branch)

    # One short line, not three: the reader glances at this, so it keeps only what
    # changes a decision - how much context is left and what it has cost. Dropped:
    # cache-read total, cache-hit ratio, cache-write, and the raw window size - all
    # of them were noise on a 9px line that also had to carry the footer links.
    parts = ["\u2191%s" % human(total["input"]), "\u2193%s" % human(total["output"])]
    if total["cost"]:
        parts.append("$%.3f" % total["cost"] if total["cost"] < 10 else "$%.2f" % total["cost"])
    if state and state.get("context"):
        pct = state["context"].get("percent")
        if pct is not None:
            parts.append("%.0f%%" % pct)

    # The note must describe the session this page SHOWS. `bridge.json` holds whichever
    # pi process registered last, so with two sessions running (a normal day) the page
    # claimed "bridge 未激活" while its own bridge was answering every poll - the
    # buttons worked, the line just lied. `port` is this session's bridge port
    # (resolved in _do_get) and `state` is what that port answered.
    if state is not None:
        bridge_note = ""
    elif port:
        # the session has a registered bridge that is not answering
        bridge_note = tr("bridge_stale", lang)
    else:
        bridge_note = tr("bridge_off", lang)
    if pend:
        bridge_note += tr("pending_note", lang)
    short = ""
    if path:
        # the id from the header row, not the file name: they disagree for sessions
        # written into an existing file, and the picker's links use the header id
        tail = str(session_meta(path).get("id") or "") or path.stem.rsplit("_", 1)[-1]
        short = tail[:8]
    line2 = " ".join(parts) + bridge_note
    status = (line1 + " \u00b7 " + line2) if line1 else line2
    if short:
        status = esc(short) + " \u00b7 " + status
    # hash both: the page repaints only when this changes, and the status line
    # carries the Working spinner
    digest = hashlib.sha1((frag + status).encode("utf-8")).hexdigest()[:12]
    suffix = ("?s=" + quote(sid, safe="")) if sid else ""
    # ?d= is already in the query, so the id needs an &amp;, not a second ?
    more = ("&amp;s=" + quote(sid, safe="")) if sid else ""
    theme_link = ('<a href="/theme?d=0%s">%s</a>' % (more, esc(tr("theme_day", lang))) if dark
                  else '<a href="/theme?d=1%s">%s</a>' % (more, esc(tr("theme_night", lang))))
    reload_link = '<a href="/%s">%s</a>' % (suffix, esc(tr("redraw", lang)))
    picker = ('<a href="/sessions">%s</a> \u00b7 ' % esc(tr("sessions", lang))) if caps == "full" else ""
    # The queue link is shown while a queue exists and while the agent is busy (then
    # there is something to queue *into*), never as a bare "0" on every page.
    # `queued` only exists on a bridge that has the queue: an older build returns a
    # /state without it, and offering the link there would advertise a page that can
    # only answer "unknown action".
    queued = (state or {}).get("queued")
    try:
        queued = int(queued or 0)
    except Exception:
        queued = 0
    queue_link = ""
    if caps == "full" and sid and state is not None and "queued" in state and (queued > 0 or busy):
        queue_link = '<a href="/queue%s">%s</a> \u00b7 ' % (suffix, esc(tr("queue", lang, queued)))
    # target language is the other one: the label doubles as the switch
    lang_link = '<a href="/lang?l=%s%s">%s</a>' % (
        "zh" if lang == "en" else "en",
        more,
        esc(tr("lang_switch", lang)))
    foot = queue_link + picker + reload_link + " \u00b7 " + theme_link + " \u00b7 " + lang_link
    if path:
        # the picker lists every project on this machine: not on a read-only network
        picker = ('<a href="/sessions">%s</a> \u00b7 ' % esc(tr("sessions_short", lang))) if caps == "full" else ""
        foot = queue_link + picker + reload_link + " \u00b7 " + theme_link + " \u00b7 " + lang_link
    return digest, frag, status, foot, items, busy


ACTIONS = ["stop", "continue", "review", "execute", "status"]   # labels live in STRINGS


def cookie_value(value: str = "", ttl: int = COOKIE_MAX_AGE) -> str:
    return "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax" % (
        COOKIE, value or token(), ttl)


def pin_session_value() -> str:
    """Mint the cookie secret for a pin login. Random and per-session: the
    six-digit pin is a convenience factor for the Kindle, and handing out the
    token for it made one lucky guess worth the master credential."""
    now = time.time()
    with _PIN_LOCK:
        for stale in [k for k, at in _PIN_SESSIONS.items() if now - at > COOKIE_MAX_AGE]:
            _PIN_SESSIONS.pop(stale, None)
        if len(_PIN_SESSIONS) > 256:
            _PIN_SESSIONS.pop(min(_PIN_SESSIONS, key=_PIN_SESSIONS.get), None)
        value = secrets.token_hex(16)
        _PIN_SESSIONS[value] = now
    return value


def pin_session_ok(value) -> bool:
    if not value:
        return False
    with _PIN_LOCK:
        at = _PIN_SESSIONS.get(value)
    return at is not None and time.time() - at <= COOKIE_MAX_AGE


# base32 secret; absent = 2FA off. Overridable so the secret can live next to the
# TLS material instead of inside the pi state dir.
TOTP_FILE = Path(os.environ.get("EINK_TOTP_FILE") or (Path.home() / ".pi/eink/totp"))
OTP_COOKIE = "pieink_otp"
# Sessions survive a restart: a viewer restart used to invalidate every 2FA
# session, so each device had to be handed a fresh code by whoever ran the box.
OTP_STORE = Path(os.environ.get("EINK_OTP_STORE") or (Path.home() / ".pi/eink/otp_sessions.json"))
_OTP_SESSIONS: dict = {}
_OTP_LOCK = threading.Lock()
_OTP_CACHE = [None, None]      # (mtime, secret)


def totp_secret() -> str:
    """base32 secret from ~/.pi/eink/totp, or "" when 2FA is not configured.

    Second factor for an internet-facing deployment: the Basic Auth password alone
    must not be enough, because that password is also what a browser remembers for
    us and what a shoulder-surfer can read.
    """
    try:
        st = TOTP_FILE.stat()
        key = (st.st_mtime, st.st_size)
    except Exception:
        return ""
    if _OTP_CACHE[0] == key:
        return _OTP_CACHE[1] or ""
    try:
        secret = TOTP_FILE.read_text().strip().replace(" ", "").upper()
    except Exception:
        return ""
    _OTP_CACHE[0] = key
    _OTP_CACHE[1] = secret
    return secret


def totp_code(secret: str, at: float = 0.0, step: int = 30) -> str:
    """RFC 6238, 6 digits, SHA1 - what every authenticator app shows."""
    if not secret:
        return ""
    counter = int((at or time.time()) // step)
    key = base64.b32decode(secret + "=" * ((8 - len(secret) % 8) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = digest[-1] & 0x0F
    return "%06d" % ((struct.unpack(">I", digest[off:off + 4])[0] & 0x7FFFFFFF) % 1000000)


def totp_verify(code: str) -> bool:
    """+-1 step of drift, constant time. Empty secret never matches."""
    secret = totp_secret()
    if not secret or not code or not code.strip().isdigit():
        return False
    given = code.strip()
    now = time.time()
    for drift in (-1, 0, 1):
        want = totp_code(secret, now + drift * 30)
        if want and hmac.compare_digest(want, given):
            return True
    return False


def _otp_load() -> dict:
    try:
        data = json.loads(OTP_STORE.read_text())
    except Exception:
        return {}
    out = {}
    now = time.time()
    for k, at in (data.items() if isinstance(data, dict) else []):
        try:
            at = float(at)
        except Exception:
            continue
        if now - at <= COOKIE_MAX_AGE:
            out[str(k)] = at
    return out


def _otp_save() -> None:
    try:
        OTP_STORE.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(OTP_STORE) + ".%d.tmp" % os.getpid()
        with open(tmp, "w") as fh:
            json.dump(_OTP_SESSIONS, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, OTP_STORE)
    except Exception:
        pass


def otp_session_value() -> str:
    now = time.time()
    with _OTP_LOCK:
        if not _OTP_SESSIONS:
            _OTP_SESSIONS.update(_otp_load())
        for stale in [k for k, at in _OTP_SESSIONS.items() if now - at > COOKIE_MAX_AGE]:
            _OTP_SESSIONS.pop(stale, None)
        if len(_OTP_SESSIONS) > 256:
            _OTP_SESSIONS.pop(min(_OTP_SESSIONS, key=_OTP_SESSIONS.get), None)
        value = secrets.token_hex(16)
        _OTP_SESSIONS[value] = now
        _otp_save()
    return value


def otp_session_ok(value) -> bool:
    if not value:
        return False
    with _OTP_LOCK:
        at = _OTP_SESSIONS.get(value)
        if at is None:
            # a session minted before the last restart
            _OTP_SESSIONS.update(_otp_load())
            at = _OTP_SESSIONS.get(value)
    return at is not None and time.time() - at <= COOKIE_MAX_AGE


def cookie_refresh(ip: str, current: str = "") -> list:
    """Rolling expiry: a page polled every few seconds must not ask for the pin
    again, but a cookie left on a lost Kindle stops working within a week."""
    now = time.time()
    if now - _COOKIE_REFRESH.get(ip, 0) < 3600:
        return []
    _COOKIE_REFRESH[ip] = now
    # a pin session is refreshed with its own secret: re-issuing the token
    # here would promote a six-digit login to the master credential
    return [("Set-Cookie", cookie_value(current or token()))]


def acts_html(live: bool = True, sid: str = "", caps: str = "full", lang: str = "") -> str:
    # relative URLs: the viewer proxies them, so one port covers actions too
    if not live:
        return '<div class="body dim">%s</div>' % esc(tr("no_bridge_view", lang))
    suffix = ("?s=" + quote(sid, safe="")) if sid else ""
    if caps != "full":
        buttons = "".join(
            '<form method="POST" action="/act/%s%s"><button type="submit">%s</button></form>'
            % (name, suffix, esc(tr(name, lang))) for name in ACTIONS if name in READONLY_ACTIONS)
        if NO_PIN:
            # No pin form at all: rendering one that can never succeed is the "fake
            # control" this project keeps warning about.
            return buttons + '<div class="body dim">%s</div>' % esc(tr("readonly_no_pin", lang))
        return buttons + (
            '<div class="body dim">%s</div>'
            # <input>, not <textarea>: Android WebView capitalises the first letter of
            # a textarea, and one capital letter silently fails the PIN - which then
            # costs a 10 minute lockout.
            '<form method="POST" action="/act/unlock%s" id="sayform">'
            '<input type="text" name="k" inputmode="numeric" maxlength="64" placeholder="PIN" '
            'autocapitalize="off" autocorrect="off" autocomplete="off" spellcheck="false">'
            '<button type="submit">%s</button></form>'
        ) % (esc(tr("readonly_prompt", lang)), suffix, esc(tr("unlock", lang)))
    def form(name: str) -> str:
        return ('<form method="POST" action="/act/%s%s"><button type="submit">%s</button></form>'
                % (name, suffix, esc(tr(name, lang))))

    # Two always-visible buttons, the rest behind a tap: seven buttons filled the whole
    # 511px row and still wrapped the eye around a wall of borders. The hidden ones stay
    # in the markup (display:none), so a no-JS client still has them and read-only
    # networks keep their own, shorter row.
    primary = "".join(form(name) for name in ("stop", "continue"))
    more = "".join(form(name) for name in ACTIONS if name not in ("stop", "continue"))
    more += form("readonly")
    return (primary
            + '<span id="more">' + more + '</span>'
            # U+22EF (midline ellipsis) is NOT in this font: it rendered as an empty box.
            # U+25BC is one of the shapes measured to be present on the device.
            + '<button type="button" id="morebtn">\u25bc</button>'
            + '<form method="POST" action="/act/say%s" id="sayform">'
        '<button type="button" id="hist">\u2191</button>'
        '<textarea name="text" rows="1" maxlength="2000" '
        'placeholder="%s"></textarea>'
        '<button type="submit">%s</button></form>'
    ) % (suffix, esc(tr("say_placeholder", lang)), esc(tr("send", lang)))



def otp_page(sid: str = "", dark: bool = False, err: str = "", lang: str = "") -> str:
    """Six-digit prompt. Shown only after the password/token gates pass, so the second
    factor cannot be reached by anyone who has not already got past the first."""
    fg, bg = ("#fff", "#000") if dark else ("#000", "#fff")
    style = ("font-family:-apple-system,sans-serif;font-size:26px;padding:20px;"
             "background:%s;color:%s" % (bg, fg))
    errhtml = ('<p style="font-size:20px">%s</p>' % esc(err)) if err else ""
    return (
        '<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width">'
        "<style>body{%s} input{font-size:28px;width:60%%;padding:8px;"
        "border:2px solid %s;background:%s;color:%s}"
        " button{font-size:24px;padding:8px 18px;border:2px solid %s;"
        "background:%s;color:%s}</style></head><body>"
        "<p>%s</p>%s"
        '<form method="POST" action="/otp">'
        '<input type="hidden" name="s" value="%s">'
        '<input name="c" inputmode="numeric" maxlength="8" autocomplete="off" '
        'autocapitalize="off" spellcheck="false" placeholder="000000"> '
        '<button type="submit">%s</button></form></body></html>'
        % (style, fg, bg, fg, fg, bg, fg,
           esc(tr("otp_prompt", lang)), errhtml, esc(sid), esc(tr("otp_submit", lang)))
    )


def auth_page(msg: str, dark: bool = False) -> str:
    style = ("font-family:-apple-system,sans-serif;font-size:28px;padding:24px"
             + (";background:#000;color:#fff" if dark else ";background:#fff;color:#000"))
    return ('<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width">'
            "<style>body{%s}</style></head><body><p>%s</p></body></html>" % (style, esc(msg)))


class Handler(BaseHTTPRequestHandler):
    # one thread per connection and no keep-alive: a client that opens a socket
    # and never finishes its request line holds the thread (and its fd) forever
    timeout = 30

    def log_message(self, *a):
        pass

    def _dark(self) -> bool:
        return parse_cookies(self.headers.get("Cookie") or "").get("piedark") == "1"

    def _note_fail(self, ip: str, guess: bool) -> None:
        """Count one failed credential attempt.

        `guess` is True for a ?k= attempt (a deliberate guess) and False for a
        cookie that simply does not match. A browser holding one stale cookie
        repeats it on every poll; counting each repeat locked the reader out of
        the page (and out of the ?k= recovery path) within seconds, so the same
        cookie can only cost one failure per minute.
        """
        now = time.time()
        with _AUTH_LOCK:
            if not guess:
                if now - _LAST_FAIL_COUNTED.get(ip, 0) < 60:
                    return
                _LAST_FAIL_COUNTED[ip] = now
            fails, since = _FAILS.get(ip, (0, now))
            if now - since > 600:
                fails, since = 0, now
            fails += 1
            if fails >= 5:
                _FAILS[ip] = (0, now)
                _BLOCKED[ip] = now + 600
            else:
                _FAILS[ip] = (fails, since)
        save_fails()

    def _clear_fails(self, ip: str) -> None:
        with _AUTH_LOCK:
            # both pops must run: `or` short-circuits, and a lockout always
            # leaves an entry in _FAILS, so the block survived a successful
            # login and kept refusing the weaker credential for ten minutes
            had_fails = _FAILS.pop(ip, None) is not None
            had_block = _BLOCKED.pop(ip, None) is not None
            touched = had_fails or had_block
        if touched:
            save_fails()

    def _send(self, body: bytes, ctype: str, code: int = 200, extra=(), cache: str = "no-store"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # one Cache-Control only: sending no-store plus a max-age made browsers
        # keep re-fetching the favicon (first header wins)
        self.send_header("Cache-Control", cache)
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _otp_gate(self, q) -> bool:
        """Second factor. True when satisfied (or when 2FA is not configured)."""
        if not totp_secret():
            return True
        if otp_session_ok(parse_cookies(self.headers.get("Cookie", "")).get(OTP_COOKIE)):
            return True
        lang = lang_of(self)
        if self.command == "POST":
            self._send(auth_page(tr("otp_needed", lang), self._dark()).encode("utf-8"),
                       "text/html; charset=utf-8", 403)
            return False
        sid = (q.get("s") or [""])[0]
        self._send(otp_page(sid, self._dark(), "", lang).encode("utf-8"),
                   "text/html; charset=utf-8")
        return False

    def _authorized(self, q) -> tuple[bool, list, str]:
        """Return (ok, headers-to-set, reason). Cookie, or ?k=<token> which sets the cookie."""
        extra = []
        ip = client_ip(self)
        # The token is checked before the lockout on purpose: after a token
        # rotation the reader must not be locked out of their own page for ten
        # minutes. Weaker credentials - the 6-digit pin, a stale cookie - wait
        # the block out: compared mid-block, a correct pin was a lucky guess
        # that walked straight in, and the lockout only slowed the guessing.
        lang = lang_of(self)
        ok_ip, why = ip_allowed(ip, local_ip(self))
        if not ok_ip:
            return False, extra, why
        ua = (self.headers.get("User-Agent") or "").lower()
        ua_strict = os.environ.get("EINK_UA_STRICT") == "1"
        allowed_ua = ua_allow_list()
        if allowed_ua and (ua_strict or not is_loopback(ip)) and not any(a in ua for a in allowed_ua):
            if not is_loopback(ip) and ua:
                note_unknown_ua(ip, self.headers.get("User-Agent") or "")
            return False, extra, tr("ua_not_allowed", lang)
        want = token()
        if not want:
            return False, extra, "token file missing or empty"
        have = parse_cookies(self.headers.get("Cookie", "")).get(COOKIE)
        given = (q.get("k") or [""])[0]
        # The TLS terminator (nginx on this machine) injects the token, so the URL a
        # reader bookmarks holds no secret at all. nginx overwrites any client-sent
        # value, and it is the only process that can reach this port.
        hdr = (self.headers.get("X-Eink-Token") or "").strip()
        if hdr and hmac.compare_digest(hdr, want):
            self._clear_fails(ip)
            return True, extra, ""
        if have and have == want:
            self._clear_fails(ip)
            return True, extra + cookie_refresh(ip, have), ""
        if given == want:
            self._clear_fails(ip)
            _COOKIE_REFRESH[ip] = time.time()
            extra.append(("Set-Cookie", cookie_value()))
            return True, extra, ""
        if have and pin_session_ok(have):
            # a pin login holds a session secret, not the token: it stays
            # valid for the same week but is worthless anywhere else
            self._clear_fails(ip)
            return True, extra + cookie_refresh(ip, have), ""

        # A guess is a ?k= attempt or a cookie that does not match. A request with
        # no credential at all is an anonymous probe and must not count, or anyone
        # could lock the real device out by polling it.
        presented = bool(given) or have is not None
        until = _BLOCKED.get(ip, 0)
        blocked = until > time.time()
        # The block is authoritative for everything weaker than the token, so
        # the pin is only compared once it cannot matter: accepting a correct
        # pin mid-block meant five guesses bought ten minutes of unlimited
        # guessing at full speed.
        if not blocked and not NO_PIN and given and given == pin():
            self._clear_fails(ip)
            _COOKIE_REFRESH[ip] = time.time()
            extra.append(("Set-Cookie", cookie_value(pin_session_value())))
            return True, extra, ""
        if presented:
            self._note_fail(ip, guess=bool(given))
        if blocked:
            return False, extra, tr("too_many", lang, max(1, round((until - time.time()) / 60)))
        return False, extra, "bad or missing token"

    def do_POST(self):
        begin_request()
        try:
            self._do_post()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as err:  # never leave a client without a response
            self._fail(err)

    def do_GET(self):
        begin_request()
        try:
            self._do_get()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as err:
            self._fail(err)

    def _fail(self, err: Exception) -> None:
        try:
            # path only, never the query: the credential travels in ?k=, and a
            # traceback into server.log must not archive it
            print("%s ERROR %s %s: %s" % (time.strftime("%H:%M:%S"), urlparse(self.path).path,
                                          type(err).__name__, err), flush=True)
            self._send(("error: %s" % type(err).__name__).encode("utf-8"),
                       "text/plain; charset=utf-8", 500)
        except Exception:
            self.close_connection = True

    def _do_post(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/otp":
            raw_len = (self.headers.get("Content-Length") or "").strip()
            length = int(raw_len) if raw_len.isdigit() else 0
            body = self.rfile.read(length) if 0 < length <= 512 else b""
            form = parse_qs(body.decode("utf-8", "replace"))
            code = (form.get("c") or [""])[0]
            sid = (form.get("s") or [""])[0]
            if totp_verify(code):
                self._clear_fails(client_ip(self))
                cookie = "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax" % (
                    OTP_COOKIE, otp_session_value(), COOKIE_MAX_AGE)
                loc = "/" + (("?s=" + quote(sid, safe="")) if sid else "")
                self._send(b"", "text/html; charset=utf-8", 303,
                           extra=[("Set-Cookie", cookie), ("Location", loc)])
                return
            self._note_fail(client_ip(self), guess=True)
            self._send(otp_page(sid, self._dark(), tr("otp_bad", lang_of(self)),
                                lang_of(self)).encode("utf-8"),
                       "text/html; charset=utf-8", 403)
            return

        if not u.path.startswith("/act/"):
            self._send(b"not found", "text/plain; charset=utf-8", 404)
            return
        # Actions are rare, and a tap that never reaches the server leaves no other
        # trace: the Kindle has no console. server.log is the only place to look.
        # the path is attacker bytes: strip control characters or a terminal
        # tailing server.log could be fed escape sequences
        print("%s POST %s ip=%s ua=%s" % (time.strftime("%H:%M:%S"), _diag_clean(u.path, 300),
                                          client_ip(self), _diag_clean(self.headers.get("User-Agent") or "-", 40)),
              flush=True)
        ok, extra, reason = self._authorized(q)
        if not ok:
            print("%s DENY %s ip=%s reason=%s" % (time.strftime("%H:%M:%S"), _diag_clean(u.path, 300),
                                                  client_ip(self), reason), flush=True)
            self._send(auth_page("denied: %s" % esc(reason), self._dark()).encode("utf-8"),
                       "text/html; charset=utf-8", 403)
            return
        if not self._otp_gate(q):
            return

        raw_len = (self.headers.get("Content-Length") or "").strip()
        try:
            length = int(raw_len) if raw_len.isdigit() else 0
        except ValueError:
            # superscripts and other unicode digits pass str.isdigit() but are
            # not ints; this used to 500 the request instead of ignoring it
            length = 0
        body = self.rfile.read(length) if 0 < length <= 8192 else b""

        ip = client_ip(self)
        caps = caps_for(self)
        action = u.path.rsplit("/", 1)[-1]
        sid_hint = (q.get("s") or [""])[0]
        lang = lang_of(self)
        keep = [(k, q[k][0]) for k in ("n", "think", "tools", "s") if k in q]

        def back(msg: str, status: int = 200) -> None:
            """Answer a viewer-local action: JSON for the page's XHR, a redirect for a
            plain form post. A 3xx with a JSON body would be followed by the XHR and
            the page handler would then parse the HTML it lands on."""
            log_action(ip, caps, action, status, sid_hint)
            if "json" in (self.headers.get("Accept") or "").lower():
                self._send(json.dumps({"ok": status < 400, "msg": msg}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8", status if status >= 400 else 200, extra=extra)
                return
            # no JS: `/?msg=` is a shell whose message list is filled in by script
            self._send(b"", "text/html; charset=utf-8", 303,
                       extra=extra + [("Location", "/static?" + urlencode(keep + [("msg", msg[:60])]))])

        # Both of these belong to the viewer, not to the bridge: they change what
        # this network is allowed to do, they do not touch the agent.
        if action == "unlock":
            given = (parse_qs(body.decode("utf-8", "replace")).get("k") or [""])[0].strip()
            give = given or (q.get("k") or [""])[0]
            # Only the pin unlocks a network. The cookie's value *is* the token, so
            # accepting the token here let anyone who sniffed a cookie upgrade an
            # unknown network to full and write it into trust.json.
            expected = "" if NO_PIN else pin()
            if not give or not expected or give != expected:
                self._note_fail(ip, guess=True)
                back(tr("bad_pin", lang), 403)
                return
            confirm_network(self)
            back(tr("unlocked", lang))
            return
        if action == "readonly":
            state = trust_state()
            state["readonly"] = True
            save_trust(state)
            back(tr("now_readonly", lang))
            return
        if action not in BRIDGE_ACTIONS:
            # The path segment is client-controlled and used to be forwarded
            # verbatim; the bridge re-parses it, so the two must not disagree.
            back(tr("unknown_action", lang), 404)
            return
        if not action_allowed(caps, action):
            back(tr("readonly_need_pin", lang), 403)
            return

        # Routing: follow the session the page names, because that is the session it
        # shows. Every request from a full-capability page carries that id (the
        # viewer pins the resolved one into the poll and the buttons), so two devices
        # can read two projects without tapping each other's stop button - routing
        # from one global "rendered" slot is what made the buttons land in the wrong
        # conversation. It is not a widening of authority: the same credential can
        # open any session with ?s= anyway. On a read-only network ?s= stays ignored
        # (the page may not choose), so the id comes from what the viewer renders.
        post_sid = ""
        if caps == "full" and sid_hint:
            post_sid = sid_hint if session_selected(sid_hint) else ""
            if not post_sid:
                # the page named a session that no longer resolves: refuse instead of
                # quietly steering whichever one the viewer happens to render
                back(tr("session_no_bridge", lang), 502)
                return
        post_sid = post_sid or rendered_session_id()
        if not post_sid:
            # approval requests belong to whichever session parked them: match its pid
            try:
                parked = int(json.loads(PENDING_FILE.read_text()).get("pid") or 0)
            except Exception:
                parked = 0
            for live_sid, info in live_bridges().items():
                if parked and info.get("pid") == parked:
                    post_sid = live_sid
                    break
        port_target = bridge_port_for(post_sid) if post_sid else 0
        if not port_target:
            # Never fall back to BRIDGE_PORT: that is a different process, or nothing
            back(tr("session_no_bridge", lang), 502)
            return
        target_pid = 0
        if action == "new":
            # "+ new session" is only possible inside a running pi process: the bridge
            # asks its command context for one. Its pid is how the replacement session
            # is recognised in the registry a moment later.
            try:
                target_pid = int(live_bridges().get(post_sid, {}).get("pid") or 0)
            except Exception:
                target_pid = 0
        target = "http://127.0.0.1:%d/act/%s" % (port_target, action)
        req = urllib.request.Request(target, data=body, method="POST")
        # forward credentials: the bridge gates on the same cookie
        if self.headers.get("Cookie"):
            # rebuilt from the server's own token: a client Cookie header must not
            # travel to the bridge (or anywhere a redirect might point)
            req.add_header("Cookie", "%s=%s" % (COOKIE, token()))
        req.add_header("User-Agent", self.headers.get("User-Agent") or "")
        # Always ask the bridge for JSON, whatever the browser sent: the outcome is
        # data (status + msg), while the bridge's HTML redirect leads to a /done/
        # page this proxy would have to scrape for the message.
        req.add_header("Accept", "application/json")
        # the bridge answers in the language the page is being read in
        req.add_header("X-Eink-Lang", lang)
        js_client = "json" in (self.headers.get("Accept") or "").lower()
        if body:
            req.add_header("Content-Type",
                           self.headers.get("Content-Type") or "application/x-www-form-urlencoded")
        opener = _bridge_opener()
        status, resp_body, resp_headers = 502, b"", []
        try:
            with opener.open(req, timeout=10) as resp:
                resp_body = resp.read()
                status = 200
                resp_headers = [(k, v) for k, v in resp.getheaders()
                                if k.lower() in ("location", "content-type")]
        except urllib.error.HTTPError as err:
            status, resp_body = err.code, err.read()
            if err.headers.get("Location"):
                resp_headers = [("Location", err.headers["Location"])]
        except Exception as err:
            resp_body = tr("bridge_not_running", lang, err).encode("utf-8")
            status = 502
        log_action(ip, caps, action, status, post_sid)
        if action == "say" and status < 400 and body:
            # Recorded here, not in the browser: the page may be reloaded, or read on
            # another device, and the box's history should still be there.
            try:
                hist_text = (parse_qs(body.decode("utf-8", "replace")).get("text") or [""])[0]
            except Exception:
                hist_text = ""
            history_push(post_sid, hist_text)

        # A plain form post (no JS, or a page cached before the inline handler
        # existed) must not land on the bridge's own page with a manual 返回:
        # bounce it straight back to the session view with the outcome inline.
        payload = {}
        try:
            parsed = json.loads(resp_body.decode("utf-8"))
            if isinstance(parsed, dict):
                payload = parsed
        except Exception:
            payload = {}

        if status == 404 and (action == "new" or action in QUEUE_ACTIONS):
            # the bridge answered "unknown action": it predates this button (a viewer is
            # upgraded by a restart, a bridge only by /reload inside pi)
            payload["msg"] = tr("bridge_old", lang)

        if action == "new" and status < 400 and target_pid:
            # The bridge answers before it is torn down, so the replacement session is
            # not in its reply: wait for it to register under the same pid. Failing
            # that, stay put and say so instead of landing on a page that will hop.
            new_sid = wait_for_new_session(target_pid, post_sid)
            if new_sid:
                payload["sid"] = new_sid
            else:
                payload["msg"] = str(payload.get("msg") or "") + " " + tr("new_session_failed", lang)

        if not js_client:
            what = u.path.rsplit("/", 1)[-1]
            text = str(payload.get("msg") or "").strip()
            if status >= 400 and text:
                text = tr("failed", lang) + text
            if not text:
                # older bridge build: no JSON body to quote
                text = {"approve": tr("allowed", lang), "deny": tr("denied", lang)}.get(what, tr("sent", lang))
                if status >= 400:
                    text = tr("failed_code", lang, status)
            if payload.get("sid"):
                # a session that was just created: land on it, not back on the old one
                loc = "/?" + urlencode([("s", str(payload["sid"])), ("msg", text[:60])])
                self._send(b"", "text/html; charset=utf-8", 303, extra=extra + [("Location", loc)])
                return
            if action in QUEUE_ACTIONS:
                # the reader was on the queue page: come back to it, with the outcome
                loc = "/queue?" + urlencode([("s", post_sid), ("msg", text[:60])])
                self._send(b"", "text/html; charset=utf-8", 303, extra=extra + [("Location", loc)])
                return
            back = "/static?" + urlencode(keep + [("msg", text[:60])])
            self._send(b"", "text/html; charset=utf-8", 303, extra=extra + [("Location", back)])
            return

        if payload:
            # the page's inline handler already speaks this shape
            self._send(json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8", status, extra=extra)
            return
        if 300 <= status < 400 and resp_headers:
            self._send(b"", "text/html; charset=utf-8", 303, extra=extra + resp_headers)
            return
        ctype = dict(resp_headers).get("Content-Type", "text/html; charset=utf-8")
        self._send(resp_body or b"bridge error", ctype, status, extra=extra)

    def _do_get(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/favicon.ico", "/favicon.png"):
            self._send(FAVICON, "image/png", cache="public, max-age=86400")
            return
        requested = (q.get("s") or [""])[0]
        caps = caps_for(self)
        # A bookmark from a trusted network must not 403 every poll on a network that
        # is not unlocked, and a read-only network may not *choose* another project's
        # history. Both drop ?s=; both then fall back to the session this viewer last
        # rendered, so the page stops hopping between running sessions.
        path, sid, gone = resolve_view(requested, caps)
        if not sid and path is not None:
            # nothing requested and nothing remembered (a fresh viewer): pin the id of
            # the session about to be rendered, or this page would re-derive "the"
            # session from view.json on every poll and follow another device's pick
            sid = str(session_meta(path).get("id") or "")
        port = bridge_port_for(sid) if sid else bridge_port_for(rendered_session_id())
        live_here = (sid in live_bridges()) if sid else bool(live_bridges())
        # What every later request from THIS page must carry. A page that does not
        # name its session re-derives "the" session on every poll, and there is only
        # one such slot for the whole viewer: another device browsing, or a new pi
        # session starting on the laptop, then swapped the conversation under the
        # reader. Naming the resolved id in the poll (and in the buttons, which take
        # the same id) makes each page follow its own session; view.json only decides
        # which session a page with no id at all lands on.
        keep = [(k, q[k][0]) for k in ("n", "think", "tools") if k in q]
        if caps == "full" and sid:
            keep.insert(0, ("s", sid))
        limit = qint(q, "n", 12, 1, 200)
        show_thinking = q.get("think", ["0"])[0] == "1"
        show_tools = q.get("tools", ["0"])[0] == "1"
        fs = qint(q, "fs", 18, 10, 48)
        lang = lang_of(self)
        hint = ('<div class="body dim">%s</div>' % esc(tr("readonly_hint", lang))
                if caps != "full" else "")
        # Carried into render() so it lands inside the polled fragment: /data never
        # repaints #acts, so a notice only in the shell would not show up.
        gone_note = tr("gone_notice", lang, gone[:8]) if gone else ""
        dark = parse_cookies(self.headers.get("Cookie") or "").get("piedark") == "1"
        css = (CSS.replace("__FS__", str(fs)) + (DARK_CSS if dark else ""))

        if os.environ.get("EINK_LOG_UA"):
            # log once per (ip, user-agent): the page polls every 2.5s, and logging
            # every request would grow the file by hundreds of lines a minute
            seen = (client_ip(self), self.headers.get("User-Agent", "-"))
            if seen not in _UA_SEEN:
                _UA_SEEN.add(seen)
                print("UA %s %s" % seen, flush=True)

        ok, extra, reason = self._authorized(q)
        if not ok:
            self._send(auth_page("denied: %s" % esc(reason), self._dark()).encode("utf-8"),
                       "text/html; charset=utf-8", 403)
            return
        if not self._otp_gate(q):
            return

        # The only writer of view.json: whatever this page is about to show becomes
        # "the session" a page with no id of its own lands on.
        if u.path == "/" and path is not None:
            remember_sid(sid or str(session_meta(path).get("id") or ""))

        # A page opened by a bare URL (a bookmark, or the address bar after a
        # reboot) gets the resolved id pinned into its address once, so the URL in
        # the bar is the session on screen - and stays that session after a reload.
        # Read-only networks skip this on purpose: ?s= is dropped there, so the
        # redirect would loop.
        if not requested and caps == "full" and u.path == "/" and sid:
            self._send(b"", "text/html; charset=utf-8", 303, extra=extra + [
                ("Location", "/?" + urlencode(keep))])
            return
        if caps != "full" and u.path == "/sessions":
            self._send(auth_page(tr("list_locked", lang), self._dark()).encode("utf-8"),
                       "text/html; charset=utf-8", 403, extra=extra)
            return
        if u.path == "/lang":
            want = (q.get("l") or ["en"])[0]
            if want not in ("zh", "en"):
                want = "en"
            keep_lang = [(k, q[k][0]) for k in ("n", "think", "tools", "s") if k in q]
            back = "/?" + urlencode(keep_lang) if keep_lang else "/"
            self._send(b"", "text/html; charset=utf-8", 303, extra=extra + [
                ("Location", back),
                ("Set-Cookie", "%s=%s; Path=/; Max-Age=31536000; SameSite=Lax" % (LANG_COOKIE, want)),
            ])
            return

        if u.path == "/theme":
            want = (q.get("d") or ["1"])[0] == "1"
            keep_dark = [(k, q[k][0]) for k in ("n", "think", "tools", "s") if k in q]
            back = "/?" + urlencode(keep_dark) if keep_dark else "/"
            self._send(b"", "text/html; charset=utf-8", 303, extra=extra + [
                ("Location", back),
                ("Set-Cookie", "piedark=%d; Path=/; Max-Age=31536000; SameSite=Lax" % (1 if want else 0)),
            ])
            return

        if u.path.startswith("/done/"):
            # rebuilt from validated parts: the bridge only knows /done/<key>?m=
            done_key = quote(u.path[len("/done/"):].split("?", 1)[0], safe="")
            done_msg = (q.get("m") or [""])[0]
            target = "http://127.0.0.1:%d/done/%s?%s" % (bridge_port(), done_key,
                                                         urlencode({"m": done_msg}))
            req = urllib.request.Request(target)
            if self.headers.get("Cookie"):
                # authenticate to the bridge, but never relay an arbitrary client
                # Cookie header onto a hop the viewer did not choose
                req.add_header("Cookie", "%s=%s" % (COOKIE, token()))
            req.add_header("User-Agent", self.headers.get("User-Agent") or "")
            try:
                with _bridge_opener().open(req, timeout=10) as resp:
                    self._send(resp.read(), "text/html; charset=utf-8", extra=extra)
            except Exception:
                self._send(auth_page(tr("bridge_not_running_short", lang), self._dark()).encode("utf-8"),
                           "text/html; charset=utf-8", 502, extra=extra)
            return

        if u.path == "/glyphs":
            groups = [
                (tr("glyphs_braille", lang), "\u280b \u2819 \u2839 \u2838 \u283c \u2834 \u2826 \u2827 \u2807 \u280f"),
                (tr("glyphs_quadrant", lang), "\u25f0 \u25f3 \u25f2 \u25f1"),
                (tr("glyphs_half", lang), "\u25d0 \u25d3 \u25d1 \u25d2"),
                (tr("glyphs_triangle", lang), "\u25e2 \u25e3 \u25e4 \u25e5"),
                ("ASCII", "[|] [/] [-] [\\]"),
            ]
            body = "".join(
                '<div style="margin:14px 0"><span style="font-size:20px">%s</span><br>'
                '<span style="font-size:44px">%s</span></div>' % (esc(name), esc(chars))
                for name, chars in groups)
            page = ("<!DOCTYPE html><html><head>"
                    '<meta name="viewport" content="width=device-width, initial-scale=1">'
                    "<style>body{font-family:sans-serif;background:#fff;color:#000;padding:16px}"
                    "div{border-bottom:1px solid #000}</style></head><body>"
                    "<p>%s</p>%s</body></html>" % (esc(tr("glyphs_prompt", lang)), body))
            self._send(page.encode("utf-8"), "text/html; charset=utf-8", extra=extra)
            return

        if u.path == "/diag":
            # Any query key: the page reports taps, outcomes and JS errors here, since
            # a Kindle has no console. Everything in it is attacker-controlled text,
            # so values are stripped of control characters (a %0A would forge log
            # lines) and one peer can only add a line every few seconds.
            ip = client_ip(self)
            if not _diag_allow(ip):
                self._send(b"slow down", "text/plain; charset=utf-8", 429, extra=extra)
                return
            vals = {}
            for k, v in list(q.items())[:10]:
                vals[_diag_clean(k, 20)] = _diag_clean((v or [""])[0], 60)
            line = "%s\tip=%s\t%s\tua=%s" % (
                time.strftime("%H:%M:%S"), ip,
                " ".join("%s=%s" % (k, v) for k, v in vals.items()),
                _diag_clean(self.headers.get("User-Agent") or "", 60))
            seen = _diag_seen()
            key = line.split("\t", 1)[1]
            if key not in seen:
                seen.add(key)
                append_bounded(DIAG_LOG, line)
            self._send(b"ok", "text/plain; charset=utf-8", extra=extra)
            return

        if u.path == "/hist":
            # The reader's own past commands for this session, newest first. Served
            # from disk so it survives a reload and is identical on every device.
            # Same notion of "the session on screen" as the POST routing below, or the
            # list read back would not be the list the box wrote.
            hist_sid = ((q.get("s") or [""])[0] if caps_for(self) == "full" else "") or rendered_session_id()
            self._send(json.dumps({"items": history_list(hist_sid)}, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8", extra=extra)
            return

        if u.path == "/data":
            digest, frag, status, foot, items, busy = render(path, limit, show_thinking, show_tools, port, dark, sid, caps,
                                                              note=gone_note, lang=lang)
            payload = json.dumps({"v": digest, "html": frag, "status": status, "foot": foot,
                                  "items": [{"k": k, "h": h} for k, h in items],
                                  "busy": busy, "spin": list(SPIN)},
                                 ensure_ascii=False)
            self._send(payload.encode("utf-8"), "application/json; charset=utf-8", extra=extra)
            return


        qs = ("?" + "&".join("%s=%s" % (k, quote(str(v), safe="")) for k, v in keep) + "&") if keep else "?"

        note = (q.get("msg") or [""])[0]
        note_html = ('<div id="note">%s</div>' % esc(note[:40])) if note else ""

        if u.path in ("/queue", "/queue/edit"):
            # The queue belongs to the session this page names (the same id the poll and
            # the buttons carry), and its content comes from that session's bridge: the
            # viewer keeps no queue state of its own.
            suffix = ("?s=" + quote(sid, safe="")) if sid else ""
            got = bridge_queue(port)
            note_msg = (q.get("msg") or [""])[0][:60]
            if u.path == "/queue/edit":
                # the way back from an edit is the queue it came from, not the session
                back_path = "/queue" + suffix
                back_label = "queue_back"
                refresh = ""
            else:
                back_path = ("/?" + urlencode(keep)) if keep else "/"
                back_label = "back_to_session"
                # No meta refresh on this page: it shares the add box with the list, and a
                # reload would wipe what the reader is typing. One tap re-reads instead.
                refresh = (' \u00b7 <a href="/queue%s">%s</a>'
                           % (suffix, esc(tr("queue_refresh", lang))))
            body = ('<div class="body"><a href="%(back_url)s">%(back)s</a>%(refresh)s</div>'
                    '%(note)s%(queue)s')
            if got is None:
                # a bridge that answers /state but not /queue is an older build, not a
                # session that is gone: saying "not running" sends the reader hunting a
                # pi process that is right there
                stale = agent_state(port) is not None
                inner = '<div class="body dim">%s</div>' % esc(
                    tr("queue_stale" if stale else "queue_no_bridge", lang))
            elif u.path == "/queue/edit":
                want = (q.get("i") or [""])[0]
                pos, item = next(((n + 1, x) for n, x in enumerate(got[0])
                                  if str(x.get("i")) == want), (0, None))
                inner = (queue_edit_html(int(item["i"]), str(item.get("text") or ""),
                                         sid, lang, pos=pos)
                         if item else '<div class="body dim">%s</div>' % esc(tr("queue_empty", lang)))
            else:
                inner = queue_html(got[0], got[1], sid, caps, lang, armed=got[2])
            page = ("<!DOCTYPE html><html><head>"
                    '<meta name="viewport" content="width=device-width, initial-scale=1">'
                    '<link rel="icon" type="image/png" href="/favicon.png">'
                    "<style>%(css)s</style></head><body>"
                    '<div id="wrap">%(body)s</div></body></html>')
            self._send((page % {"css": css, "body": body % {
                "back_url": esc(back_path),
                "back": esc(tr(back_label, lang)),
                "refresh": refresh,
                "note": ('<div id="note">%s</div>' % esc(note_msg)) if note_msg else "",
                "queue": inner}}).encode("utf-8"),
                       "text/html; charset=utf-8", extra=extra)
            return

        if u.path == "/sessions":
            # The way back is the session this picker was opened from, not "whatever the
            # viewer rendered last": with two devices in use that is the other one's pick.
            back_path = ("/?" + urlencode(keep)) if keep else "/"
            page = ("<!DOCTYPE html><html><head>"
                    '<meta name="viewport" content="width=device-width, initial-scale=1">'
                    '<link rel="icon" type="image/png" href="/favicon.png">'
                    "<style>%(css)s</style></head><body>"
                    '<div id="wrap"><div class="body"><a href="%(back_url)s">%(back)s</a></div>'
                    "%(list)s</div></body></html>")
            self._send((page % {"css": css, "list": sessions_html(lang),
                                "back_url": esc(back_path),
                                "back": esc(tr("back_to_session", lang))}).encode("utf-8"),
                       "text/html; charset=utf-8", extra=extra)
            return

        if u.path == "/static":
            digest, frag, status, foot, items, busy = render(path, limit, show_thinking, show_tools, port, dark, sid, caps,
                                                              note=gone_note, lang=lang)
            page = ("<!DOCTYPE html><html><head>"
                    '<meta http-equiv="refresh" content="15">'
                    '<meta name="viewport" content="width=device-width, initial-scale=1">'
                    '<link rel="icon" type="image/png" href="/favicon.png">'
                    "<style>%(css)s</style></head><body>"
                    '<div id="top">%(status)s</div><div id="wrap">%(frag)s<div id="acts">%(acts)s</div></div>'
                    '<div id="foot">%(foot)s</div></body></html>')
            self._send((page % {"css": css, "status": status,
                               "acts": note_html + hint + acts_html(live_here, sid, caps, lang),
                               "frag": frag, "foot": foot}).encode("utf-8"),
                       "text/html; charset=utf-8", extra=extra)
            return

        i18n = json.dumps({"sent": tr("sent", lang), "failed": tr("failed", lang),
                           "timeout": tr("js_timeout", lang),
                           "netError": tr("js_net_error", lang),
                           "noHistory": tr("js_no_history", lang)}, ensure_ascii=False)
        self._send((SHELL % {"css": css, "acts": note_html + hint + acts_html(live_here, sid, caps, lang),
                            "qs": qs, "i18n": i18n}).encode("utf-8"),
                   "text/html; charset=utf-8", extra=extra)


def git_head() -> str:
    """Short HEAD of the checkout this file lives in, without spawning git."""
    try:
        root = Path(__file__).resolve().parent.parent
        ref = (root / ".git/HEAD").read_text().strip()
        if ref.startswith("ref:"):
            return (root / ".git" / ref.split(":", 1)[1].strip()).read_text().strip()[:7]
        return ref[:7]
    except Exception:
        return "?"


def main():
    load_fails()
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    print("listening on http://%s:%d/ (file: %s, rev: %s, session: %s)"
          % (BIND, PORT, Path(__file__).resolve(), git_head(), newest_session()), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
