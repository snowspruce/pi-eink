#!/usr/bin/env bash
# One-command install for pi-eink (macOS launchd / Linux systemd --user).
#
#   ./install.sh                     # state dir, secrets, pi package, service unit
#   ./install.sh --port 8080         # different viewer port
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

while [ $# -gt 0 ]; do
	case "$1" in
		--port) PORT="$2"; shift 2 ;;
		--label) LABEL="$2"; shift 2 ;;
		--no-pi) DO_PI=0; shift ;;
		--keep-pi) KEEP_PI=1; shift ;;
		--no-service) DO_SERVICE=0; shift ;;
		--uninstall) UNINSTALL=1; shift ;;
		--ua-strict) UA_STRICT=1; shift ;;
		--no-ua-strict) UA_STRICT=0; shift ;;
		-h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
		*) echo "unknown option: $1" >&2; exit 2 ;;
	esac
done

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

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

# An existing unit may carry EINK_UA_STRICT (loopback is UA-gated too). Reinstalling
# must not silently relax a security setting the operator turned on.
if [ -z "$UA_STRICT" ]; then
	case "$OS" in
		Darwin) existing="$PLIST" ;;
		Linux) existing="$UNIT" ;;
	esac
	if [ -f "$existing" ] && grep -q "EINK_UA_STRICT" "$existing"; then
		UA_STRICT=1
	else
		UA_STRICT=0
	fi
fi

if [ "$DO_SERVICE" = 1 ]; then
	case "$OS" in
		Darwin)
			mkdir -p "$(dirname "$PLIST")"
			render "$REPO/service/com.example.pi-eink.plist.example" launchd "$UA_STRICT" > "$PLIST"
			launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
			launchctl bootstrap "gui/$(id -u)" "$PLIST"
			say "launchd unit installed: $PLIST" ;;
		Linux)
			[ "$LABEL" = "com.$(whoami).pi-eink" ] || say "note: --label only names the launchd unit; the systemd unit is pi-eink.service"
			mkdir -p "$(dirname "$UNIT")"
			render "$REPO/service/pi-eink.service.example" systemd "$UA_STRICT" > "$UNIT"
			systemctl --user daemon-reload
			systemctl --user enable --now pi-eink
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
