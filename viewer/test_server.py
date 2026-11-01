"""Tests for the viewer's trust / capability gates. Run with `npm run test:viewer`.

HOME points at a temp dir before the module is imported: every path in server.py is
resolved from Path.home() at import time, so the real ~/.pi/eink is never touched.
"""

import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import time
import types
import unittest

HOME = pathlib.Path(tempfile.mkdtemp(prefix="pi-eink-viewer-test-"))
EINK = HOME / ".pi/eink"
EINK.mkdir(parents=True)
os.environ["HOME"] = str(HOME)
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
