#!/usr/bin/env python3
"""E-ink friendly viewer for a pi session jsonl.

GET /       -> shell page (ES5 JS, polls /data, only repaints when content changes)
GET /data   -> {"v": "<hash>", "html": "<fragment>"}
GET /static -> plain page with meta refresh (JS-less fallback)

Env: PI_SESSION_FILE (default: newest *.jsonl under ~/.pi/agent/sessions)
"""

import hashlib
import html
import ipaddress
import datetime
import json
import os
import re
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
# written by the pi extension: which session and port actually accept actions
STATE_FILE = Path.home() / ".pi/eink/bridge.json"
BRIDGES_FILE = Path.home() / ".pi/eink/bridges.json"  # sessionId -> {port, pid, cwd, sessionFile, updatedAt}
COOKIE = "pieink"
DEFAULT_ALLOW = "127.0.0.1,::1"
_UA_SEEN = set()
_FAILS = {}    # ip -> (wrong guesses, window start)
_BLOCKED = {}  # ip -> unix time until which auth is refused
_COOKIE_REFRESH = {}  # ip -> unix time the cookie was last re-issued
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


def session_path_by_id(session_id: str):
    if not session_id:
        return None
    for path in SESSIONS.rglob("*.jsonl"):
        if session_id[-8:] in path.name:
            return path
    return None


