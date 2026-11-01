"""HTTP-level tests: the enforcement point, not just the decision functions.

Every earlier test drove `caps_for()` / `action_allowed()` directly, so deleting the
`action_allowed` call in the handler kept the whole suite green. These start a real
viewer server against a fake bridge and check the wire behaviour.
"""

import pathlib
import sys

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
        FakeBridge.seen.append((self.command, self.path, body.decode("utf-8", "replace")))
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


def request(port, path, method="GET", body=None, cookie=True, accept=None):
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"User-Agent": KINDLE_UA}
    if cookie:
        headers["Cookie"] = "pieink=" + TOKEN
    if accept:
        headers["Accept"] = accept
    payload = body.encode() if body is not None else None
    if payload is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    conn.request(method, path, body=payload, headers=headers)
    res = conn.getresponse()
    data = res.read()
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

    def test_actions_are_routed_to_the_rendered_session(self):
        request(self.viewer.port, "/act/stop", "POST", accept="application/json")
        self.assertEqual(FakeBridge.seen[0][1], "/act/stop")


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
