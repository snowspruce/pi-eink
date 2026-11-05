"""Tests for the viewer's trust / capability gates. Run with `npm run test:viewer`.

HOME points at a temp dir before the module is imported: every path in server.py is
resolved from Path.home() at import time, so the real ~/.pi/eink is never touched.
"""

import atexit
import importlib.util
import json
import os
import pathlib
import shutil
import sys
import tempfile
import time
import types
import unittest

HOME = pathlib.Path(tempfile.mkdtemp(prefix="pi-eink-viewer-test-"))
atexit.register(shutil.rmtree, HOME, True)
EINK = HOME / ".pi/eink"
EINK.mkdir(parents=True)
os.environ["HOME"] = str(HOME)
# pi exports this to every child it starts; left in place it outranks the temp
# session dir and every test would read the developer's real conversation.
os.environ.pop("PI_SESSION_FILE", None)
for name, value in (("token", "a" * 32), ("pin", "123456"), ("allow_ips", "127.0.0.1\n"),
                    ("uas", "kindle build/\n")):
    (EINK / name).write_text(value)

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
# one session file, so render() has something to render
sessions = HOME / ".pi/agent/sessions" / "--test--"
sessions.mkdir(parents=True)
(sessions / "2026-01-01T00-00-00-000Z_aaaabbbb-1111-2222-3333-444455556666.jsonl").write_text(
    "\n".join([
        json.dumps({"type": "session", "id": "aaaabbbb-1111-2222-3333-444455556666", "cwd": "/tmp/demo"}),
        json.dumps({"type": "message", "id": "m1", "timestamp": "t1",
                    "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}}),
    ]) + "\n")

spec = importlib.util.spec_from_file_location("eink_server", pathlib.Path(__file__).resolve().parent / "server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class FakeHandler:
    """Only the two socket lookups the gates use."""

    def __init__(self, peer, local="192.168.3.45"):
        self.client_address = (peer, 51234)
        self.request = types.SimpleNamespace(getsockname=lambda: (local, 8000))


class History(unittest.TestCase):
    """The box's own command history lives on disk, not in the browser's localStorage:
    DOM storage is a per-app switch in Android WebView, and a disabled one made the
    recall button do nothing at all."""

    def setUp(self):
        try:
            server.HISTORY_FILE.unlink()
        except FileNotFoundError:
            pass

    def test_newest_first_and_deduped(self):
        server.history_push("s1", "a")
        server.history_push("s1", "b")
        server.history_push("s1", "a")
        self.assertEqual(server.history_list("s1"), ["a", "b"])

    def test_bounded(self):
        for i in range(server.HISTORY_MAX + 5):
            server.history_push("s1", "cmd%d" % i)
        self.assertEqual(len(server.history_list("s1")), server.HISTORY_MAX)

    def test_sessions_do_not_share_history(self):
        server.history_push("s1", "a")
        server.history_push("s2", "b")
        self.assertEqual(server.history_list("s1"), ["a"])
        self.assertEqual(server.history_list("s2"), ["b"])

    def test_blank_and_sidless_are_dropped(self):
        server.history_push("s1", "")
        server.history_push("s1", "   ")
        server.history_push("", "x")
        self.assertEqual(server.history_list("s1"), [])

    def test_missing_file_reads_as_empty(self):
        self.assertEqual(server.history_list("never"), [])


class IpGate(unittest.TestCase):
    def setUp(self):
        os.environ.pop("EINK_ALLOW_SAME_SUBNET", None)

    def test_loopback_always_allowed(self):
        self.assertTrue(server.ip_allowed("127.0.0.1", "")[0])

    def test_same_subnet_is_allowed_by_default(self):
        # the whole point: a new network must work without editing allow_ips
        self.assertTrue(server.ip_allowed("192.168.3.25", "192.168.3.45")[0])
        self.assertFalse(server.ip_allowed("10.1.2.3", "192.168.3.45")[0])

    def test_same_subnet_can_be_turned_off(self):
        os.environ["EINK_ALLOW_SAME_SUBNET"] = "0"
        try:
            self.assertFalse(server.ip_allowed("192.168.3.25", "192.168.3.45")[0])
        finally:
            os.environ.pop("EINK_ALLOW_SAME_SUBNET", None)

    def test_allow_list_entries_still_win(self):
        (EINK / "allow_ips").write_text("10.1.2.0/24\n")
        try:
            self.assertTrue(server.ip_allowed("10.1.2.3", "192.168.3.45")[0])
        finally:
            (EINK / "allow_ips").write_text("127.0.0.1\n")


class ClientIdentity(unittest.TestCase):
    """Behind the public entry every request comes from loopback: without the proxy's
    X-Real-IP the log says 127.0.0.1 and caps_for() hands the internet "full"."""

    def _handler(self, ip, real=""):
        class H:
            client_address = (ip, 1234)
            headers = {"X-Real-IP": real} if real else {}
        return H()

    def test_the_proxy_header_names_the_real_peer(self):
        self.assertEqual(server.client_ip(self._handler("127.0.0.1", "58.35.179.121")),
                         "58.35.179.121")

    def test_the_header_never_changes_the_trust_decision(self):
        """Only the local proxy can reach the port, so loopback stays "full" whatever
        X-Real-IP says - otherwise the public entry would be judged by subnet rules it
        was never part of, and the Kindle would land in read-only."""
        class H:
            client_address = ("127.0.0.1", 1)
            headers = {"X-Real-IP": "58.35.179.121"}
        self.assertEqual(server.caps_for(H()), "full")

    def test_a_direct_peer_cannot_rename_itself(self):
        # not from loopback: the header is only trusted from the local proxy
        self.assertEqual(server.client_ip(self._handler("192.168.1.9", "10.0.0.1")),
                         "192.168.1.9")

    def test_a_garbage_header_is_ignored(self):
        self.assertEqual(server.client_ip(self._handler("127.0.0.1", "not-an-ip")), "127.0.0.1")


class Capabilities(unittest.TestCase):
    def setUp(self):
        server.save_trust({"confirmed": [], "peers": [], "readonly": False})

    def test_loopback_is_always_full(self):
        self.assertEqual(server.caps_for(FakeHandler("127.0.0.1")), "full")

    def test_unconfirmed_network_is_readonly(self):
        # a café looks exactly like home to the peer address: only the host's own
        # subnet tells them apart, and it has never been unlocked
        self.assertEqual(server.caps_for(FakeHandler("192.168.9.9", "192.168.9.20")), "readonly")

    def test_confirmed_network_allows_typing(self):
        handler = FakeHandler("192.168.9.9", "192.168.9.20")
        server.confirm_network(handler)
        self.assertEqual(server.caps_for(handler), "full")
        self.assertEqual(server.trust_state()["peers"], ["192.168.9.0/24"])

    def test_peer_routed_in_from_another_subnet_stays_readonly(self):
        # confirmed host network, but the peer is not in it: office guest VLAN
        server.save_trust({"confirmed": ["192.168.9.0/24"], "peers": ["192.168.9.0/24"], "readonly": False})
        self.assertEqual(server.caps_for(FakeHandler("10.5.5.5", "192.168.9.20")), "readonly")

    def test_unlocking_a_split_subnet_office_remembers_both(self):
        handler = FakeHandler("10.5.5.5", "192.168.9.20")
        server.confirm_network(handler)
        self.assertEqual(server.caps_for(handler), "full")
        self.assertEqual(server.trust_state()["confirmed"], ["192.168.9.0/24"])
        self.assertEqual(server.trust_state()["peers"], ["10.5.5.0/24"])

    def test_readonly_switch_wins_over_a_confirmed_network(self):
        handler = FakeHandler("192.168.9.9", "192.168.9.20")
        server.confirm_network(handler)
        state = server.trust_state()
        state["readonly"] = True
        server.save_trust(state)
        self.assertEqual(server.caps_for(handler), "readonly")
        self.assertEqual(server.caps_for(FakeHandler("127.0.0.1")), "full")

    def test_readonly_actions_are_stop_and_status_only(self):
        self.assertEqual(server.READONLY_ACTIONS, {"stop", "status", "unlock", "readonly"})
        for dangerous in ("say", "execute", "review", "approve", "deny", "continue"):
            self.assertNotIn(dangerous, server.READONLY_ACTIONS)


def fake_handler(cookie=None, ua="Kindle Build/4.0-rc2", ip="127.0.0.1", local="192.168.3.45"):
    """A Handler with only the attributes _authorized() touches."""
    handler = server.Handler.__new__(server.Handler)
    headers = {"User-Agent": ua}
    if cookie is not None:
        headers["Cookie"] = cookie
    handler.headers = headers
    handler.client_address = (ip, 51234)
    handler.request = types.SimpleNamespace(getsockname=lambda: (local, 8000))
    return handler


class Authorized(unittest.TestCase):
    """_authorized() is the only enforcement point for the identity gates, and it
    had no test at all: the claims in README's Security model were unchecked."""

    def setUp(self):
        server._FAILS.clear()
        server._BLOCKED.clear()
        server._LAST_FAIL_COUNTED.clear()
        os.environ.pop("EINK_UA_STRICT", None)

    def tearDown(self):
        server._FAILS.clear()
        server._BLOCKED.clear()
        server._LAST_FAIL_COUNTED.clear()
        os.environ.pop("EINK_UA_STRICT", None)

    def test_good_cookie_passes(self):
        # extra may carry a rolling cookie refresh, never an error
        ok, extra, reason = fake_handler(cookie="pieink=" + "a" * 32)._authorized({})
        self.assertTrue(ok, reason)
        for name, value in extra:
            self.assertEqual(name, "Set-Cookie")
            self.assertIn("Max-Age=%d" % server.COOKIE_MAX_AGE, value)

    def test_no_credential_is_refused_and_does_not_lock_out(self):
        for _ in range(10):
            ok, _extra, reason = fake_handler()._authorized({})
            self.assertFalse(ok)
        self.assertEqual(server._BLOCKED, {})   # anonymous probes are not guesses

    def test_query_token_seeds_the_cookie(self):
        ok, extra, _reason = fake_handler()._authorized({"k": ["a" * 32]})
        self.assertTrue(ok)
        self.assertEqual(len(extra), 1)
        self.assertIn("Set-Cookie", extra[0][0])
        self.assertIn("Max-Age=%d" % server.COOKIE_MAX_AGE, extra[0][1])

    def test_pin_also_seeds_the_cookie(self):
        ok, extra, _reason = fake_handler()._authorized({"k": ["123456"]})
        self.assertTrue(ok)
        self.assertIn("Set-Cookie", extra[0][0])
        # the seeded cookie must NOT be the master token: the pin is a
        # convenience factor, and the token also unlocks install.sh and the
        # bridge and survives a pin change
        self.assertNotIn("a" * 32, extra[0][1])
        minted = extra[0][1].split("=", 1)[1].split(";", 1)[0]
        ok, _extra, reason = fake_handler(cookie="pieink=" + minted)._authorized({})
        self.assertTrue(ok, reason)   # and the session secret keeps working

    def test_wrong_cookie_costs_one_failure_per_minute(self):
        # the same stale cookie is repeated by every poll; counting each repeat is
        # what locked a reader out (and shut the ?k= recovery path) within seconds
        for _ in range(10):
            fake_handler(cookie="pieink=wrong")._authorized({})
        self.assertNotIn("127.0.0.1", server._BLOCKED)
        self.assertEqual(server._FAILS["127.0.0.1"][0], 1)

    def test_wrong_guesses_still_lock(self):
        for _ in range(4):
            fake_handler()._authorized({"k": ["wrong"]})
        self.assertNotIn("127.0.0.1", server._BLOCKED)
        fake_handler()._authorized({"k": ["wrong"]})
        self.assertIn("127.0.0.1", server._BLOCKED)

    def test_a_valid_credential_recovers_from_a_lockout(self):
        # the token is checked before the lockout on purpose: after a token
        # rotation the reader must not be locked out of their own page. The pin
        # is six digits, so it waits the block out like any other guess: letting
        # it through mid-block turned the lockout into a speed bump.
        server._BLOCKED["127.0.0.1"] = time.time() + 600
        ok, _extra, reason = fake_handler(cookie="pieink=" + "a" * 32)._authorized({})
        self.assertTrue(ok, reason)
        self.assertNotIn("127.0.0.1", server._BLOCKED)
        server._BLOCKED["127.0.0.1"] = time.time() + 600
        ok, _extra, reason = fake_handler()._authorized({"k": ["a" * 32]})
        self.assertTrue(ok, reason)
        server._BLOCKED["127.0.0.1"] = time.time() + 600
        ok, _extra, reason = fake_handler()._authorized({"k": ["123456"]})
        self.assertFalse(ok)
        self.assertIn("错误次数过多", reason)
        self.assertIn("127.0.0.1", server._BLOCKED)

    def test_a_blocked_peer_with_a_bad_credential_is_still_refused(self):
        server._BLOCKED["127.0.0.1"] = time.time() + 600
        ok, _extra, reason = fake_handler(cookie="pieink=wrong")._authorized({})
        self.assertFalse(ok)
        self.assertIn("错误次数过多", reason)

    def test_missing_token_file_is_closed(self):
        token_file = server.TOKEN_FILE
        backup = token_file.read_text() if token_file.exists() else None
        try:
            token_file.write_text("")
            ok, _extra, reason = fake_handler(cookie="pieink=x")._authorized({})
            self.assertFalse(ok)
            self.assertIn("token file missing", reason)
        finally:
            if backup is not None:
                token_file.write_text(backup)

    def test_ua_gate_stops_unknown_browsers_from_the_lan(self):
        ok, _extra, reason = fake_handler(ua="curl/8", ip="192.168.3.99")._authorized({})
        self.assertFalse(ok)
        self.assertIn("ua not allowed", reason)
        ok, _extra, reason = fake_handler(cookie="pieink=" + "a" * 32, ua="curl/8",
                                          ip="127.0.0.1")._authorized({})
        self.assertTrue(ok, reason)   # loopback is exempt unless EINK_UA_STRICT=1


class PendingOwnership(unittest.TestCase):
    def test_only_the_session_that_parked_it_shows_the_block(self):
        parked = {"pid": os.getpid()}
        server.live_bridges.__globals__["_LIVE_CACHE"][:2] = [time.time(), {
            "mine": {"pid": os.getpid(), "port": 8010},
            "other": {"pid": os.getpid() + 1, "port": 8011},
        }]
        try:
            self.assertTrue(server.pending_owned_by(8010, parked))
            self.assertFalse(server.pending_owned_by(8011, parked))
            self.assertFalse(server.pending_owned_by(0, parked))
        finally:
            server.live_bridges.__globals__["_LIVE_CACHE"][:2] = [0.0, {}]


class UsageTolerance(unittest.TestCase):
    def test_garbage_usage_does_not_break_the_page(self):
        rows = [{"type": "message", "message": {"role": "assistant",
                                               "usage": {"input": "12abc", "cost": {"total": "x"}}}}]
        total = server.usage_totals(rows)
        self.assertEqual(total["input"], 0)
        self.assertEqual(total["cost"], 0.0)

    def test_usage_that_is_not_a_dict_is_skipped(self):
        rows = [{"type": "message", "message": {"role": "assistant", "usage": "broken"}}]
        self.assertEqual(server.usage_totals(rows)["input"], 0)


class UaComments(unittest.TestCase):
    def test_commented_out_ua_is_not_allowed(self):
        ua_file = server.UA_FILE
        ua_file.write_text("#mozilla/5.0\nkindle build/\n")
        try:
            entries = server.ua_allow_list()
            self.assertEqual(entries, ["kindle build/"])
        finally:
            ua_file.write_text("kindle build/\n")


class TrustScope(unittest.TestCase):
    """A confirmation belongs to the network it was given on."""

    def setUp(self):
        server.save_trust({"confirmed": ["192.168.9.0/24"], "peers": ["192.168.9.0/24"],
                           "readonly": False, "stamp": {"subnet": "192.168.9.0/24",
                                                        "at": time.time()}})
        os.environ.pop("EINK_TRUST_HOURS", None)

    def tearDown(self):
        os.environ.pop("EINK_TRUST_HOURS", None)

    def test_same_network_stays_full(self):
        self.assertEqual(server.caps_for(FakeHandler("192.168.9.9", "192.168.9.20")), "full")

    def test_another_network_with_the_same_subnet_is_not_inherited(self):
        # the host moved to a different network; only its own address says so
        self.assertEqual(server.caps_for(FakeHandler("10.9.9.9", "10.9.9.20")), "readonly")

    def test_network_change_reverts_even_when_the_subnet_was_confirmed(self):
        # trust.json lists both, but the confirmation was given on the first one
        server.save_trust({"confirmed": ["192.168.9.0/24", "10.9.9.0/24"],
                           "peers": ["192.168.9.0/24", "10.9.9.0/24"], "readonly": False,
                           "stamp": {"subnet": "192.168.9.0/24", "at": time.time()}})
        self.assertEqual(server.caps_for(FakeHandler("10.9.9.9", "10.9.9.20")), "readonly")

    def test_optional_expiry(self):
        server.save_trust({"confirmed": ["192.168.9.0/24"], "peers": ["192.168.9.0/24"],
                           "readonly": False,
                           "stamp": {"subnet": "192.168.9.0/24", "at": time.time() - 9 * 3600}})
        handler = FakeHandler("192.168.9.9", "192.168.9.20")
        self.assertEqual(server.caps_for(handler), "full")     # no TTL configured
        os.environ["EINK_TRUST_HOURS"] = "8"
        self.assertEqual(server.caps_for(handler), "readonly")

    def test_pin_confirmation_records_the_network_and_time(self):
        handler = FakeHandler("172.20.10.7", "172.20.10.2")
        server.confirm_network(handler)
        state = server.trust_state()
        self.assertEqual(state["stamp"]["subnet"], "172.20.10.0/24")
        self.assertGreater(state["stamp"]["at"], 0)
        self.assertEqual(server.caps_for(handler), "full")


class RenderRobustness(unittest.TestCase):
    def test_non_object_rows_do_not_break_the_page(self):
        import json as _json
        path = server.SESSIONS / "--broken--" / "broken.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join([
            "null",
            '"just a string"',
            "[1, 2]",
            _json.dumps({"type": "message", "id": "ok1", "timestamp": "t",
                         "message": {"role": "user", "content": [{"type": "text", "text": "still here"}]}}),
            _json.dumps({"type": "message", "id": "bad", "message": "not a dict"}),
        ]) + "\n")
        try:
            digest, frag, status, foot, items, busy = server.render(path, 5, False, False)
            self.assertIn("still here", frag)
        finally:
            path.unlink()

    def test_rewritten_file_does_not_serve_cached_rows(self):
        path = server.SESSIONS / "--rewrite--" / "rewrite.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        import json as _json
        row = {"type": "message", "id": "same-id", "timestamp": "same-ts",
               "message": {"role": "user", "content": [{"type": "text", "text": "ORIGINAL"}]}}
        path.write_text(_json.dumps(row) + "\n")
        try:
            first = server.render(path, 5, False, False)[1]
            self.assertIn("ORIGINAL", first)
            row["message"]["content"][0]["text"] = "REWRITTEN TEXT v2"
            path.write_text(_json.dumps(row) + "\n")
            second = server.render(path, 5, False, False)[1]
            self.assertIn("REWRITTEN TEXT v2", second)
        finally:
            path.unlink()


