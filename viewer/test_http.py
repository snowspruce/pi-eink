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

    def _record(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        FakeBridge.seen.append((self.command, self.path, body.decode("utf-8", "replace"),
                               dict(self.headers)))
        payload = b'{"ok": true, "msg": "fake bridge"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
