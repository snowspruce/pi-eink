#!/usr/bin/env bash
# One-command install for pi-eink (macOS launchd / Linux systemd --user).
#
#   ./install.sh                     # state dir, secrets, pi package, service unit
#   ./install.sh --port 8080         # different viewer port
#   ./install.sh --port 8080 --label com.me.eink   # custom port and launchd label
#   ./install.sh --no-service        # everything except the service unit
#   ./install.sh --no-pi             # skip `pi install` (loose-file setups)
#   ./install.sh --ua-strict         # also require a whitelisted UA on loopback
#   ./install.sh --uninstall         # stop the service, remove the unit, keep state
#   ./install.sh --uninstall --keep-pi # ... and leave the pi package registered
#
# Re-running is safe: an existing token/pin is never regenerated (that would log
# every device out), and the service unit is reinstalled in place.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="$HOME/.pi/eink"
PORT=8000
LABEL="com.$(whoami).pi-eink"
DO_PI=1
DO_SERVICE=1
KEEP_PI=0
UNINSTALL=0
UA_STRICT=""   # kept from an existing unit unless --ua-strict/--no-ua-strict

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
	case "$1" in
		--port) [ $# -ge 2 ] || die "--port needs a value"; PORT="$2"; PORT_SET=1; shift 2 ;;
		--label) [ $# -ge 2 ] || die "--label needs a value"; LABEL="$2"; shift 2 ;;
		--no-pi) DO_PI=0; shift ;;
		--keep-pi) KEEP_PI=1; shift ;;
		--no-service) DO_SERVICE=0; shift ;;
		--uninstall) UNINSTALL=1; shift ;;
		--ua-strict) UA_STRICT=1; shift ;;
		--no-ua-strict) UA_STRICT=0; shift ;;
		-h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
		*) echo "unknown option: $1" >&2; exit 2 ;;
	esac
done

# Both land in a service unit verbatim: a bad value would only show up as a
# restart loop later.
case "$PORT" in
	''|*[!0-9]*) die "--port must be a number, got '$PORT'" ;;
esac
[ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || die "--port out of range: $PORT"
case "$LABEL" in
	*[!A-Za-z0-9._-]*|'') die "--label may only contain letters, digits, '.', '_', '-': '$LABEL'" ;;
esac

PY="$(command -v python3 || true)"
[ -n "$PY" ] || die "python3 not found (the viewer needs the standard library only)"

OS="$(uname -s)"
case "$OS" in
	Darwin) PLIST="$HOME/Library/LaunchAgents/$LABEL.plist" ;;
	Linux) UNIT="$HOME/.config/systemd/user/pi-eink.service" ;;
	*) die "unsupported OS: $OS (macOS and Linux only)" ;;
esac

# An install records the label it used: `--uninstall` must not need the operator to
# remember it, and a custom label was silently left running before.
META="$STATE/install-meta"
if [ -f "$META" ]; then
	# parsed, not sourced: a truncated meta used to abort the script under `set -u`
	META_LABEL="$(sed -n 's/^META_LABEL=//p' "$META" | head -1)"
	META_PORT="$(sed -n 's/^META_PORT=//p' "$META" | head -1)"
	META_OS="$(sed -n 's/^META_OS=//p' "$META" | head -1)"
	[ "$UNINSTALL" = 1 ] && { [ -n "${META_LABEL:-}" ] && LABEL="$META_LABEL"; }
	# PORT_SET distinguishes "--port 8000" from the default value
	[ "${PORT_SET:-0}" = 0 ] && [ -n "${META_PORT:-}" ] && PORT="$META_PORT"
	case "$OS" in
		Darwin) PLIST="$HOME/Library/LaunchAgents/$LABEL.plist" ;;
	esac
fi

# --- uninstall -----------------------------------------------------------------
if [ "$UNINSTALL" = 1 ]; then
	case "$OS" in
		Darwin)
			[ -f "$PLIST" ] && launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
			rm -f "$PLIST" ;;
		Linux)
			systemctl --user disable --now pi-eink 2>/dev/null || true
			rm -f "$UNIT"; systemctl --user daemon-reload 2>/dev/null || true ;;
	esac
	if [ "$KEEP_PI" = 0 ] && command -v pi >/dev/null 2>&1; then
		say "unregistering pi package $REPO (use --keep-pi to keep it)"
		pi remove "$REPO" >/dev/null 2>&1 || say "warning: pi remove did not match; check 'pi list'"
	fi
	# put a hand-maintained extension back where it was
	if [ -f "$HOME/.pi/agent/extensions/eink-bridge.ts.off" ]; then
		mv "$HOME/.pi/agent/extensions/eink-bridge.ts.off" "$HOME/.pi/agent/extensions/eink-bridge.ts"
		say "restored the loose extension copy at ~/.pi/agent/extensions/eink-bridge.ts"
	fi
	rm -f "$META"
	say "service removed."
	say "state kept in $STATE (delete it to revoke the token: rm -rf $STATE)."
	exit 0
