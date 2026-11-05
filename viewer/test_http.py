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
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import test_server as base  # noqa: E402  (sets up the temp HOME first)
import threading
import types
import urllib.parse
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
    # per-path bodies: the viewer asks the bridge for /state on every poll and for
    # /queue when it renders the queue page, and those are different shapes
    routes: dict = {}
    # paths an older bridge build simply does not have (it answers 404 "unknown action")
    missing: set = set()

    def _record(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        FakeBridge.seen.append((self.command, self.path, body.decode("utf-8", "replace"),
                               dict(self.headers)))
        route = self.path.split("?", 1)[0]
        if route in FakeBridge.missing:
            payload = b'{"ok": false, "msg": "unknown action"}'
            self.send_response(404)
        else:
            payload = FakeBridge.routes.get(route, FakeBridge.reply_body)
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

    def test_say_is_recorded_where_the_recall_button_reads_it(self):
        server.HISTORY_FILE.unlink(missing_ok=True)
        request(self.viewer.port, "/act/say", "POST", "text=hello", accept="application/json")
        status, data = request(self.viewer.port, "/hist")
        self.assertEqual(status, 200, data[:200])
        self.assertEqual(json.loads(data)["items"], ["hello"])

    def test_new_session_lands_on_the_window_it_opened(self):
        """`/act/new` spawns a pi in another directory, so its reply cannot name the new
        session: the viewer waits for that directory to register one and sends the page
        there - without the outcome note, which would otherwise sit above the input box
        of the page it just opened."""
        old, new = "aaaabbbb-1111-2222-3333-444455556666", "eeeeffff-1111-2222-3333-444455556666"
        pid = os.getpid()
        target = tempfile.mkdtemp(prefix="eink-proj-")
        self.addCleanup(shutil.rmtree, target, True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
        live = {old: {"pid": pid, "port": self.bridge_port, "cwd": "/tmp/elsewhere",
                      "updatedAt": stamp}}
        FakeBridge.reply_body = json.dumps({"ok": True, "msg": "\u5f00\u65b0\u7a97\u53e3"}).encode()

        def appear():
            time.sleep(0.3)   # the window comes up after the bridge answered
            live[new] = {"pid": pid, "port": self.bridge_port, "cwd": target,
                         "updatedAt": stamp}

        threading.Thread(target=appear, daemon=True).start()
        with mock.patch.object(server, "live_bridges", lambda: live):
            seen = {}
            status, _d = request(self.viewer.port, "/act/new?s=" + old, "POST",
                                 "dir=" + urllib.parse.quote(target), out=seen)
        self.assertEqual(status, 303)
        location = seen.get("Location", "")
        self.assertIn("s=" + new, location)
        self.assertNotIn("msg=", location)      # no stale line above the new page's box
        self.assertEqual(FakeBridge.seen[0][1], "/act/new")

    def test_an_unchanged_poll_ships_no_transcript(self):
        """The fragment is 14-26 KB; every poll used to carry it even when nothing had
        changed (1.4 MB/min through a 250ms/lossy tunnel, measured in nginx access.log).
        A poll whose digest the client already has must come back as a short "same"."""
        sid = "01234567-89ab-cdef-0123-456789abcdef"
        status, data = request(self.viewer.port, "/data?s=" + sid)
        self.assertEqual(status, 200)
        full = json.loads(data)
        self.assertIn("html", full)
        self.assertGreater(len(data), 500)

        status, data = request(self.viewer.port, "/data?s=" + sid + "&v=" + full["v"])
        self.assertEqual(status, 200)
        same = json.loads(data)
        self.assertTrue(same.get("same"))
        self.assertNotIn("html", same)
        self.assertNotIn("items", same)
        self.assertLess(len(data), 200)          # two orders of magnitude smaller

    def test_the_page_asks_with_the_digest_it_already_has(self):
        """Half a fix is worse than none: a server that can short-circuit is no use if
        the page never sends its digest back."""
        status, data = request(self.viewer.port, "/?s=01234567-89ab-cdef-0123-456789abcdef")
        self.assertEqual(status, 200)
        self.assertIn('encodeURIComponent(V)', data.decode("utf-8"))

    def test_the_box_comes_back_when_the_keyboard_resizes_the_viewport(self):
        """Unchanged polls no longer repaint, and the repaint was what scrolled the
        page to the bottom - without this the input box stays under the soft keyboard."""
        status, data = request(self.viewer.port, "/?s=01234567-89ab-cdef-0123-456789abcdef")
        self.assertEqual(status, 200)
        html = data.decode("utf-8")
        self.assertIn('addEventListener("resize"', html)
        self.assertIn("function showBox()", html)

    def test_a_stale_digest_gets_the_whole_fragment_again(self):
        status, data = request(self.viewer.port, "/data?s=01234567-89ab-cdef-0123-456789abcdef&v=deadbeef0000")
        self.assertEqual(status, 200)
        self.assertIn("html", json.loads(data))

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


class PerPageSession(unittest.TestCase):
    """Each page follows its own session.

    The viewer used to keep one "the session" slot for everybody, so a page that did
    not name a session re-derived it on every poll: browsing on the laptop, or a new pi
    session starting there, swapped the conversation under the Kindle. The page now
    pins the id it resolved into every request it makes, and actions follow that id.
    """

    A = "aaaabbbb-1111-2222-3333-444455556666"
    B = "ccccdddd-1111-2222-3333-444477778888"

    @classmethod
    def setUpClass(cls):
        cls.viewer = Viewer()

    @classmethod
    def tearDownClass(cls):
        cls.viewer.close()

    def setUp(self):
        folder = server.SESSIONS / "--perpage--"
        folder.mkdir(parents=True, exist_ok=True)
        for sid, cwd in ((self.A, "/tmp/aaa"), (self.B, "/tmp/bbb")):
            (folder / ("2026-03-03T00-00-00-000Z_%s.jsonl" % sid)).write_text(
                json.dumps({"type": "session", "id": sid, "cwd": cwd}) + "\n")
        self.addCleanup(shutil.rmtree, folder, True)
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(server, "live_bridges", lambda: {}).start()
        mock.patch.object(server, "caps_for", lambda _h: "full").start()

    def test_the_shell_polls_the_session_it_shows(self):
        status, data = request(self.viewer.port, "/?s=" + self.B)
        self.assertEqual(status, 200)
        self.assertIn(b"/data?s=" + self.B.encode(), data)

    def test_a_page_keeps_its_own_session_while_another_device_picks_one(self):
        server.VIEW_FILE.write_text(json.dumps({"sid": self.B}))
        self.addCleanup(server.VIEW_FILE.unlink, missing_ok=True)
        status, data = request(self.viewer.port, "/data?s=" + self.A)
        self.assertEqual(status, 200)
        foot = json.loads(data)["foot"]
        self.assertIn(self.A, foot)
        self.assertNotIn(self.B, foot)

    def test_a_first_visit_pins_what_it_renders(self):
        server.VIEW_FILE.unlink(missing_ok=True)
        seen = {}
        status, _d = request(self.viewer.port, "/", out=seen)
        self.assertEqual(status, 303)
        self.assertRegex(seen.get("Location", ""), r"/\?s=[0-9a-f-]{8,}")

    def test_a_bare_visit_pins_even_when_a_session_is_already_remembered(self):
        """The old pin only fired when view.json was empty, i.e. never on a viewer in
        daily use: every page then re-derived its session from that one global slot."""
        server.VIEW_FILE.write_text(json.dumps({"sid": self.A}))
        self.addCleanup(server.VIEW_FILE.unlink, missing_ok=True)
        seen = {}
        status, _d = request(self.viewer.port, "/", out=seen)
        self.assertEqual(status, 303)
        self.assertIn("s=" + self.A, seen.get("Location", ""))

    def test_another_devices_pick_does_not_move_an_open_page(self):
        server.VIEW_FILE.write_text(json.dumps({"sid": self.A}))
        self.addCleanup(server.VIEW_FILE.unlink, missing_ok=True)
        seen = {}
        request(self.viewer.port, "/", out=seen)          # bare entry: pinned to A
        poll = seen["Location"]                            # what that page now polls
        server.VIEW_FILE.write_text(json.dumps({"sid": self.B}))   # the laptop browsed
        status, data = request(self.viewer.port, poll.replace("/?", "/data?", 1))
        self.assertEqual(status, 200)
        foot = json.loads(data)["foot"]
        self.assertIn(self.A, foot)
        self.assertNotIn(self.B, foot)

    def test_actions_route_to_the_session_the_page_names(self):
        seen = []

        def port_for(sid):
            seen.append(sid)
            return 0

        with mock.patch.object(server, "bridge_port_for", port_for), \
             mock.patch.object(server, "rendered_session_id", lambda: self.A):
            request(self.viewer.port, "/act/stop?s=" + self.B, "POST", accept="application/json")
        self.assertEqual(seen[-1], self.B)

    def test_the_picker_links_back_to_the_session_it_was_opened_from(self):
        status, data = request(self.viewer.port, "/sessions?s=" + self.B)
        self.assertEqual(status, 200)
        head = data.decode("utf-8").split('<div class="sess">', 1)[0]   # the back link only
        self.assertIn('href="/?s=%s"' % self.B, head)


class OlderBridge(QueuePage.__mro__[1] if False else unittest.TestCase):
    """A viewer that is newer than the bridge it drives.

    That is the normal state right after an upgrade: the viewer is restarted, each pi
    process keeps the extension it loaded until someone runs /reload. Everything the
    page renders has to degrade honestly - no link to a page that cannot work, and no
    "this session is not running" for a session that is plainly running.
    """

    SID = "aaaabbbb-1111-2222-3333-444455556666"

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
        FakeBridge.reply_headers = []
        FakeBridge.routes = {
            # an older /state: running, but no `queued` field at all
            "/state": json.dumps({"idle": False, "pending": False}).encode(),
        }
        FakeBridge.missing = {"/queue", "/act/new", "/act/qadd"}
        self.addCleanup(setattr, FakeBridge, "missing", set())
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(server, "caps_for", lambda _h: "full").start()
        mock.patch.object(server, "bridge_port_for", lambda _sid: self.bridge_port).start()
        mock.patch.object(server, "live_bridges", lambda: {}).start()

    def test_the_footer_does_not_offer_a_queue_the_bridge_cannot_serve(self):
        status, data = request(self.viewer.port, "/data?s=" + self.SID)
        self.assertEqual(status, 200)
        foot = json.loads(data)["foot"]
        self.assertNotIn("/queue", foot)
        self.assertNotIn("排队中（0）", foot)

    def test_the_queue_page_says_old_bridge_not_session_gone(self):
        status, data = request(self.viewer.port, "/queue?s=" + self.SID)
        self.assertEqual(status, 200)
        body = data.decode("utf-8")
        self.assertIn("旧版", body)
        self.assertNotIn("未在运行", body)

    def test_a_new_session_page_does_not_offer_an_old_bridge(self):
        live = {"aaaabbbb-1111-2222-3333-444455556666":
                {"port": self.bridge_port, "pid": os.getpid(), "cwd": "/tmp/demo",
                 "updatedAt": "2026-01-01T00:00:00Z"}}
        with mock.patch.object(server, "live_bridges", lambda: live):
            status, data = request(self.viewer.port, "/new?s=" + self.SID)
        self.assertEqual(status, 200)
        body = data.decode("utf-8")
        self.assertNotIn("/act/new", body)
        self.assertIn("/reload", body)

    def test_a_new_session_tap_explains_the_old_bridge(self):
        status, data = request(self.viewer.port, "/act/new?s=" + self.SID, "POST", "",
                               accept="application/json")
        self.assertEqual(status, 404)
        self.assertIn("旧版", json.loads(data)["msg"])


class QueuePage(unittest.TestCase):
    """The queue page: what the box typed while the agent was busy.

    The list, the count and the buttons all come from the session's bridge - pi's own
    queue is only a boolean to an extension, so the bridge is the owner and the viewer
    never keeps a copy of it.
    """

    SID = "aaaabbbb-1111-2222-3333-444455556666"

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
        FakeBridge.routes = {
            "/queue": json.dumps({"items": [{"i": 7, "text": "first", "at": 1767225600000},
                                            {"i": 9, "text": "second", "at": 1767225660000}],
                                  "pending": False, "armed": True}).encode(),
            "/state": json.dumps({"idle": False, "pending": False, "queued": 2,
                                  "spawn": True}).encode(),
        }
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(server, "caps_for", lambda _h: "full").start()
        mock.patch.object(server, "bridge_port_for", lambda _sid: self.bridge_port).start()
        mock.patch.object(server, "live_bridges", lambda: {}).start()

    def test_the_new_session_page_offers_directories(self):
        root = pathlib.Path(tempfile.mkdtemp(prefix="eink-projects-"))
        (root / "alpha").mkdir()
        self.addCleanup(shutil.rmtree, root, True)
        with mock.patch.object(server, "PROJECT_ROOTS", [str(root)]):
            status, data = request(self.viewer.port, "/new?s=" + self.SID)
        self.assertEqual(status, 200)
        body = data.decode("utf-8")
        self.assertIn('/act/new?s=' + self.SID, body)
        self.assertIn('name="dir" value="%s"' % (root / "alpha"), body)

    def test_the_page_lists_the_queue_with_per_row_controls(self):
        status, data = request(self.viewer.port, "/queue?s=" + self.SID)
        self.assertEqual(status, 200)
        body = data.decode("utf-8")
        self.assertIn("排队中（2 条）", body)
        self.assertIn("first", body)
        self.assertIn("second", body)
        self.assertIn('action="/act/qdel?s=%s"' % self.SID, body)
        self.assertIn('action="/act/qclear?s=%s"' % self.SID, body)
        self.assertIn('action="/act/qadd?s=%s"' % self.SID, body)
        self.assertIn('/queue/edit?s=%s&amp;i=7' % self.SID, body)
        # a button that cannot act is not rendered: no 上移 on the first row, no 下移
        # on the last one
        self.assertEqual(body.count("上移"), 1)
        self.assertEqual(body.count("下移"), 1)

    def test_the_edit_page_prefills_that_message(self):
        status, data = request(self.viewer.port, "/queue/edit?s=%s&i=9" % self.SID)
        self.assertEqual(status, 200)
        body = data.decode("utf-8")
        self.assertIn("<textarea name=\"text\" rows=\"6\" maxlength=\"2000\">second</textarea>", body)
        self.assertIn('action="/act/qedit?s=%s"' % self.SID, body)
        self.assertIn('name="i" value="9"', body)

    def test_an_action_comes_back_to_the_queue_page(self):
        seen = {}
        status, _d = request(self.viewer.port, "/act/qdel?s=" + self.SID, "POST", "i=7", out=seen)
        self.assertEqual(status, 303)
        self.assertTrue(seen.get("Location", "").startswith("/queue?s=" + self.SID))
        self.assertIn("msg=", seen.get("Location", ""))
        self.assertEqual(FakeBridge.seen[0][1], "/act/qdel")
        self.assertEqual(FakeBridge.seen[0][2], "i=7")

    def test_the_footer_shows_the_count_and_links_to_the_page(self):
        status, data = request(self.viewer.port, "/data?s=" + self.SID)
        self.assertEqual(status, 200)
        foot = json.loads(data)["foot"]
        self.assertIn("排队中（2）", foot)
        self.assertIn('href="/queue?s=' + self.SID, foot)

    def test_a_queue_restored_with_a_resumed_session_waits_for_a_tap(self):
        FakeBridge.routes["/queue"] = json.dumps({
            "items": [{"i": 1, "text": "old", "at": 1767225600000}],
            "pending": False, "armed": False}).encode()
        status, data = request(self.viewer.port, "/queue?s=" + self.SID)
        self.assertEqual(status, 200)
        body = data.decode("utf-8")
        self.assertIn('action="/act/qrun?s=%s"' % self.SID, body)
        self.assertIn("开始投递", body)
        self.assertIn("不会自己跑", body)

    def test_the_footer_link_is_there_even_when_idle_and_empty(self):
        # it disappeared while the agent was idle with nothing queued, which is exactly
        # when a reader goes looking for it
        FakeBridge.routes["/state"] = json.dumps({"idle": True, "pending": False, "queued": 0,
                                                  "spawn": True}).encode()
        status, data = request(self.viewer.port, "/data?s=" + self.SID)
        self.assertEqual(status, 200)
        foot = json.loads(data)["foot"]
        self.assertIn("排队中（0）", foot)
        self.assertIn('href="/queue?s=' + self.SID, foot)

    def test_an_armed_queue_has_no_deliver_button(self):
        status, data = request(self.viewer.port, "/queue?s=" + self.SID)
        self.assertEqual(status, 200)
        self.assertNotIn("/act/qrun", data.decode("utf-8"))

    def test_starting_delivery_comes_back_to_the_queue_page(self):
        seen = {}
        status, _d = request(self.viewer.port, "/act/qrun?s=" + self.SID, "POST", "", out=seen)
        self.assertEqual(status, 303)
        self.assertTrue(seen.get("Location", "").startswith("/queue?s=" + self.SID))

    def test_a_readonly_network_cannot_mutate_the_queue(self):
        with mock.patch.object(server, "caps_for", lambda _h: "readonly"):
            for action in ("qadd", "qdel", "qedit", "qmove", "qclear", "qrun"):
                status, _d = request(self.viewer.port, "/act/%s?s=%s" % (action, self.SID),
                                     "POST", "i=7&text=x", accept="application/json")
                self.assertEqual(status, 403, action)
        self.assertEqual([s for s in FakeBridge.seen if s[0] == "POST"], [])

    def test_a_session_without_a_bridge_has_no_queue(self):
        with mock.patch.object(server, "bridge_port_for", lambda _sid: 0):
            status, data = request(self.viewer.port, "/queue?s=" + self.SID)
        self.assertEqual(status, 200)
        self.assertIn("未在运行", data.decode("utf-8"))
        self.assertEqual(FakeBridge.seen, [])


class ReadOnlyPages(unittest.TestCase):
    """A read-only network may not see the project list: it exposes every directory this
    machine runs pi in. `/sessions` has been refused for that reason from the start;
    `/new` lists the same directories and needs the same rule."""

    @classmethod
    def setUpClass(cls):
        cls.viewer = Viewer()

    @classmethod
    def tearDownClass(cls):
        cls.viewer.close()

    def test_the_project_list_is_refused_on_a_readonly_network(self):
        for name, value in (("caps_for", lambda _h: "readonly"),
                            ("live_bridges", lambda: {"x": {"cwd": "/secret/project",
                                                           "updatedAt": "2026-01-01T00:00:00Z"}})):
            patcher = mock.patch.object(server, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for path in ("/new", "/sessions"):
            status, data = request(self.viewer.port, path)
            self.assertEqual(status, 403, path)
            self.assertNotIn(b"/secret/project", data, path)


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

    def test_footer_reports_a_bridge_that_does_not_answer(self):
        # registered for this session, but nothing is listening on that port
        with mock.patch.object(server, "bridge_port_for", lambda _sid: 1):
            status, data = request(self.viewer.port, "/data?s=aaaabbbb-1111-2222-3333-444455556666")
        self.assertEqual(status, 200)
        self.assertIn("bridge", json.loads(data)["status"])

    def test_footer_says_nothing_when_this_sessions_bridge_answers(self):
        # the fake bridge serves /state, so this session is fine - and the line must
        # not complain just because some OTHER session registered last (bridge.json)
        status, data = request(self.viewer.port, "/data?s=aaaabbbb-1111-2222-3333-444455556666")
        self.assertEqual(status, 200)
        self.assertNotIn("bridge", json.loads(data)["status"])

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
        # percent, not the raw window size: the window total was dropped from the
        # one-line status (see render()); the percentage is what the reader acts on.
        self.assertIn("4%", payload["status"])


class SecondFactor(unittest.TestCase):
    """P1: a password alone must not open a page that can drive a shell-capable agent.

    The code prompt appears only after the password/token gates pass, and the cookie it
    sets is a session secret - not the token.
    """

    @classmethod
    def setUpClass(cls):
        cls.viewer = Viewer()

    @classmethod
    def tearDownClass(cls):
        cls.viewer.close()

    def setUp(self):
        server._OTP_SESSIONS.clear()
        self.secret = "JBSWY3DPEHPK3PXP"
        self.pdir = pathlib.Path(tempfile.mkdtemp(prefix="pi-eink-totp-"))
        (self.pdir / "totp").write_text(self.secret)
        for name, value in (("TOTP_FILE", self.pdir / "totp"),
                            ("caps_for", lambda _h: "full"),
                            ("bridge_port_for", lambda _s: 0),
                            ("rendered_session_id", lambda: "session-id")):
            p = mock.patch.object(server, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(server._OTP_SESSIONS.clear)
        server._OTP_CACHE[0] = None
        server._OTP_CACHE[1] = None
        self.addCleanup(shutil.rmtree, self.pdir, True)

    def _req(self, path, method="GET", body=None, otp=None, header_token=None, cookie=True):
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", self.viewer.port, timeout=5)
        headers = {"User-Agent": KINDLE_UA}
        ck = []
        if cookie:
            ck.append("pieink=" + TOKEN)
        if otp:
            ck.append("pieink_otp=" + otp)
        if ck:
            headers["Cookie"] = "; ".join(ck)
        if header_token:
            headers["X-Eink-Token"] = header_token
        payload = body.encode() if body is not None else None
        if payload is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        conn.request(method, path, body=payload, headers=headers)
        res = conn.getresponse()
        data = res.read()
        hdrs = dict(res.getheaders())
        conn.close()
        return res.status, data, hdrs

    def test_page_asks_for_the_code_instead_of_rendering(self):
        status, data, _h = self._req("/")
        self.assertEqual(status, 200)
        self.assertIn("动态码".encode("utf-8"), data)

    def test_wrong_code_is_refused(self):
        status, data, _h = self._req("/otp", "POST", "c=000000")
        self.assertEqual(status, 403)
        self.assertIn("动态码".encode("utf-8"), data)

    def test_correct_code_opens_the_page(self):
        code = server.totp_code(self.secret)
        status, _d, hdrs = self._req("/otp", "POST", "c=" + code)
        self.assertEqual(status, 303)
        cookie = hdrs.get("Set-Cookie", "")
        self.assertIn("pieink_otp=", cookie)
        otp = cookie.split("pieink_otp=", 1)[1].split(";", 1)[0]
        # the session secret must not be the master token
        self.assertNotEqual(otp, TOKEN)
        # "/" without ?s= answers 303 (it pins the session id into the URL)
        status, data, _h = self._req("/", otp=otp)
        self.assertIn(status, (200, 303))
        self.assertNotIn("动态码".encode("utf-8"), data)

    def test_otp_cookie_alone_does_not_replace_the_password(self):
        code = server.totp_code(self.secret)
        _s, _d, hdrs = self._req("/otp", "POST", "c=" + code)
        otp = hdrs.get("Set-Cookie", "").split("pieink_otp=", 1)[1].split(";", 1)[0]
        status, _d, _h = self._req("/", otp=otp, cookie=False)
        self.assertEqual(status, 403)

    def test_token_in_a_header_is_accepted_without_any_query_secret(self):
        code = server.totp_code(self.secret)
        _s, _d, hdrs = self._req("/otp", "POST", "c=" + code)
        otp = hdrs.get("Set-Cookie", "").split("pieink_otp=", 1)[1].split(";", 1)[0]
        status, data, _h = self._req("/", otp=otp, cookie=False, header_token=TOKEN)
        # 303 on a first visit with no ?s=: the page pins the session id into the URL
        self.assertIn(status, (200, 303))
        self.assertNotIn("动态码".encode("utf-8"), data)

    def test_session_survives_a_restart(self):
        """A viewer restart used to invalidate every 2FA session, so each device had
        to be handed a fresh code by whoever ran the box."""
        store = pathlib.Path(tempfile.mkdtemp(prefix="pi-eink-otpstore-"))
        self.addCleanup(shutil.rmtree, store, True)
        with mock.patch.object(server, "OTP_STORE", store / "s.json"):
            code = server.totp_code(self.secret)
            _s, _d, hdrs = self._req("/otp", "POST", "c=" + code)
            otp = hdrs.get("Set-Cookie", "").split("pieink_otp=", 1)[1].split(";", 1)[0]
            self.assertTrue((store / "s.json").exists())
            server._OTP_SESSIONS.clear()          # simulate a restart: memory gone
            self.assertTrue(server.otp_session_ok(otp))
            self.assertFalse(server.otp_session_ok("0" * 32))

    def test_wrong_header_token_is_refused(self):
        code = server.totp_code(self.secret)
        _s, _d, hdrs = self._req("/otp", "POST", "c=" + code)
        otp = hdrs.get("Set-Cookie", "").split("pieink_otp=", 1)[1].split(";", 1)[0]
        status, _d, _h = self._req("/", otp=otp, cookie=False, header_token="nope")
        self.assertEqual(status, 403)


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


class ProxyHardening(unittest.TestCase):
    """The proxy is the only thing between a credential holder and the agent
    process: it forwards what it validated, never the raw request line."""

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
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(server, "caps_for", lambda _h: "full").start()
        mock.patch.object(server, "bridge_port_for", lambda _sid: self.bridge_port).start()
        mock.patch.object(server, "rendered_session_id", lambda: "session-id").start()

    def test_an_unknown_action_is_refused_before_the_bridge(self):
        status, data = request(self.viewer.port, "/act/nosuch", "POST", "x=1",
                               accept="application/json")
        self.assertEqual(status, 404, data[:200])
        self.assertEqual(FakeBridge.seen, [])

    def test_the_proxied_path_is_rebuilt_not_forwarded(self):
        # %2F keeps the slash out of the viewer's action segment; the bridge must
        # never see it either, whatever the bridge would do with it
        for path in ("/act/say%2F..%2F..%2Fstate", "/act/a/b/c", "/act/say/"):
            FakeBridge.seen = []
            status, _data = request(self.viewer.port, path, "POST", "text=hi",
                                    accept="application/json")
            self.assertEqual(status, 404, path)
            self.assertEqual(FakeBridge.seen, [], path)

    def test_a_known_action_is_forwarded_on_a_rebuilt_path(self):
        status, _data = request(self.viewer.port, "/act/say", "POST", "text=hi",
                                accept="application/json")
        self.assertEqual(status, 200)
        self.assertEqual(FakeBridge.seen[0][1], "/act/say")

    def test_an_action_routes_to_the_session_its_page_names(self):
        """A page carries its own session in every form, so the button has to follow it -
        routing from one global "rendered" slot sent taps into the other conversation.
        Not a widening of authority: the same credential can open that session with
        `?s=` anyway."""
        entry = {"port": self.bridge_port, "pid": os.getpid(), "sessionFile": "/tmp/x.jsonl"}
        with mock.patch.object(server, "live_bridges", lambda: {"other-project": entry}):
            status, _data = request(self.viewer.port, "/act/say?s=other-project", "POST",
                                    "text=hi", accept="application/json")
        self.assertEqual(status, 200)
        self.assertEqual(FakeBridge.seen[0][1], "/act/say")
        self.assertEqual(FakeBridge.seen[0][3].get("Cookie"),
                         "pieink=" + server.token())

    def test_an_action_naming_a_gone_session_is_refused_not_retargeted(self):
        # the page says which session it is about; quietly steering the one the viewer
        # happens to render is the "tapped 停下, another project stopped" bug
        status, data = request(self.viewer.port, "/act/say?s=this-session-is-gone", "POST",
                               "text=hi", accept="application/json")
        self.assertEqual(status, 502, data[:200])
        self.assertEqual(FakeBridge.seen, [])

    def test_the_client_cookie_is_not_relayed_to_the_bridge(self):
        request(self.viewer.port, "/act/say", "POST", "text=hi", accept="application/json")
        self.assertEqual(FakeBridge.seen[0][3].get("Cookie"),
                         "pieink=" + server.token())

    def test_done_rebuilds_its_request_and_never_follows_a_redirect(self):
        with mock.patch.object(server, "bridge_port", lambda: self.bridge_port):
            FakeBridge.reply_status = 302
            FakeBridge.reply_headers = [("Location", "http://127.0.0.1:1/harvest")]
            status, _data = request(self.viewer.port, "/done/say?m=ok&k=secret-token")
            self.assertEqual(status, 502)   # the redirect is not followed
            # only m travels; k never reaches the bridge
            self.assertEqual(FakeBridge.seen[0][1], "/done/say?m=ok")
            # the cookie is the server's own, not the client header relayed
            self.assertEqual(FakeBridge.seen[0][3].get("Cookie"),
                             "pieink=" + server.token())

    def test_agent_state_does_not_follow_a_redirect(self):
        # a hostile or colliding service on a bridge port must not be able to
        # bounce the viewer's credentialed request somewhere else
        FakeBridge.reply_status = 302
        FakeBridge.reply_headers = [("Location", "http://127.0.0.1:1/harvest")]
        with mock.patch.object(server, "bridge_port", lambda: self.bridge_port):
            self.assertIsNone(server.agent_state(self.bridge_port))
        self.assertEqual(FakeBridge.seen[0][1], "/state")
        # the request carried only the viewer's own token, built by the server
        self.assertEqual(FakeBridge.seen[0][3].get("Cookie"),
                          "pieink=" + server.token())


if __name__ == "__main__":
    unittest.main(verbosity=2)