def sessions_html() -> str:
    """Picker: running bridges first, then the most recently used session files."""
    live = live_bridges()
    rows = []
    for sid, info in sorted(live.items(), key=lambda kv: kv[1].get("updatedAt", ""), reverse=True):
        cwd = info.get("cwd") or ""
        rows.append('<div class="sess"><a href="/?s=%s"><b>%s</b></a>'
                    '<div class="mono dim">%s 运行中 :%s</div></div>'
                    % (quote(str(sid), safe=""), esc(short_path(cwd) or sid[:8]),
                       esc(short_path(cwd)), esc(str(info.get("port", "")))))

    files = sorted(SESSIONS.rglob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
    shown = 0
    for path in files:
        meta = session_meta(path)
        sid = meta.get("id") or path.stem.rsplit("_", 1)[-1]
        if sid in live:
            continue
        when = time.strftime("%m-%d %H:%M", time.localtime(path.stat().st_mtime))
        rows.append('<div class="sess"><a href="/?s=%s"><b>%s</b></a>'
                    '<div class="mono dim">%s · %s</div>'
                    '<div class="body">%s</div></div>'
                    % (quote(str(sid), safe=""), esc(when + "  " + (short_path(meta.get("cwd")) or "?")),
                       esc(short_path(meta.get("cwd"))), esc(when), esc(meta.get("first") or "(空)")))
        shown += 1
        if shown >= 25:
            break
    return "".join(rows) or '<div class="body">(没有会话)</div>'


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
    """
    info = session_meta(newest_session("") or Path("/nonexistent"))
    return str(info.get("id") or "")


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
        with ACTION_LOG.open("a") as fh:
            fh.write("%s\tip=%s\tcaps=%s\tsid=%s\taction=%s\tstatus=%d\n" % (
                time.strftime("%Y-%m-%d %H:%M:%S"), ip, caps, sid[:8], action, status))
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


SPIN = ("[-]", "[\\]", "[|]", "[/]")  # ASCII box; braille/quadrant/half-circle are missing from the Kindle font


def agent_state(port: int = 0):
    """Ask the bridge whether the agent is running. None when unavailable."""
    if port <= 0:
        return None  # reading a session that has no bridge: never claim it is busy
    try:
        req = urllib.request.Request("http://127.0.0.1:%d/state" % (port or bridge_port()),
                                     headers={"Cookie": "%s=%s" % (COOKIE, token()),
                                              "User-Agent": "viewer"})
        with urllib.request.urlopen(req, timeout=1.0) as resp:
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


def pending_html(sid: str = "", port: int = 0, caps: str = "full") -> str:
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
        raw_cmd = raw_cmd[:600] + "\n\u2026 \u5df2\u622a\u65ad\uff08\u5171 %d \u5b57\u7b26\uff09" % len(raw_cmd)
    command = esc(raw_cmd)
    return (
        '<div id="pending"><div class="body"><b>\u7b49\u5f85\u786e\u8ba4</b>\uff1a%s</div>'
        "<pre>%s</pre>"
        '<form method="POST" action="/act/approve%s"><button type="submit">\u5141\u8bb8</button></form>'
        '<form method="POST" action="/act/deny%s"><button type="submit">\u62d2\u7edd</button></form>'
        "</div>"
    ) % (reason, command, suffix, suffix)


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
#top {
  position: fixed; top: 0; left: 0; right: 0;
  background: #fff; border-bottom: 1px solid #000;
  padding: 0 8px; font-size: 9px; line-height: 18px; height: 18px;
  white-space: nowrap; overflow: hidden;
}
#wrap { padding: 24px 14px 8px 14px; }
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
li { margin: 2px 0; }
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
  margin: 8px 0 0 0; padding: 3px 0 0 0;
  border-top: 2px solid #000;
  line-height: 1; height: auto;
  white-space: nowrap; overflow: hidden;
}
#acts form { display: inline-block; vertical-align: top; margin: 0 8px 0 0; line-height: 1; }
/* Block layout on purpose: an older WebView drops `display:flex`, and under
   #acts{white-space:nowrap} the send button then landed off-screen. */
#acts #sayform { display: block; margin-top: 6px; white-space: normal; }
#acts #sayform textarea, #acts #sayform input {
  display: block; width: 100%; -webkit-box-sizing: border-box; box-sizing: border-box;
  font-family: inherit; font-size: 14px; line-height: 1.3;
  border: 2px solid #000; padding: 4px 8px; margin: 0;
  background: #fff; color: #000; resize: none;
}
#acts #sayform button { display: block; width: 100%; margin-top: 6px; padding: 8px 12px; }
#acts button, .btn {
  font-family: inherit; font-size: 12px; min-height: 30px;
  background: #fff; color: #000; border: 2px solid #000;
  padding: 2px 10px; margin: 0;
}
.sess { border-bottom: 1px solid #000; padding: 8px 0; }
.sess a { color: #000; }
#note { font-size: 20px; font-weight: bold; padding: 6px 0 0; }
#result { padding: 3px 8px; font-size: 14px; font-weight: bold; color: #000; min-height: 20px; }
#foot {
  padding: 0 8px 3px; margin: 0; font-size: 9px; line-height: 11px;
  color: #777; border-top: 1px solid #bbb;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
"""

# Night mode: the Kindle frontlight plus a white page is painful in the dark.
# Inverted, high-contrast rules appended after the base sheet so they win.
DARK_CSS = """
body { background: #000; color: #fff; }
#top { background: #000; border-bottom: 1px solid #fff; color: #fff; }
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
#acts #sayform textarea { background: #000; color: #fff; border-color: #fff; }
/* Chromium 30 (the Kindle) only knows the -webkit- prefixed pseudo element */
#acts #sayform textarea::-webkit-input-placeholder { color: #eee; }
#acts #sayform textarea::placeholder { color: #eee; }
.sess { border-bottom-color: #fff; }
.sess a { color: #fff; }
#note, #result { color: #fff; }
#foot { color: #999; border-top-color: #555; }
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
<div id="top"><span id="status">loading</span></div>
<div id="wrap"><div id="content"></div><div id="acts">%(acts)s</div></div>
<div id="foot"><span id="foottxt"></span></div>
<script>
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
}
var POLL_MS_BUSY = 1200, POLL_MS_IDLE = 3000, POLL_MS = POLL_MS_IDLE;
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
// one-shot geometry beacon so the server side can see the device's real viewport
(function () {
  try {
    var x = new XMLHttpRequest();
    x.open("GET", "/diag?w=" + window.innerWidth + "&h=" + window.innerHeight +
      "&sw=" + document.body.scrollWidth + "&dpr=" + (window.devicePixelRatio || 1) +
      "&iw=" + document.documentElement.clientWidth, true);
    x.send(null);
  } catch (e) {}
})();
document.addEventListener("submit", function (e) {
  var form = e.target;
  var action = form.getAttribute("action") || "";
  if (action.indexOf("/act/") !== 0) { return; }
  if (new RegExp("/status$").test(action)) { return; }   // 状态 keeps its own page
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
  // button stays disabled and the only way out is the 9px "重绘" link in the footer.
  var guard = setTimeout(function () {
    try { x.abort(); } catch (e) {}
    release();
    showResult("超时，请重试");
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
    var msg = (j && j.msg) ? j.msg : (ok ? "已发送" : "失败（" + (x.status || "网络错误") + "）");
    diag("ev=done&a=" + encodeURIComponent(action) + "&st=" + x.status + "&m=" + encodeURIComponent(msg));
    showResult(msg);
    if (ok) {
      var ta = form.querySelector("textarea");
      if (ta) { ta.value = ""; }
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

var resultTimer = null;
function showResult(msg) {
  // Right below the input: that is where the reader's eye is after tapping 发送.
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
            target = Path(str(info["sessionFile"]))
            if target.exists():
                return target
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
    files = sorted(SESSIONS.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
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


def md(text: str) -> str:
    """Small markdown subset -> html: fences, tables, headings, lists, quotes,
    rules, bold/italic/strike/code/links. Input is raw text."""
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
        return "text", block.get("text", "")
    if t == "thinking":
        return "thinking", block.get("thinking") or block.get("text", "")
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


def row_html(path_key: str, row: dict, index: int, show_thinking: bool, show_tools: bool, version=()):
    """(key, html) for one row, or False when the row renders nothing.

    Cached per row: rows are append-only, so only the tail is new on each poll. The
    (mtime, size) of the file is part of the key, otherwise a file rewritten in
    place keeps serving the HTML of its previous content, digest included.
    """
    # the index is part of the key: rows without an id would otherwise collide
    ck = (path_key, version, row.get("id") or index, row.get("timestamp"), show_thinking, show_tools)
    if ck in ROW_HTML:
        return ROW_HTML[ck]
    if len(ROW_HTML) > 20000:
        ROW_HTML.clear()  # one long-lived viewer across many sessions
    res = build_row(row, index, show_thinking, show_tools)
    ROW_HTML[ck] = res
    return res


def build_row(row: dict, index: int, show_thinking: bool, show_tools: bool):
    if row.get("type") == "compaction":
        # raw history stays in the file after /compact, so mark the boundary:
        # without this the page silently keeps showing pre-compaction messages
        before = row.get("tokensBefore")
        note = "\u4e0a\u4e0b\u6587\u5df2\u538b\u7f29"
        if isinstance(before, int) and before > 0:
            note += "\uff08\u6b64\u524d %s tokens\uff09" % human(before)
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
           sid: str = "", caps: str = "full"):
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
        res = row_html(path_key, row, idx, show_thinking, show_tools, version)
        if res:
            out.append(res)

    if limit > 0:
        out = out[-limit:]

    state = agent_state(port)
    busy = bool(state) and state.get("idle") is False
    if busy:
        # E-ink ghosts a little with every partial repaint, and the frame index is
        # part of the page digest: a 1.2s tick meant a repaint every poll while the
        # agent works. 4.8s keeps the "still running" signal at a quarter the cost.
        frame = SPIN[int(time.time() / 4.8) % len(SPIN)]
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
                'class="role assistant">agent %s</span>' % esc(frame), 1))
        else:
            # agent has not produced anything yet this turn: show a fresh card
            out.append(("working", '<div class="msg"><span class="role assistant">agent %s</span></div>'
                        % esc(frame)))
    pend = pending_html(sid, port, caps)
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

    cache_hit = 0.0
    denom = total["cacheRead"] + total["input"]
    if denom:
        cache_hit = 100.0 * total["cacheRead"] / denom
    parts = ["\u2191%s" % human(total["input"]), "\u2193%s" % human(total["output"]),
             "R%s" % human(total["cacheRead"])]
    if total["cacheWrite"]:
        parts.append("W%s" % human(total["cacheWrite"]))
    parts.append("CH%.1f%%" % cache_hit)
    if total["cost"]:
        parts.append("$%.3f" % total["cost"] if total["cost"] < 10 else "$%.2f" % total["cost"])
    if state and state.get("context") and state["context"].get("window"):
        ctxinfo = state["context"]
        pct = ctxinfo.get("percent")
        parts.append("%s/%s" % ("%.1f%%" % pct if pct is not None else "?", human(ctxinfo["window"])))

    if bridge_state():
        bridge_note = ""
    elif bridge_alive():
        # reachable but not reporting its session: an older extension build, so the
        # page may be showing a different session than the buttons act on
        bridge_note = " / bridge \u65e7\u7248\uff08\u4f1a\u8bdd\u672a\u4e0a\u62a5\uff09"
    else:
        bridge_note = " / bridge \u672a\u6fc0\u6d3b"
    if pend:
        bridge_note += " / \u5f85\u786e\u8ba4"
    line2 = " ".join(parts) + bridge_note
    status = (line1 + " " + line2) if line1 else line2
    # hash both: the page repaints only when this changes, and the status line
    # carries the Working spinner
    digest = hashlib.sha1((frag + status).encode("utf-8")).hexdigest()[:12]
    theme_link = ('<a href="/theme?d=0">\u65e5\u95f4</a>' if dark
                  else '<a href="/theme?d=1">\u591c\u95f4</a>')
    reload_link = '<a href="/%s">\u91cd\u7ed8</a>' % (("?s=" + quote(sid, safe="")) if sid else "")
    picker = '<a href="/sessions">\u4f1a\u8bdd\u5217\u8868</a> \u00b7 ' if caps == "full" else ""
    foot = picker + reload_link + " \u00b7 " + theme_link
    if path:
        stem = path.stem
        short = stem.rsplit("_", 1)[-1][:8] if "_" in stem else stem[-8:]
        # the picker lists every project on this machine: not on a read-only network
        picker = '<a href="/sessions">\u4f1a\u8bdd</a> \u00b7 ' if caps == "full" else ""
        foot = (esc(short) + " \u00b7 " + picker + reload_link + " \u00b7 " + theme_link)
    return digest, frag, status, foot, items, busy


ACTIONS = [
    ("stop", "\u505c\u4e0b"),
    ("continue", "\u7ee7\u7eed"),
    ("review", "\u5ba1\u67e5"),
    ("execute", "\u6267\u884c"),
    ("status", "\u72b6\u6001"),
]


def cookie_value(ttl: int = COOKIE_MAX_AGE) -> str:
    return "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax" % (COOKIE, token(), ttl)


def cookie_refresh(ip: str) -> list:
    """Rolling expiry: a page polled every few seconds must not ask for the pin
    again, but a cookie left on a lost Kindle stops working within a week."""
    now = time.time()
    if now - _COOKIE_REFRESH.get(ip, 0) < 3600:
        return []
    _COOKIE_REFRESH[ip] = now
    return [("Set-Cookie", cookie_value())]


def acts_html(live: bool = True, sid: str = "", caps: str = "full") -> str:
    # relative URLs: the viewer proxies them, so one port covers actions too
    if not live:
        return ('<div class="body dim">该会话未在运行，只能查看（没有可用的动作按钮）</div>')
    suffix = ("?s=" + quote(sid, safe="")) if sid else ""
    if caps != "full":
        buttons = "".join(
            '<form method="POST" action="/act/%s%s"><button type="submit">%s</button></form>'
            % (name, suffix, label) for name, label in ACTIONS if name in READONLY_ACTIONS)
        return buttons + (
            '<div class="body dim">此网络为只读。输入 PIN 才能打字或执行：</div>'
            # <input>, not <textarea>: Android WebView capitalises the first letter of
            # a textarea, and one capital letter silently fails the PIN - which then
            # costs a 10 minute lockout.
            '<form method="POST" action="/act/unlock%s" id="sayform">'
            '<input type="text" name="k" inputmode="numeric" maxlength="64" placeholder="PIN" '
            'autocapitalize="off" autocorrect="off" autocomplete="off" spellcheck="false">'
            '<button type="submit">\u89e3\u9501</button></form>' % suffix
        )
    buttons = "".join('<form method="POST" action="/act/%s%s"><button type="submit">%s</button></form>'
                      % (name, suffix, label) for name, label in ACTIONS)
    return buttons + (
        '<form method="POST" action="/act/readonly%s"><button type="submit">\u53ea\u8bfb</button></form>'
        '<form method="POST" action="/act/say%s" id="sayform">'
        '<textarea name="text" rows="1" maxlength="2000" '
        'placeholder="\u8f93\u5165\u6307\u4ee4\uff0c\u56de\u8f66\u53d1\u9001"></textarea>'
        '<button type="submit">\u53d1\u9001</button></form>' % (suffix, suffix)
    )


def auth_page(msg: str, dark: bool = False) -> str:
    style = ("font-family:-apple-system,sans-serif;font-size:28px;padding:24px"
             + (";background:#000;color:#fff" if dark else ";background:#fff;color:#000"))
    return ('<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width">'
            "<style>body{%s}</style></head><body><p>%s</p></body></html>" % (style, esc(msg)))


class Handler(BaseHTTPRequestHandler):
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
            touched = _FAILS.pop(ip, None) is not None or _BLOCKED.pop(ip, None) is not None
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

    def _authorized(self, q) -> tuple[bool, list, str]:
        """Return (ok, headers-to-set, reason). Cookie, or ?k=<token> which sets the cookie."""
        extra = []
        ip = client_ip(self)
        # Credentials are checked before the lockout, so a correct token/pin always
        # recovers: locking the door in front of the key was how a token rotation
        # locked the reader out for ten minutes.
        ok_ip, why = ip_allowed(ip, local_ip(self))
        if not ok_ip:
            return False, extra, why
        ua = (self.headers.get("User-Agent") or "").lower()
        ua_strict = os.environ.get("EINK_UA_STRICT") == "1"
        allowed_ua = ua_allow_list()
        if allowed_ua and (ua_strict or not is_loopback(ip)) and not any(a in ua for a in allowed_ua):
            if not is_loopback(ip) and ua:
                note_unknown_ua(ip, self.headers.get("User-Agent") or "")
            return False, extra, "ua not allowed（已记录，可加入 ~/.pi/eink/uas）"
        want = token()
        if not want:
            return False, extra, "token file missing or empty"
        if parse_cookies(self.headers.get("Cookie", "")).get(COOKIE) == want:
            self._clear_fails(ip)
            return True, extra + cookie_refresh(ip), ""
        given = (q.get("k") or [""])[0]
        if given and given in (want, pin()):
            self._clear_fails(ip)
            _COOKIE_REFRESH[ip] = time.time()
            extra.append(("Set-Cookie", cookie_value()))
            return True, extra, ""

        # A guess is a ?k= attempt or a cookie that does not match. A request with
        # no credential at all is an anonymous probe and must not count, or anyone
        # could lock the real device out by polling it.
        presented = bool(given) or COOKIE in parse_cookies(self.headers.get("Cookie", ""))
        if presented:
            self._note_fail(ip, guess=bool(given))
        until = _BLOCKED.get(ip, 0)
        if until > time.time():
            return False, extra, "错误次数过多，%d 分钟后再试" % max(1, round((until - time.time()) / 60))
        return False, extra, "bad or missing token"

    def do_POST(self):
        try:
            self._do_post()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as err:  # never leave a client without a response
            self._fail(err)

    def do_GET(self):
        try:
            self._do_get()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as err:
            self._fail(err)

    def _fail(self, err: Exception) -> None:
        try:
            print("%s ERROR %s %s: %s" % (time.strftime("%H:%M:%S"), self.path,
                                          type(err).__name__, err), flush=True)
            self._send(("error: %s" % type(err).__name__).encode("utf-8"),
                       "text/plain; charset=utf-8", 500)
        except Exception:
            self.close_connection = True

    def _do_post(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if not u.path.startswith("/act/"):
            self._send(b"not found", "text/plain; charset=utf-8", 404)
            return
        # Actions are rare, and a tap that never reaches the server leaves no other
        # trace: the Kindle has no console. server.log is the only place to look.
        print("%s POST %s ip=%s ua=%s" % (time.strftime("%H:%M:%S"), u.path,
                                          client_ip(self), (self.headers.get("User-Agent") or "-")[:40]),
              flush=True)
        ok, extra, reason = self._authorized(q)
        if not ok:
            print("%s DENY %s ip=%s reason=%s" % (time.strftime("%H:%M:%S"), u.path,
                                                  client_ip(self), reason), flush=True)
            self._send(auth_page("denied: %s" % esc(reason), self._dark()).encode("utf-8"),
                       "text/html; charset=utf-8", 403)
            return

        raw_len = (self.headers.get("Content-Length") or "").strip()
        length = int(raw_len) if raw_len.isdigit() else 0
        body = self.rfile.read(length) if 0 < length <= 8192 else b""

        ip = client_ip(self)
        caps = caps_for(self)
        action = u.path.rsplit("/", 1)[-1]
        sid_hint = (q.get("s") or [""])[0]
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
            expected = pin()
            if not give or not expected or give != expected:
                self._note_fail(ip, guess=True)
                back("PIN 不对", 403)
                return
            confirm_network(self)
            back("\u5df2\u5728\u8fd9\u4e2a\u7f51\u7edc\u89e3\u9501\uff0c\u53ef\u4ee5\u6253\u5b57\u4e86")
            return
        if action == "readonly":
            state = trust_state()
            state["readonly"] = True
            save_trust(state)
            back("\u5df2\u5207\u4e3a\u53ea\u8bfb")
            return
        if not action_allowed(caps, action):
            back("\u672c\u7f51\u7edc\u53ea\u8bfb\uff08\u672a\u89e3\u9501\uff09\uff1a\u8f93\u5165 PIN \u624d\u80fd\u6253\u5b57\u6216\u6267\u884c", 403)
            return

        # Routing only: on a read-only network the page must not *choose* another
        # session's history, but its stop/status still has to reach the session it
        # displays, so the id is kept for routing and never used to pick a view.
        # Route to the session the page displays. In read-only mode ?s= is ignored
        # entirely (no choosing another session's history), so the id comes from the
        # rendered page rather than from the request.
        post_sid = (sid_hint if caps == "full" else "") or rendered_session_id()
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
            back("该会话未在运行（没有可用的 bridge）", 502)
            return
        target = "http://127.0.0.1:%d%s" % (port_target, u.path)
        req = urllib.request.Request(target, data=body, method="POST")
        # forward credentials: the bridge gates on the same cookie
        if self.headers.get("Cookie"):
            req.add_header("Cookie", self.headers["Cookie"])
        req.add_header("User-Agent", self.headers.get("User-Agent") or "")
        # Always ask the bridge for JSON, whatever the browser sent: the outcome is
        # data (status + msg), while the bridge's HTML redirect leads to a /done/
        # page this proxy would have to scrape for the message.
        req.add_header("Accept", "application/json")
        js_client = "json" in (self.headers.get("Accept") or "").lower()
        if body:
            req.add_header("Content-Type",
                           self.headers.get("Content-Type") or "application/x-www-form-urlencoded")
        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None  # surface the 3xx as HTTPError so we can relay it

        opener = urllib.request.build_opener(_NoRedirect)
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
            resp_body = ("bridge 未激活：%s" % err).encode("utf-8")
            status = 502
        log_action(ip, caps, action, status, post_sid)

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

        if not js_client:
            what = u.path.rsplit("/", 1)[-1]
            text = str(payload.get("msg") or "").strip()
            if status >= 400 and text:
                text = "失败：" + text
            if not text:
                # older bridge build: no JSON body to quote
                text = {"approve": "已允许", "deny": "已拒绝"}.get(what, "已发送")
                if status >= 400:
                    text = "失败（%d）" % status
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
        sid = (q.get("s") or [""])[0]
        caps = caps_for(self)
        if caps != "full" and sid:
            # A bookmark from a trusted network must not 403 every poll on a network
            # that is not unlocked: show the default session instead. What read-only
            # mode must not allow is *choosing* another project's history.
            sid = ""
        port = bridge_port_for(sid) if sid else bridge_port_for(rendered_session_id())
        live_here = (sid in live_bridges()) if sid else bool(live_bridges())
        keep = [(k, q[k][0]) for k in ("n", "think", "tools", "s") if k in q]
        limit = qint(q, "n", 12, 1, 200)
        show_thinking = q.get("think", ["0"])[0] == "1"
        show_tools = q.get("tools", ["0"])[0] == "1"
        fs = qint(q, "fs", 18, 10, 48)
        hint = ('<div class="body dim">\u8be5\u7f51\u7edc\u53ea\u8bfb\uff1a\u53ea\u80fd\u67e5\u770b\u4e0e\u505c\u4e0b\u3002</div>'
                if caps != "full" else "")
        dark = parse_cookies(self.headers.get("Cookie") or "").get("piedark") == "1"
        css = (CSS.replace("__FS__", str(fs)) + (DARK_CSS if dark else ""))

        path = newest_session(sid)

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
        if caps != "full" and u.path == "/sessions":
            self._send(auth_page("只读网络：会话列表要先解锁（页面上输入 PIN）", self._dark()).encode("utf-8"),
                       "text/html; charset=utf-8", 403, extra=extra)
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
            target = "http://127.0.0.1:%d%s" % (bridge_port(), self.path)
            req = urllib.request.Request(target)
            if self.headers.get("Cookie"):
                req.add_header("Cookie", self.headers["Cookie"])
            req.add_header("User-Agent", self.headers.get("User-Agent") or "")
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    self._send(resp.read(), "text/html; charset=utf-8", extra=extra)
            except Exception:
                self._send(auth_page("bridge 未激活", self._dark()).encode("utf-8"),
                           "text/html; charset=utf-8", 502, extra=extra)
            return

        if u.path == "/glyphs":
            groups = [
                ("\u76f2\u6587 (pi TUI)", "\u280b \u2819 \u2839 \u2838 \u283c \u2834 \u2826 \u2827 \u2807 \u280f"),
                ("\u56db\u8c61\u9650", "\u25f0 \u25f3 \u25f2 \u25f1"),
                ("\u534a\u5706", "\u25d0 \u25d3 \u25d1 \u25d2"),
                ("\u4e09\u89d2\u5f69", "\u25e2 \u25e3 \u25e4 \u25e5"),
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
                    "<p>\u54ea\u4e9b\u884c\u663e\u793a\u6b63\u5e38\uff1a</p>%s</body></html>" % body)
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

        if u.path == "/data":
            digest, frag, status, foot, items, busy = render(path, limit, show_thinking, show_tools, port, dark, sid, caps)
            payload = json.dumps({"v": digest, "html": frag, "status": status, "foot": foot,
                                  "items": [{"k": k, "h": h} for k, h in items],
                                  "busy": busy},
                                 ensure_ascii=False)
            self._send(payload.encode("utf-8"), "application/json; charset=utf-8", extra=extra)
            return


        qs = ("?" + "&".join("%s=%s" % (k, quote(str(v), safe="")) for k, v in keep) + "&") if keep else "?"

        note = (q.get("msg") or [""])[0]
        note_html = ('<div id="note">%s</div>' % esc(note[:40])) if note else ""

        if u.path == "/sessions":
            page = ("<!DOCTYPE html><html><head>"
                    '<meta name="viewport" content="width=device-width, initial-scale=1">'
                    '<link rel="icon" type="image/png" href="/favicon.png">'
                    "<style>%(css)s</style></head><body>"
                    '<div id="wrap"><div class="body"><a href="/">返回当前会话</a></div>'
                    "%(list)s</div></body></html>")
            self._send((page % {"css": css, "list": sessions_html()}).encode("utf-8"),
                       "text/html; charset=utf-8", extra=extra)
            return

        if u.path == "/static":
            digest, frag, status, foot, items, busy = render(path, limit, show_thinking, show_tools, port, dark, sid, caps)
            page = ("<!DOCTYPE html><html><head>"
                    '<meta http-equiv="refresh" content="15">'
                    '<meta name="viewport" content="width=device-width, initial-scale=1">'
                    '<link rel="icon" type="image/png" href="/favicon.png">'
                    "<style>%(css)s</style></head><body>"
                    '<div id="top">%(status)s</div><div id="wrap">%(frag)s<div id="acts">%(acts)s</div></div>'
                    '<div id="foot">%(foot)s</div></body></html>')
            self._send((page % {"css": css, "status": status,
                               "acts": note_html + hint + acts_html(live_here, sid, caps),
                               "frag": frag, "foot": foot}).encode("utf-8"),
                       "text/html; charset=utf-8", extra=extra)
            return

        self._send((SHELL % {"css": css, "acts": note_html + hint + acts_html(live_here, sid, caps),
                            "qs": qs}).encode("utf-8"),
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