fi

# --- state dir and secrets -----------------------------------------------------
mkdir -p "$STATE"
chmod 700 "$STATE"

if [ ! -s "$STATE/token" ]; then
	if command -v openssl >/dev/null 2>&1; then
		openssl rand -hex 16 > "$STATE/token"
	else
		"$PY" -c 'import secrets,pathlib;pathlib.Path("'"$STATE"'/token").write_text(secrets.token_hex(16))'
	fi
	say "created $STATE/token"
fi
chmod 600 "$STATE/token"
[ -f "$STATE/masked_secrets" ] && chmod 600 "$STATE/masked_secrets"   # retired tokens
TOKEN="$(tr -d '\n' < "$STATE/token")"

if [ ! -s "$STATE/pin" ]; then
	# short code for the Kindle keyboard; only useful together with the token gate
	"$PY" -c 'import secrets;print(secrets.randbelow(10**6))' > "$STATE/pin"
	say "created $STATE/pin (short code, printed below)"
fi
chmod 600 "$STATE/pin"
PIN="$(tr -d '\n' < "$STATE/pin")"

[ -s "$STATE/uas" ] || printf '%s\n' \
	'# User-Agent substrings allowed to use the page (spoofable speed bump).' \
	'kindle build/' > "$STATE/uas"

[ -s "$STATE/allow_ips" ] || printf '%s\n' \
	'# Peers allowed to connect at all. loopback is always allowed and peers in the' \
	'# host own /24 are allowed by default (EINK_ALLOW_SAME_SUBNET=0 to disable),' \
	'# so this only needs entries for routed/VPN peers.' \
	'127.0.0.1' \
	'::1' > "$STATE/allow_ips"

# Which networks may type. Seeding the host's current /24 means the machine works
# immediately; any other network opens read-only until the pin is entered there.
if [ ! -s "$STATE/trust.json" ]; then
	"$PY" - "$STATE/trust.json" <<'PY'
import json, pathlib, socket, sys
subnet = ""
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("8.8.8.8", 80))          # no packet is sent: this only picks a route
    ip = s.getsockname()[0]
    s.close()
    subnet = ".".join(ip.split(".")[:3]) + ".0/24"
except Exception:
    pass
state = {"confirmed": [subnet] if subnet else [], "peers": [subnet] if subnet else [], "readonly": False}
pathlib.Path(sys.argv[1]).write_text(json.dumps(state, indent=2))
print("trust seeded for", subnet or "(unknown network: first visit will be read-only)")
PY
fi

# --- pi package ----------------------------------------------------------------
if [ "$DO_PI" = 1 ]; then
	if ! command -v pi >/dev/null 2>&1; then
		say "warning: pi not on PATH; skipping 'pi install'"
	else
		LOOSE="$HOME/.pi/agent/extensions/eink-bridge.ts"
		if [ -f "$LOOSE" ] && [ ! -L "$LOOSE" ]; then
			# two copies of the extension = two bridges in every session
			mv "$LOOSE" "$LOOSE.off"
			say "disabled the loose copy at $LOOSE (moved to .off)"
		fi
		# pi stores local sources relative to settings.json, so compare resolved paths
		if "$PY" - "$HOME/.pi/agent/settings.json" "$REPO" <<'PYCHECK'
import json, pathlib, sys
settings, repo = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
try:
    packages = json.loads(settings.read_text()).get("packages") or []
except Exception:
    packages = []

def is_this_repo(entry):
    text = str(entry)
    if text.startswith(("npm:", "git:", "http://", "https://", "ssh://")):
        return False
    path = pathlib.Path(text)
    if not path.is_absolute():
        path = settings.parent / path
    try:
        return path.resolve() == repo.resolve()
    except Exception:
        return False

sys.exit(0 if any(is_this_repo(e) for e in packages) else 1)
PYCHECK
		then
			say "pi already has this package registered"
		else
			pi install "$REPO"
		fi
	fi
