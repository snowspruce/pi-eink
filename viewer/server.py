#!/usr/bin/env python3
"""E-ink friendly viewer for a pi session jsonl.

GET /       -> shell page (ES5 JS, polls /data, only repaints when content changes)
GET /data   -> {"v": "<hash>", "html": "<fragment>"}
GET /static -> plain page with meta refresh (JS-less fallback)

Env: PI_SESSION_FILE (default: newest *.jsonl under ~/.pi/agent/sessions)
"""

import hashlib
import html
import json
import os
import re
import socket
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, parse_qs, urlparse

PORT = int(os.environ.get("EINK_PORT", "8000"))
BRIDGE_PORT = int(os.environ.get("EINK_BRIDGE_PORT", "8001"))
SESSIONS = Path.home() / ".pi/agent/sessions"
TOKEN_FILE = Path.home() / ".pi/eink/token"
MASK_FILE = Path.home() / ".pi/eink/masked_secrets"
COOKIE = "pieink"
DEFAULT_ALLOW = "192.168.3.25,192.168.3.45,127.0.0.1,::1"
ALLOW = {x.strip() for x in os.environ.get("EINK_ALLOW_IPS", DEFAULT_ALLOW).split(",") if x.strip()}
_UA_SEEN = set()
# Devices allowed to use the page. Kindle Voyage's browser reports "Kindle Build/4.0-rc2".
# A UA is trivially spoofable: this is a speed bump in front of the token, not a boundary.
UA_ALLOW = [u.strip().lower() for u in
            os.environ.get("EINK_ALLOW_UA", "Kindle Build/").split(",") if u.strip()]


def token() -> str:
    try:
        return TOKEN_FILE.read_text().strip()
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


def bridge_alive() -> bool:
    """The action buttons live on a port owned by the pi extension. When that
    extension is not bound, buttons silently fail; surface it instead."""
    try:
        with socket.create_connection(("127.0.0.1", BRIDGE_PORT), timeout=0.4):
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
ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")

CSS = """
* { box-sizing: border-box; }
body {
  margin: 0; padding: 0;
  background: #fff; color: #000;
  font-family: -apple-system, "Helvetica Neue", Helvetica, Arial, sans-serif;
  font-size: __FS__px; line-height: 1.45;
  -webkit-text-size-adjust: 100%;
}
#top {
  position: fixed; top: 0; left: 0; right: 0; height: 22px;
  background: #fff; border-bottom: 1px solid #000;
  padding: 0 8px; font-size: 10px; line-height: 21px;
  white-space: nowrap; overflow: hidden;
}
#wrap { padding: 28px 14px 20px 14px; }
.msg { margin: 0 0 26px 0; }
.role {
  font-size: 0.68em; letter-spacing: 1px; text-transform: uppercase;
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
#acts {
  margin: 12px 0 0 0; padding: 4px 0 0 0;
  border-top: 2px solid #000;
  line-height: 1; height: auto;
  white-space: nowrap; overflow: hidden;
}
#acts form { display: inline-block; vertical-align: top; margin: 0 8px 0 0; line-height: 1; }
#acts button, .btn {
  font-family: inherit; font-size: 0.62em;
  background: #fff; color: #000; border: 2px solid #000;
  padding: 2px 10px; margin: 0;
}
#foot {
  padding: 0 8px 2px; margin: 0; font-size: 9px; line-height: 11px;
  color: #777; border-top: 1px solid #bbb;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
"""

SHELL = """<!DOCTYPE html>
<html><head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Type" content="text/html; charset=utf-8">
<title>pi / eink</title>
<style>%(css)s</style>
</head><body>
<div id="top"><span id="status">loading</span></div>
<div id="wrap"><div id="content"></div><div id="acts">%(acts)s</div></div>
<div id="foot"><span id="foottxt"></span></div>
<script>
var V = "";
function paint(j) {
  if (j.v === V) { return; }
  V = j.v;
  document.getElementById("content").innerHTML = j.html;
  document.getElementById("status").innerHTML = j.status;
  document.getElementById("foottxt").innerHTML = j.foot;
  window.scrollTo(0, document.body.scrollHeight);
}
function poll() {
  var x = new XMLHttpRequest();
  x.open("GET", "/data%(qs)st=" + (new Date().getTime()), true);
  x.onreadystatechange = function () {
    if (x.readyState === 4) {
      if (x.status === 200) {
        try { paint(JSON.parse(x.responseText)); }
        catch (e) { document.getElementById("status").innerHTML = "parse error"; }
      } else {
        document.getElementById("status").innerHTML = "offline";
      }
      setTimeout(poll, 2500);
    }
  };
  x.send(null);
}
poll();
</script>
</body></html>
"""


def newest_session() -> Path | None:
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


def render(path: Path, limit: int, show_thinking: bool, show_tools: bool):
    rows = []
    if path and path.exists():
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue

    msgs = [r for r in rows if r.get("type") == "message"]

    out = []
    for row in msgs:
        mm = row.get("message") or {}
        role = mm.get("role") or "?"
        if role == "system":
            continue
        if role == "toolResult" and not show_tools:
            continue
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
            continue

        if role == "toolResult":
            if not show_tools:
                continue
            name = mm.get("toolName") or "result"
            cls = "err" if mm.get("isError") else ""
            out.append('<div class="msg"><span class="role tool">%s</span>'
                       '<div class="mono %s">%s</div></div>'
                       % (esc(name), cls, esc(clip("\n".join(
                           block_text(b)[1] for b in blocks
                           if isinstance(b, dict) and block_text(b)[0] == "text"), 900))))
            continue

        label = {"user": "you", "assistant": "agent"}.get(role, role)
        out.append('<div class="msg"><span class="role %s">%s</span>%s</div>'
                   % ("user" if role == "user" else "assistant", esc(label), "".join(parts)))

    if limit > 0:
        out = out[-limit:]
    frag = mask_text("".join(out) or '<div class="body">(no messages yet)</div>')
    digest = hashlib.sha1(frag.encode("utf-8")).hexdigest()[:12]
    stamp = time.strftime("%H:%M:%S")
    visible = len(out)
    status = "pi session / %d msgs / %s%s" % (
        visible, stamp, "" if bridge_alive() else " / bridge \u672a\u6fc0\u6d3b")
    foot = ""
    if path:
        stem = path.stem
        short = stem.rsplit("_", 1)[-1][:8] if "_" in stem else stem[-8:]
        foot = short
    return digest, frag, status, foot