class ActionGate(unittest.TestCase):
    """action_allowed() is the enforcement point; caps_for() only decides."""

    def test_full_allows_everything(self):
        for action in ("say", "execute", "review", "approve", "deny", "continue", "stop", "status"):
            self.assertTrue(server.action_allowed("full", action), action)

    def test_readonly_blocks_the_dangerous_ones(self):
        for action in ("say", "execute", "review", "approve", "deny", "continue"):
            self.assertFalse(server.action_allowed("readonly", action), action)

    def test_readonly_keeps_stop_status_and_the_unlock_flow(self):
        for action in ("stop", "status", "unlock", "readonly"):
            self.assertTrue(server.action_allowed("readonly", action), action)


class DiagSanitize(unittest.TestCase):
    def test_control_characters_cannot_forge_a_log_line(self):
        # %0A reaches the handler as a real newline
        self.assertEqual(server._diag_clean("x\nfake\tline", 60), "x fake line")

    def test_values_are_bounded(self):
        self.assertEqual(len(server._diag_clean("A" * 500, 60)), 60)


class DiagRate(unittest.TestCase):
    def test_one_tap_can_send_both_beacons(self):
        # submit + done arrive within a second; the second must not be dropped
        server._DIAG_HITS.clear()
        self.assertTrue(server._diag_allow("10.9.9.9"))
        self.assertTrue(server._diag_allow("10.9.9.9"))

    def test_a_burst_is_cut_off(self):
        server._DIAG_HITS.clear()
        allowed = sum(1 for _ in range(20) if server._diag_allow("10.9.9.8"))
        self.assertEqual(allowed, server._DIAG_BURST)

    def test_another_peer_is_not_affected(self):
        server._DIAG_HITS.clear()
        for _ in range(20):
            server._diag_allow("10.9.9.7")
        self.assertTrue(server._diag_allow("10.9.9.6"))