fi

# --- safety-gate dependency ----------------------------------------------------
# The 允许/拒绝 block renders from ~/.pi/eink/pending.json, which only the
# safety-gate extension writes. A missing gate yields an empty block and no error
# anywhere, so report what was found instead of leaving it to be discovered.
GATE="$HOME/.pi/agent/extensions/safety-gate/index.ts"
if [ -f "$GATE" ]; then
	say "safety-gate found: the 允许/拒绝 buttons have a source"
else
	say "warning: no safety-gate at $GATE"
	say "warning: the page will never show a pending command until it is installed"
fi

# --- service unit --------------------------------------------------------------
render() {  # $1 = template, $2 = "launchd" | "systemd", $3 = "1" to keep UA strict
	"$PY" - "$1" "$2" "$LABEL" "$REPO" "$PY" "$PORT" "$STATE" "$3" <<'PYRENDER'
import sys, pathlib
src, kind, label, repo, py, port, state, strict = sys.argv[1:9]
t = pathlib.Path(src).read_text()
t = t.replace("com.example.pi-eink", label).replace("/path/to/pi-eink", repo)
t = t.replace("/usr/bin/python3", py).replace("/tmp/pi-eink.log", state + "/server.log")
if kind == "launchd":
    t = t.replace("<key>EINK_PORT</key>\n    <string>8000</string>",
                  "<key>EINK_PORT</key>\n    <string>%s</string>" % port)
    if strict == "1":
        t = t.replace("<key>EINK_PORT</key>\n    <string>%s</string>" % port,
                      "<key>EINK_PORT</key>\n    <string>%s</string>\n"
                      "    <key>EINK_UA_STRICT</key>\n    <string>1</string>" % port)
else:
    t = t.replace("Environment=EINK_PORT=8000", "Environment=EINK_PORT=%s" % port)
    if strict == "1":
        t = t.replace("Environment=EINK_PORT=%s" % port,
                      "Environment=EINK_PORT=%s\nEnvironment=EINK_UA_STRICT=1" % port)
sys.stdout.write(t)
PYRENDER
}

# An existing unit may carry EINK_* variables the operator added (EINK_BIND,
# EINK_UA_STRICT, ...). Re-rendering the unit from the template would silently drop
# them, so they are collected first and re-applied afterwards.
KEEP_ENV=""
case "$OS" in
	Darwin) existing_unit="$PLIST" ;;
	Linux) existing_unit="$UNIT" ;;
esac
if [ -f "$existing_unit" ]; then
	# launchd units hold <key>NAME</key><string>VALUE</string>, systemd units hold
	# NAME=VALUE; the systemd pattern matched nothing on macOS, so settings were
	# silently dropped on every re-run there.
	case "$OS" in
		Darwin)
			KEEP_ENV="$("$PY" - "$existing_unit" <<'PYREAD'
import json, plistlib, sys
try:
    with open(sys.argv[1], "rb") as fh:
        env = (plistlib.load(fh) or {}).get("EnvironmentVariables") or {}
except Exception:
    env = {}
print("\n".join("%s=%s" % (k, v) for k, v in env.items() if k.startswith("EINK_") and k != "EINK_PORT"))
PYREAD
)" ;;
		Linux)
			KEEP_ENV="$(sed -n 's/^Environment=\(EINK_[A-Z_]*\)=\(.*\)$/\1=\2/p' "$existing_unit" | grep -v '^EINK_PORT=' | sort -u)" ;;
	esac
fi

# An existing unit may carry EINK_UA_STRICT (loopback is UA-gated too). Reinstalling
# must not silently relax a security setting the operator turned on.
if [ -z "$UA_STRICT" ]; then
	# The template's own comment mentions EINK_UA_STRICT, so a substring grep on the
	# rendered file always matched and every re-run re-enabled the loopback UA gate,
	# making --no-ua-strict impossible to keep.
	strict_now=""
	case "$OS" in
		Darwin)
			if [ -f "$PLIST" ]; then
				strict_now="$(plutil -extract EnvironmentVariables.EINK_UA_STRICT raw -o - "$PLIST" 2>/dev/null || true)"
			fi ;;
		Linux)
			if [ -f "$UNIT" ]; then
				strict_now="$(sed -n 's/^Environment=EINK_UA_STRICT=\(.*\)$/\1/p' "$UNIT" | head -1)"
			fi ;;
	esac
	[ "$strict_now" = "1" ] && UA_STRICT=1 || UA_STRICT=0
