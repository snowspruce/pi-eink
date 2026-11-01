"""HTTP-level tests: the enforcement point, not just the decision functions.

Every earlier test drove `caps_for()` / `action_allowed()` directly, so deleting the
`action_allowed` call in the handler kept the whole suite green. These start a real
viewer server against a fake bridge and check the wire behaviour.
"""

import json
import os
import pathlib
import shutil
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import test_server as base  # noqa: E402  (sets up the temp HOME first)
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

server = base.server
TOKEN = "a" * 32
KINDLE_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/534.24 (KHTML, like Gecko) Chrome/11.0.696.34 Safari/534.24"


class FakeBridge(BaseHTTPRequestHandler):
    """Stands in for a pi bridge and records what the viewer proxies to it."""

    seen: list = []

    def log_message(self, *_args):
        pass

    reply_status = 200
    reply_body = b'{"ok": true, "msg": "fake bridge"}'
    reply_headers: list = []

    def _record(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        FakeBridge.seen.append((self.command, self.path, body.decode("utf-8", "replace"),
                               dict(self.headers)))
        payload = FakeBridge.reply_body
        self.send_response(FakeBridge.reply_status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        for key, value in FakeBridge.reply_headers:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    do_GET = _record
    do_POST = _record


class Viewer:
    """The real Handler on an ephemeral port."""

    def __init__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def request(port, path, method="GET", body=None, cookie=True, accept=None, out=None, lang=None):
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"User-Agent": KINDLE_UA}
    if cookie:
        headers["Cookie"] = "pieink=" + TOKEN + (("; pielang=" + lang) if lang else "")
    if accept:
        headers["Accept"] = accept
    payload = body.encode() if body is not None else None
    if payload is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    conn.request(method, path, body=payload, headers=headers)
    res = conn.getresponse()
    data = res.read()
    if out is not None:
        out.update(dict(res.getheaders()))
    conn.close()
    return res.status, data


class ReadOnlyEnforcement(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bridge = ThreadingHTTPServer(("127.0.0.1", 0), FakeBridge)
        cls.bridge_port = cls.bridge.server_address[1]
        threading.Thread(target=cls.bridge.serve_forever, daemon=True).start()
        cls.viewer = Viewer()

    @classmethod
    def tearDownClass(cls):
        cls.viewer.close()
        cls.bridge.shutdown()
        cls.bridge.server_close()

    def setUp(self):
        FakeBridge.seen = []
        FakeBridge.reply_status = 200
        FakeBridge.reply_body = b'{"ok": true, "msg": "fake bridge"}'
        FakeBridge.reply_headers = []
        # the pin is server-side state: every test starts from "nothing remembered"
        server.VIEW_FILE.unlink(missing_ok=True)
        self.caps = mock.patch.object(server, "caps_for", return_value="readonly")
        self.caps.start()
        self.route = mock.patch.object(server, "bridge_port_for", return_value=self.bridge_port)
        self.route.start()
        # read-only routing must come from the rendered page, not from ?s=
        self.rendered = mock.patch.object(server, "rendered_session_id", return_value="session-id")
        self.rendered.start()

    def tearDown(self):
        mock.patch.stopall()

    def test_readonly_say_is_refused_and_never_reaches_the_bridge(self):
        status, data = request(self.viewer.port, "/act/say", "POST", "text=hello",
                               accept="application/json")
        self.assertEqual(status, 403, data[:200])
        self.assertEqual(FakeBridge.seen, [])

    def test_readonly_execute_is_refused(self):
        status, _data = request(self.viewer.port, "/act/execute", "POST")
        self.assertEqual(status, 303)          # plain form post: bounced back to /static
        self.assertEqual(FakeBridge.seen, [])

    def test_readonly_stop_still_reaches_the_bridge(self):
        status, _data = request(self.viewer.port, "/act/stop", "POST", accept="application/json")
        self.assertEqual(status, 200)
        self.assertEqual(len(FakeBridge.seen), 1)
        self.assertEqual(FakeBridge.seen[0][1], "/act/stop")

    def test_readonly_cannot_open_the_session_picker(self):
        status, _data = request(self.viewer.port, "/sessions")
        self.assertEqual(status, 403)

    def test_readonly_ignores_a_bookmarked_session_id(self):
        status, _data = request(self.viewer.port, "/data?s=01234567-89ab-cdef-0123-456789abcdef")
        self.assertEqual(status, 200)

    def test_readonly_page_is_not_redirected(self):
        # ?s= is dropped on a read-only network, so pinning it would loop forever
        status, _data = request(self.viewer.port, "/")
        self.assertEqual(status, 200)


class FullNetwork(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bridge = ThreadingHTTPServer(("127.0.0.1", 0), FakeBridge)
        cls.bridge_port = cls.bridge.server_address[1]
        threading.Thread(target=cls.bridge.serve_forever, daemon=True).start()
        cls.viewer = Viewer()

    @classmethod
    def tearDownClass(cls):
        cls.viewer.close()
        cls.bridge.shutdown()
        cls.bridge.server_close()

    def setUp(self):
        FakeBridge.seen = []
        FakeBridge.reply_status = 200
        FakeBridge.reply_body = b'{"ok": true, "msg": "fake bridge"}'
        FakeBridge.reply_headers = []
        server.VIEW_FILE.unlink(missing_ok=True)
        for name, value in (("caps_for", lambda _h: "full"),
                            ("bridge_port_for", lambda _sid: self.bridge_port),
                            ("rendered_session_id", lambda: "session-id")):
            patcher = mock.patch.object(server, name, value)
            patcher.start()
        self.addCleanup(mock.patch.stopall)

    def test_say_reaches_the_bridge_once(self):
        status, data = request(self.viewer.port, "/act/say", "POST", "text=hello",
                               accept="application/json")
        self.assertEqual(status, 200, data[:200])
        self.assertEqual(len(FakeBridge.seen), 1)
        self.assertEqual(FakeBridge.seen[0][2], "text=hello")

    def test_language_reaches_the_bridge(self):
        request(self.viewer.port, "/act/say", "POST", "text=hi", accept="application/json", lang="en")
        self.assertEqual(FakeBridge.seen[0][3].get("X-Eink-Lang"), "en")
        FakeBridge.seen = []
        FakeBridge.reply_status = 200
        FakeBridge.reply_body = b'{"ok": true, "msg": "fake bridge"}'
        FakeBridge.reply_headers = []
        request(self.viewer.port, "/act/say", "POST", "text=hi", accept="application/json")
        self.assertEqual(FakeBridge.seen[0][3].get("X-Eink-Lang"), "zh")

    def test_actions_are_routed_to_the_rendered_session(self):
        request(self.viewer.port, "/act/stop", "POST", accept="application/json")
        self.assertEqual(FakeBridge.seen[0][1], "/act/stop")

    def test_page_without_s_pins_the_session_into_the_url(self):
        """Without this the poll re-resolved "newest live session" every 1.2s, so a
        second pi session's heartbeat swapped the whole page to another conversation."""
        seen = {}
        status, _data = request(self.viewer.port, "/", out=seen)
        self.assertEqual(status, 303)
        self.assertIn("s=aaaabbbb-1111-2222-3333-444455556666", seen.get("Location", ""))

    def test_a_stale_session_id_is_reported_not_silently_swapped(self):
        status, data = request(self.viewer.port, "/data?s=ffffffff-0000-1111-2222-333333333333")
        self.assertEqual(status, 200)
        self.assertIn("\u5df2\u4e0d\u53ef\u7528", data.decode("utf-8"))


class ReadOnlyPinnedView(unittest.TestCase):
    """A read-only network may not choose a session, but it must not hop either:
    the viewer remembers what it last rendered (~/.pi/eink/view.json) and keeps
    rendering that, while still ignoring any ?s= the client sends."""

    REMEMBERED = "aaaabbbb-1111-2222-3333-444455556666"   # the only session in the temp HOME
    NEWER = "ccccdddd-1111-2222-3333-444477778888"        # added per test, newer on disk

    @classmethod
    def setUpClass(cls):
        cls.viewer = Viewer()

    @classmethod
    def tearDownClass(cls):
        cls.viewer.close()

    def setUp(self):
        folder = server.SESSIONS / "--other--"
        folder.mkdir(parents=True, exist_ok=True)
        self.newer = folder / ("2026-02-02T00-00-00-000Z_%s.jsonl" % self.NEWER)
        self.newer.write_text(json.dumps({"type": "session", "id": self.NEWER, "cwd": "/tmp/other"}) + "\n")
        later = time.time() + 60
        os.utime(self.newer, (later, later))
        server.VIEW_FILE.write_text(json.dumps({"sid": self.REMEMBERED}))
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(server.VIEW_FILE.unlink, missing_ok=True)
        self.addCleanup(shutil.rmtree, folder, True)
        mock.patch.object(server, "caps_for", lambda _h: "readonly").start()

    def test_readonly_page_stays_on_the_remembered_session(self):
        status, data = request(self.viewer.port, "/data")
        self.assertEqual(status, 200)
        foot = json.loads(data)["foot"]
        self.assertIn("aaaabbbb", foot)
        self.assertNotIn("ccccdddd", foot)

    def test_readonly_still_cannot_choose_another_session(self):
        status, data = request(self.viewer.port, "/data?s=" + self.NEWER)
        self.assertEqual(status, 200)
        self.assertNotIn("ccccdddd", json.loads(data)["foot"])

    def test_readonly_actions_route_to_the_remembered_session(self):
        seen = []

        def port_for(sid):
            seen.append(sid)
            return 0

        with mock.patch.object(server, "bridge_port_for", port_for):
            request(self.viewer.port, "/act/stop", "POST", accept="application/json")
        self.assertEqual(seen[-1], self.REMEMBERED)


class Language(unittest.TestCase):
    """Page furniture is bilingual; the default is Chinese, English is opt-in and
    must not depend on guessing from the User-Agent."""

    SID = "aaaabbbb-1111-2222-3333-444455556666"

    @classmethod
    def setUpClass(cls):
        cls.viewer = Viewer()

    @classmethod
    def tearDownClass(cls):
        cls.viewer.close()

    def _page(self, lang=None):
        live = {self.SID: {"port": 1, "sessionId": self.SID}}
        with mock.patch.object(server, "caps_for", lambda _h: "full"), \
             mock.patch.object(server, "live_bridges", lambda: live), \
             mock.patch.object(server, "bridge_port_for", lambda _s: 1), \
             mock.patch.object(server, "bridge_state", lambda: None), \
             mock.patch.object(server, "bridge_alive", lambda: False):
            return request(self.viewer.port, "/?s=" + self.SID, lang=lang)

    def test_chinese_is_the_default(self):
        status, data = self._page()
        self.assertEqual(status, 200)
        body = data.decode("utf-8")
        self.assertIn("停下", body)
        self.assertIn("输入指令", body)
        self.assertNotIn(">Stop<", body)

    def test_pielang_cookie_switches_to_english(self):
        status, data = self._page("en")
        self.assertEqual(status, 200)
        body = data.decode("utf-8")
        self.assertIn(">Stop<", body)
        self.assertIn("Type a command", body)
        self.assertNotIn("停下", body)

    def test_lang_route_sets_the_cookie_and_comes_back(self):
        seen = {}
        status, _data = request(self.viewer.port, "/lang?l=en&s=" + self.SID, out=seen)
        self.assertEqual(status, 303)
        self.assertIn("pielang=en", seen.get("Set-Cookie", ""))
        self.assertIn("s=" + self.SID, seen.get("Location", ""))

    def test_env_default_applies_when_no_cookie(self):
        with mock.patch.object(server, "DEFAULT_LANG", "en"):
            status, data = self._page()
        self.assertEqual(status, 200)
        self.assertIn(">Stop<", data.decode("utf-8"))


class Concurrency(unittest.TestCase):
    def test_concurrent_fails_writes_stay_parsable(self):
        """A fixed .tmp name produced invalid JSON under load; a 12-thread burst is
        what found it, so it stays as a regression test."""
        import json

        errors = []

        def worker():
            try:
                for _ in range(20):
                    server._FAILS["10.0.0.%d" % (threading.get_ident() % 200)] = (1, 1.0)
                    server.save_fails()
                    json.loads(server.FAILS_FILE.read_text())
            except Exception as err:  # noqa: BLE001 - the assertion is "no error"
                errors.append(repr(err))

        threads = [threading.Thread(target=worker) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        server._FAILS.clear()
        server.save_fails()


class StrictUa(unittest.TestCase):
    def test_strict_applies_the_ua_gate_to_loopback_too(self):
        handler = base.fake_handler(cookie="pieink=" + TOKEN, ua="curl/8", ip="127.0.0.1")
        handler.headers["User-Agent"] = "curl/8"
        with mock.patch.dict("os.environ", {"EINK_UA_STRICT": "1"}):
            ok, _extra, reason = handler._authorized({})
        self.assertFalse(ok)
        self.assertIn("ua not allowed", reason)


class ProxyBehaviour(unittest.TestCase):
    """The viewer is a proxy: whatever the bridge answers has to reach the page
    unchanged, and a missing bridge must say so instead of looking successful."""

    @classmethod
    def setUpClass(cls):
        cls.bridge = ThreadingHTTPServer(("127.0.0.1", 0), FakeBridge)
        cls.bridge_port = cls.bridge.server_address[1]
        threading.Thread(target=cls.bridge.serve_forever, daemon=True).start()
        cls.viewer = Viewer()

    @classmethod
    def tearDownClass(cls):
        cls.viewer.close()
        cls.bridge.shutdown()
        cls.bridge.server_close()

    def setUp(self):
        FakeBridge.seen = []
        FakeBridge.reply_status = 200
        FakeBridge.reply_body = b'{"ok": true, "msg": "fake bridge"}'
        FakeBridge.reply_headers = []
        FakeBridge.reply_body = b'{"ok": true, "msg": "fake bridge"}'
        FakeBridge.reply_headers = []
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(server.VIEW_FILE.unlink, missing_ok=True)
        for name, value in (("caps_for", lambda _h: "full"),
                            ("bridge_port_for", lambda _sid: self.bridge_port)):
            mock.patch.object(server, name, value).start()

    def test_approve_relays_the_bridge_verdict(self):
        FakeBridge.reply_body = '{"ok": true, "msg": "已允许（fake）"}'.encode()
        status, data = request(self.viewer.port, "/act/approve", "POST", accept="application/json")
        self.assertEqual(status, 200)
        self.assertIn("已允许", json.loads(data)["msg"])
        self.assertEqual(FakeBridge.seen[0][1], "/act/approve")

    def test_bridge_error_status_is_forwarded_with_its_message(self):
        FakeBridge.reply_status = 409
        FakeBridge.reply_body = '{"ok": false, "msg": "该确认属于另一个会话"}'.encode()
        status, data = request(self.viewer.port, "/act/deny", "POST", accept="application/json")
        self.assertEqual(status, 409)
        self.assertIn("另一个会话", json.loads(data)["msg"])

    def test_plain_form_post_is_bounced_to_the_static_page(self):
        # no JSON accept header: an old browser cached before the inline handler
        FakeBridge.reply_body = '{"ok": true, "msg": "已发送"}'.encode()
        seen = {}
        status, _data = request(self.viewer.port, "/act/say", "POST", "text=hi", out=seen)
        self.assertEqual(status, 303)
        self.assertIn("/static?", seen.get("Location", ""))
        self.assertIn("msg=", seen.get("Location", ""))

    def test_non_json_bridge_body_falls_back_to_a_generic_verdict(self):
        FakeBridge.reply_body = b"<html>not json</html>"
        status, _data = request(self.viewer.port, "/act/say", "POST", "text=hi")
        self.assertEqual(status, 303)

    def test_post_without_a_running_bridge_is_502(self):
        with mock.patch.object(server, "bridge_port_for", lambda _sid: 0):
            status, data = request(self.viewer.port, "/act/say", "POST", "text=hi",
                                   accept="application/json")
        self.assertEqual(status, 502)
        self.assertIn("bridge", json.loads(data)["msg"])

    def test_done_page_is_relayed_from_the_bridge(self):
        with mock.patch.object(server, "bridge_port", lambda: self.bridge_port):
            status, data = request(self.viewer.port, "/done/say?m=ok")
        self.assertEqual(status, 200)
        self.assertIn("/done/say", FakeBridge.seen[0][1])

    def test_done_page_without_a_bridge_reports_it(self):
        with mock.patch.object(server, "bridge_port", lambda: 1):
            status, data = request(self.viewer.port, "/done/say?m=ok")
        self.assertEqual(status, 502)
        self.assertIn(b"bridge", data)

    def test_glyphs_page_lists_the_candidates(self):
        status, data = request(self.viewer.port, "/glyphs")
        self.assertEqual(status, 200)
        body = data.decode("utf-8")
        self.assertIn("盲文 (pi TUI)", body)     # default language is Chinese
        self.assertIn("[|]", body)

    def test_theme_switches_both_ways(self):
        for want, cookie in (("1", "piedark=1"), ("0", "piedark=0")):
            seen = {}
            status, _data = request(self.viewer.port, "/theme?d=" + want, out=seen)
            self.assertEqual(status, 303)
            self.assertIn(cookie, seen.get("Set-Cookie", ""))

    def test_lang_rejects_an_unknown_value(self):
        seen = {}
        status, _data = request(self.viewer.port, "/lang?l=klingon", out=seen)
        self.assertEqual(status, 303)
        self.assertIn("pielang=en", seen.get("Set-Cookie", ""))

    def test_footer_reports_a_stale_bridge(self):
        live = {"aaaabbbb-1111-2222-3333-444455556666": {"port": 1}}
        with mock.patch.object(server, "live_bridges", lambda: live), \
             mock.patch.object(server, "bridge_state", lambda: None), \
             mock.patch.object(server, "bridge_alive", lambda: True):
            status, data = request(self.viewer.port, "/data?s=aaaabbbb-1111-2222-3333-444455556666")
        self.assertEqual(status, 200)
        self.assertIn("bridge", json.loads(data)["status"])

    def test_busy_page_carries_the_spinner_into_the_digest(self):
        live = {"aaaabbbb-1111-2222-3333-444455556666": {"port": 1}}
        state = {"idle": False, "context": {"window": 200000, "percent": 3.5}}
        with mock.patch.object(server, "live_bridges", lambda: live), \
             mock.patch.object(server, "bridge_state", lambda: {"port": 1, "pid": os.getpid(),
                                                               "sessionFile": "x"}), \
             mock.patch.object(server, "agent_state", lambda _p=0: state):
            status, data = request(self.viewer.port, "/data?s=aaaabbbb-1111-2222-3333-444455556666")
        self.assertEqual(status, 200)
        payload = json.loads(data)
        self.assertTrue(payload["busy"])
        self.assertIn("200k", payload["status"])


class UnlockFlow(unittest.TestCase):
    """The two viewer-local actions: /act/unlock and /act/readonly."""

    @classmethod
    def setUpClass(cls):
        cls.viewer = Viewer()

    @classmethod
    def tearDownClass(cls):
        cls.viewer.close()

    def setUp(self):
        self.trust = server.TRUST_FILE
        self.saved = self.trust.read_text() if self.trust.exists() else None
        self.trust.write_text(json.dumps({"confirmed": [], "peers": [], "readonly": False}))
        self.addCleanup(self._restore)

    def _restore(self):
        if self.saved is None:
            self.trust.unlink(missing_ok=True)
        else:
            self.trust.write_text(self.saved)
        mock.patch.stopall()

    def test_unlock_with_the_right_pin_unlocks_this_network(self):
        with mock.patch.object(server, "peer_subnet", lambda _h: "192.168.3.0/24"), \
             mock.patch.object(server, "host_subnet", lambda _h: "192.168.3.0/24"):
            status, data = request(self.viewer.port, "/act/unlock", "POST",
                                   "k=" + (base.EINK / "pin").read_text().strip(),
                                   accept="application/json")
        self.assertEqual(status, 200)
        self.assertIn("解锁", json.loads(data)["msg"])
        state = json.loads(self.trust.read_text())
        self.assertIn("192.168.3.0/24", state["confirmed"])

    def test_unlock_with_a_wrong_pin_is_refused(self):
        status, data = request(self.viewer.port, "/act/unlock", "POST", "k=000000",
                               accept="application/json")
        self.assertEqual(status, 403)
        self.assertIn("PIN", json.loads(data)["msg"])
        self.assertEqual(json.loads(self.trust.read_text())["confirmed"], [])

    def test_readonly_action_switches_the_network_back(self):
        status, data = request(self.viewer.port, "/act/readonly", "POST", accept="application/json")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(self.trust.read_text())["readonly"])


class ProxyEdges(unittest.TestCase):
    """Bridge responses that are not the happy-path 200 JSON."""

    @classmethod
    def setUpClass(cls):
        cls.bridge = ThreadingHTTPServer(("127.0.0.1", 0), FakeBridge)
        cls.bridge_port = cls.bridge.server_address[1]
        threading.Thread(target=cls.bridge.serve_forever, daemon=True).start()
        cls.viewer = Viewer()

    @classmethod
    def tearDownClass(cls):
        cls.viewer.close()
        cls.bridge.shutdown()
        cls.bridge.server_close()

    def setUp(self):
        FakeBridge.seen = []
        FakeBridge.reply_status = 200
        FakeBridge.reply_body = b'{"ok": true, "msg": "fake bridge"}'
        FakeBridge.reply_headers = []
        FakeBridge.reply_status = 200
        FakeBridge.reply_body = b'{"ok": true, "msg": "fake bridge"}'
        FakeBridge.reply_headers = []
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(server.VIEW_FILE.unlink, missing_ok=True)
        mock.patch.object(server, "caps_for", lambda _h: "full").start()
        mock.patch.object(server, "bridge_port_for", lambda _sid: self.bridge_port).start()

    def test_a_bridge_redirect_is_relayed_as_a_redirect(self):
        FakeBridge.reply_status = 303
        FakeBridge.reply_body = b""
        with mock.patch.object(server, "bridge_port_for", lambda _sid: self.bridge_port):
            status, _data = request(self.viewer.port, "/act/say", "POST", "text=hi",
                                    accept="application/json")
        self.assertIn(status, (200, 303))

    def test_unreachable_bridge_reports_502(self):
        dead = ThreadingHTTPServer(("127.0.0.1", 0), FakeBridge)
        port = dead.server_address[1]
        dead.server_close()          # bound then released: connection refused
        with mock.patch.object(server, "bridge_port_for", lambda _sid: port):
            status, data = request(self.viewer.port, "/act/say", "POST", "text=hi",
                                   accept="application/json")
        self.assertEqual(status, 502)
        self.assertIn(b"bridge", data)

    def test_an_oversized_body_is_not_forwarded(self):
        status, _data = request(self.viewer.port, "/act/say", "POST", "x=" + "a" * 9000,
                                accept="application/json")
        # over the 8 KiB cap the body is dropped, not buffered: the page's own limit
        # is 2000 chars, so anything larger is a client bug
        self.assertEqual(FakeBridge.seen[0][2], "")
        self.assertEqual(status, 200)

    def test_post_is_routed_by_pending_pid_when_no_session_is_named(self):
        server.PENDING_FILE.write_text(json.dumps({"id": "p", "pid": os.getpid()}))
        self.addCleanup(server.PENDING_FILE.unlink, missing_ok=True)
        live = {"aaaa1111-2222-3333-4444-555566667777":
                {"port": self.bridge_port, "pid": os.getpid()}}
        with mock.patch.object(server, "live_bridges", lambda: live), \
             mock.patch.object(server, "rendered_session_id", lambda: ""):
            status, _data = request(self.viewer.port, "/act/approve", "POST",
                                    accept="application/json")
        self.assertEqual(status, 200)
        self.assertEqual(FakeBridge.seen[0][1], "/act/approve")


class HandlerEdges(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.viewer = Viewer()

    @classmethod
    def tearDownClass(cls):
        cls.viewer.close()

    def setUp(self):
        FakeBridge.seen = []
        FakeBridge.reply_status = 200
        FakeBridge.reply_body = b'{"ok": true, "msg": "fake bridge"}'
        FakeBridge.reply_headers = []
        self.addCleanup(mock.patch.stopall)

    def test_post_to_a_non_action_path_is_404(self):
        status, data = request(self.viewer.port, "/nope", "POST", "x=1", accept="application/json")
        self.assertEqual(status, 404)
        self.assertIn(b"not found", data)

    def test_post_without_a_credential_never_reaches_the_bridge(self):
        status, _data = request(self.viewer.port, "/act/say", "POST", "text=hi",
                                cookie=False, accept="application/json")
        self.assertEqual(status, 403)
        self.assertEqual(FakeBridge.seen, [])

    def test_an_internal_error_still_answers_the_device(self):
        with mock.patch.object(server, "caps_for", lambda _h: "full"), \
             mock.patch.object(server, "resolve_view", side_effect=RuntimeError("boom")):
            status, data = request(self.viewer.port, "/")
        self.assertEqual(status, 500)
        self.assertIn(b"RuntimeError", data)


class EndpointPages(unittest.TestCase):
    """Pages that are served by the viewer itself, not by the bridge."""

    @classmethod
    def setUpClass(cls):
        cls.viewer = Viewer()

    @classmethod
    def tearDownClass(cls):
        cls.viewer.close()

    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(server.VIEW_FILE.unlink, missing_ok=True)
        self.addCleanup(server.DIAG_LOG.unlink, missing_ok=True)
        server._DIAG_SEEN = None
        server._DIAG_SEEN_TIME.clear() if hasattr(server, "_DIAG_SEEN_TIME") else None
        mock.patch.object(server, "caps_for", lambda _h: "full").start()
        mock.patch.object(server, "bridge_port_for", lambda _sid: 0).start()

    def test_picker_page_lists_sessions(self):
        status, data = request(self.viewer.port, "/sessions")
        self.assertEqual(status, 200)
        self.assertIn(b'class="sess"', data)

    def test_static_page_renders_without_javascript(self):
        status, data = request(self.viewer.port, "/static")
        self.assertEqual(status, 200)
        self.assertIn(b"http-equiv=\"refresh\"", data)

    def test_diag_beacon_is_recorded_once(self):
        for _ in range(3):
            status, data = request(self.viewer.port, "/diag?ev=tap&a=%2Fact%2Fsay")
            self.assertEqual(status, 200)
        self.assertEqual(data, b"ok")
        log = server.DIAG_LOG.read_text()
        self.assertEqual(log.count("ev=tap"), 1)      # the same beacon is deduped

    def test_diag_rate_limit_answers_429(self):
        with mock.patch.object(server, "_diag_allow", lambda _ip: False):
            status, data = request(self.viewer.port, "/diag?ev=x")
        self.assertEqual(status, 429)
        self.assertIn(b"slow down", data)

    def test_ua_logging_records_a_peer_once(self):
        with mock.patch.dict(os.environ, {"EINK_LOG_UA": "1"}):
            server._UA_SEEN.clear()
            request(self.viewer.port, "/data")
            request(self.viewer.port, "/data")
            self.assertEqual(len(server._UA_SEEN), 1)

    def test_get_without_credential_is_refused(self):
        status, data = request(self.viewer.port, "/", cookie=False)
        self.assertEqual(status, 403)
        self.assertIn(b"denied", data)

    def test_favicon_is_served_for_both_names(self):
        for name in ("/favicon.ico", "/favicon.png"):
            status, data = request(self.viewer.port, name)
            self.assertEqual(status, 200)
            self.assertTrue(data.startswith(b"\x89PNG"))

    def test_corrupt_pending_file_does_not_break_a_post(self):
        server.PENDING_FILE.write_text("{not json")
        self.addCleanup(server.PENDING_FILE.unlink, missing_ok=True)
        with mock.patch.object(server, "rendered_session_id", lambda: ""):
            status, _data = request(self.viewer.port, "/act/approve", "POST",
                                    accept="application/json")
        self.assertEqual(status, 502)          # no session to route to, said out loud

    def test_bridge_redirect_reaches_a_javascript_client_as_303(self):
        bridge = ThreadingHTTPServer(("127.0.0.1", 0), FakeBridge)
        port = bridge.server_address[1]
        threading.Thread(target=bridge.serve_forever, daemon=True).start()
        self.addCleanup(bridge.server_close)
        self.addCleanup(bridge.shutdown)
        FakeBridge.seen = []
        FakeBridge.reply_status = 200
        FakeBridge.reply_body = b'{"ok": true, "msg": "fake bridge"}'
        FakeBridge.reply_headers = []
        FakeBridge.reply_status = 303
        FakeBridge.reply_body = b""
        FakeBridge.reply_headers = [("Location", "/done/say?m=ok")]
        with mock.patch.object(server, "bridge_port_for", lambda _sid: port):
            status, _data = request(self.viewer.port, "/act/say", "POST", "text=hi",
                                    accept="application/json")
        self.assertEqual(status, 303)

    def test_plain_form_failure_is_bounced_with_the_failure_text(self):
        bridge = ThreadingHTTPServer(("127.0.0.1", 0), FakeBridge)
        port = bridge.server_address[1]
        threading.Thread(target=bridge.serve_forever, daemon=True).start()
        self.addCleanup(bridge.server_close)
        self.addCleanup(bridge.shutdown)
        FakeBridge.seen = []
        FakeBridge.reply_status = 200
        FakeBridge.reply_body = b'{"ok": true, "msg": "fake bridge"}'
        FakeBridge.reply_headers = []
        FakeBridge.reply_status = 403
        FakeBridge.reply_body = '{"ok": false, "msg": "not in the allow list"}'.encode()
        FakeBridge.reply_headers = []
        seen = {}
        with mock.patch.object(server, "bridge_port_for", lambda _sid: port):
            status, _data = request(self.viewer.port, "/act/say", "POST", "text=hi", out=seen)
        self.assertEqual(status, 303)
        self.assertIn("msg=", seen.get("Location", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