ACTIONS = [
    ("stop", "\u505c\u4e0b"),
    ("continue", "\u7ee7\u7eed"),
    ("review", "\u5ba1\u67e5"),
    ("execute", "\u6267\u884c"),
    ("status", "\u72b6\u6001"),
]


HOST_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]{0,252}$")


def safe_host(handler) -> str:
    """Host to advertise in action URLs: the local address the client reached,
    never the client-supplied Host header."""
    try:
        ip = handler.request.getsockname()[0]
    except Exception:
        ip = ""
    if ip and HOST_OK.match(ip):
        return ip
    fallback = (handler.headers.get("Host") or "").rsplit(":", 1)[0]
    return fallback if HOST_OK.match(fallback) else "127.0.0.1"


def acts_html(host: str) -> str:
    base = "http://%s:%d/act/" % (esc(host), BRIDGE_PORT)
    return "".join('<form method="POST" action="%s%s"><button type="submit">%s</button></form>'
                   % (base, name, label) for name, label in ACTIONS)


def auth_page(msg: str) -> str:
    return ('<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width">'
            '<style>body{font-family:-apple-system,sans-serif;font-size:28px;padding:24px}</style>'
            "</head><body><p>%s</p></body></html>" % esc(msg))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body: bytes, ctype: str, code: int = 200, extra=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self, q) -> tuple[bool, list, str]:
        """Return (ok, headers-to-set, reason). Cookie, or ?k=<token> which sets the cookie."""
        extra = []
        ip = client_ip(self)
        # Empty allowlist must deny, never allow. (`if ALLOW and ...` was fail-open.)
        if not ALLOW or ip not in ALLOW:
            return False, extra, "ip %s not allowed" % (ip or "unknown")
        ua = (self.headers.get("User-Agent") or "").lower()
        if UA_ALLOW and not is_loopback(ip) and not any(a in ua for a in UA_ALLOW):
            return False, extra, "ua not allowed"
        want = token()
        if not want:
            return False, extra, "token file missing or empty"
        if parse_cookies(self.headers.get("Cookie", "")).get(COOKIE) == want:
            return True, extra, ""
        given = (q.get("k") or [""])[0]
        if given and given == want:
            extra.append(("Set-Cookie",
                          "%s=%s; Path=/; Max-Age=2592000; HttpOnly; SameSite=Lax" % (COOKIE, want)))
            return True, extra, ""
        return False, extra, "bad or missing token"

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        limit = int(q.get("n", ["12"])[0] or 12)
        show_thinking = q.get("think", ["0"])[0] == "1"
        show_tools = q.get("tools", ["0"])[0] == "1"
        fs = int(q.get("fs", ["24"])[0] or 24)
        css = CSS.replace("__FS__", str(fs))
        path = newest_session()

        if os.environ.get("EINK_LOG_UA"):
            # log once per (ip, user-agent): the page polls every 2.5s, and logging
            # every request would grow the file by hundreds of lines a minute
            seen = (client_ip(self), self.headers.get("User-Agent", "-"))
            if seen not in _UA_SEEN:
                _UA_SEEN.add(seen)
                print("UA %s %s" % seen, flush=True)

        ok, extra, reason = self._authorized(q)
        if not ok:
            self._send(auth_page("denied: %s" % esc(reason)).encode("utf-8"),
                       "text/html; charset=utf-8", 403)
            return

        if u.path == "/data":
            digest, frag, status, foot = render(path, limit, show_thinking, show_tools)
            payload = json.dumps({"v": digest, "html": frag, "status": status, "foot": foot},
                                 ensure_ascii=False)
            self._send(payload.encode("utf-8"), "application/json; charset=utf-8", extra=extra)
            return

        host = safe_host(self)
        keep = [(k, q[k][0]) for k in ("n", "think", "tools") if k in q]
        qs = ("?" + "&".join("%s=%s" % (k, quote(str(v), safe="")) for k, v in keep) + "&") if keep else "?"

        if u.path == "/static":
            digest, frag, status, foot = render(path, limit, show_thinking, show_tools)
            page = ("<!DOCTYPE html><html><head>"
                    '<meta http-equiv="refresh" content="15">'
                    '<meta name="viewport" content="width=device-width, initial-scale=1">'
                    "<style>%(css)s</style></head><body>"
                    '<div id="top">%(status)s</div><div id="wrap">%(frag)s<div id="acts">%(acts)s</div></div>'
                    '<div id="foot">%(foot)s</div></body></html>')
            self._send((page % {"css": css, "status": esc(status), "acts": acts_html(host),
                               "frag": frag, "foot": esc(foot)}).encode("utf-8"),
                       "text/html; charset=utf-8", extra=extra)
            return

        self._send((SHELL % {"css": css, "acts": acts_html(host), "qs": qs}).encode("utf-8"),
                   "text/html; charset=utf-8", extra=extra)


def main():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("listening on http://0.0.0.0:%d/ (session: %s)" % (PORT, newest_session()), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