class RenderPicker(unittest.TestCase):
    def test_session_picker_link_is_hidden_when_readonly(self):
        path = next(iter(sorted(server.SESSIONS.rglob("*.jsonl"))), None)
        if path is None:
            self.skipTest("no session files to render")
        full = server.render(path, 3, False, False, 0, False, "", "full")[3]
        ro = server.render(path, 3, False, False, 0, False, "", "readonly")[3]
        self.assertIn("/sessions", full)
        self.assertNotIn("/sessions", ro)


class ActionsHtml(unittest.TestCase):
    def test_full_network_gets_the_text_box(self):
        html = server.acts_html(True, "", "full")
        self.assertIn('name="text"', html)
        self.assertIn("只读", html)  # the switch to drop to read-only on arrival

    def test_readonly_network_gets_a_pin_box_instead(self):
        html = server.acts_html(True, "", "readonly")
        self.assertNotIn('name="text"', html)
        self.assertNotIn("/act/execute", html)
        self.assertIn('name="k"', html)
        self.assertIn("/act/unlock", html)

    def test_readonly_keeps_stop_and_status(self):
        html = server.acts_html(True, "", "readonly")
        self.assertIn("/act/stop", html)
        self.assertIn("/act/status", html)
        self.assertNotIn("/act/say", html)