fi

if [ "$DO_SERVICE" = 1 ]; then
	case "$OS" in
		Darwin)
			mkdir -p "$(dirname "$PLIST")"
			render "$REPO/service/com.example.pi-eink.plist.example" launchd "$UA_STRICT" > "$PLIST"
			if [ -n "$KEEP_ENV" ]; then
				say "keeping from the previous unit: $(printf '%s' "$KEEP_ENV" | tr '\n' ' ')"
				# re-parse the plist so the keys land inside EnvironmentVariables, not
				# as siblings of Label (which plutil -lint accepts and launchd ignores)
				"$PY" - "$PLIST" "$KEEP_ENV" <<'PYENV'
import plistlib, sys
path = sys.argv[1]
with open(path, "rb") as fh:
    unit = plistlib.load(fh)
env = unit.setdefault("EnvironmentVariables", {})
for entry in sys.argv[2].split():
    if "=" in entry:
        key, value = entry.split("=", 1)
        env.setdefault(key, value)
with open(path, "wb") as fh:
    plistlib.dump(unit, fh)
PYENV
			fi
			plutil -lint "$PLIST" >/dev/null || die "generated unit is not a valid plist: $PLIST"
			if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
				say "warning: port $PORT is already in use by:"
				lsof -nP -iTCP:"$PORT" -sTCP:LISTEN | sed -n '2,3p'
			fi
			launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
			launchctl bootstrap "gui/$(id -u)" "$PLIST"
			say "launchd unit installed: $PLIST" ;;
		Linux)
			[ "$LABEL" = "com.$(whoami).pi-eink" ] || say "note: --label only names the launchd unit; the systemd unit is pi-eink.service"
			mkdir -p "$(dirname "$UNIT")"
			render "$REPO/service/pi-eink.service.example" systemd "$UA_STRICT" > "$UNIT"
			if [ -n "$KEEP_ENV" ]; then
				say "keeping from the previous unit: $(printf '%s' "$KEEP_ENV" | tr '\n' ' ')"
				"$PY" - "$UNIT" "$KEEP_ENV" <<'PYENV2'
import pathlib, sys
path, env = pathlib.Path(sys.argv[1]), sys.argv[2].split()
lines = path.read_text().splitlines()
extra = ["Environment=%s" % e for e in env if "=" in e]
out, placed = [], False
for line in lines:
    if not placed and line.startswith("[Install]"):
        out.extend(extra); extra = []; placed = True
    out.append(line)
if extra:
    out.extend(extra)
path.write_text("\n".join(out) + "\n")
PYENV2
			fi
			systemctl --user daemon-reload
			systemctl --user enable pi-eink
			# enable --now does not restart a running unit, so a changed --port or env
			# silently kept the old process
			systemctl --user restart pi-eink
			say "systemd unit installed: $UNIT (enable-linger for a headless box)" ;;
	esac
	sleep 1
	# Kindle-ish UA on purpose: with EINK_UA_STRICT=1 loopback is gated too, and this
	# check answers "did the unit come up", not "is my browser whitelisted"
	code="$(curl -s -o /dev/null -w '%{http_code}' -m 5 -A 'Kindle Build/4.0-rc2' \
		-H "Cookie: pieink=$TOKEN" "http://127.0.0.1:$PORT/" || true)"
	case "$code" in
		200) say "viewer answers on 127.0.0.1:$PORT" ;;
		"") say "warning: nothing answered on port $PORT yet; check $STATE/server.log" ;;
		*) say "warning: viewer answered $code; check $STATE/server.log (UA gate?)" ;;
	esac
fi

if [ "$DO_SERVICE" = 1 ]; then
	printf 'META_LABEL=%s\nMETA_PORT=%s\nMETA_OS=%s\n' "$LABEL" "$PORT" "$OS" > "$META"
fi

LAN="$("$PY" - <<'PY' 2>/dev/null || true
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.connect(("8.8.8.8", 80))
print(s.getsockname()[0])
s.close()
PY
)"
LAN="${LAN:-<this-host>}"

say ""
say "open once per device:"
say "  computer : http://127.0.0.1:$PORT/?k=$TOKEN"
say "  Kindle   : http://$LAN:$PORT/?k=$PIN      (pin, easier to type)"
say ""
say "state dir : $STATE   (token, pin, trust.json, logs)"
say "next      : reload pi (/reload) so the bridge extension loads"