class Credentials(unittest.TestCase):
    def test_cookie_lives_a_week(self):
        self.assertIn("Max-Age=%d" % (7 * 24 * 3600), server.cookie_value())

    def test_token_is_masked_in_page_output(self):
        self.assertNotIn("a" * 32, server.mask_text("token is " + "a" * 32))

    def test_failed_guesses_survive_a_restart(self):
        server._FAILS.clear()
        server._BLOCKED.clear()
        ip = "192.168.3.77"
        server._FAILS[ip] = (5, time.time())
        server._BLOCKED[ip] = time.time() + 600
        server.save_fails()
        server._FAILS.clear()
        server._BLOCKED.clear()
        server.load_fails()
        self.assertIn(ip, server._BLOCKED)
        self.assertTrue(server._BLOCKED[ip] > time.time())


if __name__ == "__main__":
    unittest.main(verbosity=2)


class NoPinMode(unittest.TestCase):
    """EINK_NO_PIN=1: the public entry only accepts the 32-char token.

    The pin is a six digit LAN convenience; on an internet-facing endpoint it is a
    credential for driving a shell-capable agent, so it must not be accepted at all -
    and the read-only page must not render a pin form that can never succeed.
    """

    def setUp(self):
        self._old = server.NO_PIN
        server.NO_PIN = True
        self.addCleanup(setattr, server, "NO_PIN", self._old)

    def test_readonly_page_has_no_pin_form(self):
        html = server.acts_html(True, "", "readonly")
        self.assertNotIn("/act/unlock", html)
        self.assertNotIn('name="k"', html)
        self.assertIn("只读", html)

    def test_full_page_unchanged(self):
        html = server.acts_html(True, "", "full")
        self.assertIn('name="text"', html)
