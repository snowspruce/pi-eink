"""Rendering, parsing and small-helper tests.

These cover the layers the HTTP tests only touch incidentally: markdown -> html,
one row -> one message block, the session picker, the approval block, the trust
helpers, and the session-resolution glue. Assertions are on behaviour (what the
page would show), not on call counts.
"""

import json
import os
import pathlib
import re
import shutil
import sys
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import test_server as base  # noqa: E402  (sets up the temp HOME first)

server = base.server
EINK = base.EINK
SID = "aaaabbbb-1111-2222-3333-444455556666"


def write_session(folder: str, name: str, rows: list) -> pathlib.Path:
    d = server.SESSIONS / folder
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
    return p


def clean(folder: str) -> None:
    shutil.rmtree(server.SESSIONS / folder, ignore_errors=True)


class BridgeNote(unittest.TestCase):
    """The status line must describe the session the page shows, not the bridge that
    registered last (bridge.json) - two parallel pi sessions made it lie."""

    def setUp(self):
        self.path = write_session("--bridgenote--", "s.jsonl", [
            {"type": "session", "id": SID, "cwd": "/tmp/demo"},
            {"type": "message", "id": "m", "timestamp": "t",
             "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}},
        ])

    def tearDown(self):
        clean("--bridgenote--")

    def _status(self, port, agent_state):
        with mock.patch.object(server, "agent_state", lambda _p=0: agent_state):
            return server.render(self.path, 20, False, False, port=port)[2]

    def test_live_bridge_for_this_session_says_nothing(self):
        status = self._status(8123, {"idle": True})
        self.assertNotIn("bridge", status)

    def test_no_bridge_for_this_session_is_reported(self):
        self.assertIn("bridge 未激活", self._status(0, None))

    def test_registered_but_silent_bridge_is_reported_as_stale(self):
        self.assertIn("bridge 旧版", self._status(8123, None))


class Markdown(unittest.TestCase):
    def test_bold_italic_strike_and_code(self):
        out = server.md("**b** *i* ~~s~~ `c`")
        for frag in ("<b>b</b>", "<i>i</i>", "<s>s</s>", "<code>c</code>"):
            self.assertIn(frag, out)

    def test_code_span_is_not_italicised(self):
        # a `*` inside backticks must survive as text
        out = server.inline("`a*b*c`")
        self.assertIn("<code>a*b*c</code>", out)
        self.assertNotIn("<i>", out)

    def test_links_become_underlined_text(self):
        self.assertIn("<u>docs</u>", server.inline("[docs](http://example.com/x)"))

    def test_html_in_input_is_escaped(self):
        out = server.inline("<script>alert(1)</script>")
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)

    def test_headings_bullets_numbers_rules_quotes(self):
        html = server.md("# H1\n## H2\n\n- a\n- b\n\n1. one\n2. two\n\n---\n\n> quoted\n")
        self.assertIn("<h2>H1</h2>", html)      # one level down: it is a message, not a document
        self.assertIn("<h3>H2</h3>", html)
        self.assertIn("<li>a</li>", html)
        self.assertIn("<ol>", html)
        self.assertIn("<hr>", html)
        self.assertIn("<blockquote>quoted</blockquote>", html)

    def test_fenced_block_is_escaped_and_preformatted(self):
        html = server.md("```\nif a < b:\n    pass\n```\n")
        self.assertIn("<pre>", html)
        self.assertIn("a &lt; b", html)

    def test_single_line_fence_becomes_a_block(self):
        html = server.md("```ls -la```")
        self.assertIn("<pre>ls -la</pre>", html)

    def test_table_rendering_and_escaped_pipes(self):
        table = "| a | b |\n|---|---|\n| 1 \\| x | 2 |\n"
        html = server.md(table)
        self.assertIn("<table>", html)
        self.assertIn("1 | x", html)

    def test_table_cells_keeps_pipes_inside_code_spans(self):
        self.assertEqual(server.table_cells("| a `x|y` b | c |"), ["a `x|y` b", "c"])

    def test_table_cells_strips_outer_pipes_and_spaces(self):
        self.assertEqual(server.table_cells("|  one  |  two |"), ["one", "two"])

    def test_crlf_does_not_leak_into_output(self):
        self.assertNotIn("\r", server.md("a\r\nb\r\n"))

    def test_empty_and_none_input(self):
        # an empty message still gets a body div: the page digest must not change shape
        self.assertEqual(server.md(""), '<div class="body"></div>')
        self.assertEqual(server.md(None), '<div class="body"></div>')


class BlockText(unittest.TestCase):
    def test_text_thinking_and_toolcall(self):
        self.assertEqual(server.block_text({"type": "text", "text": "hi"}), ("text", "hi"))
        kind, text = server.block_text({"type": "thinking", "thinking": "hmm"})
        self.assertEqual(kind, "thinking")
        self.assertIn("hmm", text)
        kind, text = server.block_text({"type": "toolCall", "name": "bash", "arguments": {"cmd": "ls"}})
        self.assertEqual(kind, "toolcall")
        self.assertIn("bash", text)
        self.assertIn("ls", text)

    def test_unknown_block_falls_back_to_json(self):
        kind, text = server.block_text({"type": "weird", "v": 1})
        self.assertEqual(kind, "weird")
        self.assertIn('"v"', text)


class Rows(unittest.TestCase):
    def test_compaction_marker_shows_token_count(self):
        res = server.build_row({"type": "compaction", "tokensBefore": 12000}, 0, False, False)
        self.assertIn("上下文已压缩", res[1])
        self.assertIn("12k", res[1])

    def test_compaction_marker_without_count(self):
        res = server.build_row({"type": "compaction"}, 0, False, False)
        self.assertIn("上下文已压缩", res[1])
        self.assertNotIn("tokens", res[1])

    def test_english_language_marker(self):
        res = server.build_row({"type": "compaction"}, 0, False, False, "en")
        self.assertIn("context compacted", res[1])

    def test_non_message_rows_render_nothing(self):
        self.assertFalse(server.build_row({"type": "session"}, 0, False, False))
        self.assertFalse(server.build_row({"type": "message", "message": "not a dict"}, 0, False, False))
        self.assertFalse(server.build_row({"type": "message", "message": {"role": "system"}}, 0, False, False))

    def test_tool_result_hidden_unless_tools_enabled(self):
        row = {"type": "message", "id": "t1",
               "message": {"role": "toolResult", "toolName": "bash",
                           "content": [{"type": "text", "text": "out"}]}}
        self.assertFalse(server.build_row(row, 0, False, False))
        res = server.build_row(row, 0, False, True)
        self.assertIn("out", res[1])
        self.assertIn('class="mono "', res[1])

    def test_tool_result_error_class(self):
        row = {"type": "message", "id": "t2",
               "message": {"role": "toolResult", "toolName": "bash", "isError": True,
                           "content": [{"type": "text", "text": "boom"}]}}
        self.assertIn("mono err", server.build_row(row, 0, False, True)[1])

    def test_thinking_hidden_unless_enabled(self):
        row = {"type": "message", "id": "th",
               "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "deep"}]}}
        self.assertFalse(server.build_row(row, 0, False, False))
        self.assertIn("[thinking]", server.build_row(row, 0, True, False)[1])

    def test_tool_call_block(self):
        row = {"type": "message", "id": "tc",
               "message": {"role": "assistant",
                           "content": [{"type": "toolCall", "name": "read", "arguments": {"p": "x"}}]}}
        self.assertIn('class="toolname"', server.build_row(row, 0, False, True)[1])

    def test_user_and_assistant_labels(self):
        user = {"type": "message", "id": "u", "message": {"role": "user", "content": "hey"}}
        assistant = {"type": "message", "id": "a", "message": {"role": "assistant", "content": "yo"}}
        self.assertIn(">you<", server.build_row(user, 0, False, False)[1])
        self.assertIn(">agent<", server.build_row(assistant, 0, False, False)[1])

    def test_long_text_is_clipped(self):
        row = {"type": "message", "id": "long",
               "message": {"role": "assistant", "content": "x" * 5000}}
        self.assertIn("clipped", server.build_row(row, 0, False, False)[1])


class LoadRows(unittest.TestCase):
    def test_bad_lines_are_skipped(self):
        p = server.SESSIONS / "--load--" / "s.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"type":"message"}\nnot json\n\n{"type":"session"}\n')
        try:
            rows, _totals = server.load_rows(p)
            self.assertEqual(len(rows), 2)
        finally:
            clean("--load--")

    def test_missing_file_yields_nothing(self):
        rows, totals = server.load_rows(pathlib.Path("/nonexistent/nope.jsonl"))
        self.assertEqual(rows, [])
        self.assertEqual(totals["input"], 0)


class RenderPage(unittest.TestCase):
    def setUp(self):
        self.path = write_session("--render--", "s.jsonl", [
            {"type": "session", "id": SID, "cwd": "/tmp/demo"},
            {"type": "message", "id": "m1", "timestamp": "t1",
             "message": {"role": "user", "content": [{"type": "text", "text": "hello"}]}},
            {"type": "message", "id": "m2", "timestamp": "t2",
             "message": {"role": "assistant", "usage": {"input": 100, "output": 20,
                                                        "cacheRead": 50, "cost": {"total": 0.5}},
                         "content": [{"type": "text", "text": "world"}]}},
        ])

    def tearDown(self):
        clean("--render--")

    def test_fragment_status_and_foot(self):
        digest, frag, status, foot, items, busy = server.render(self.path, 20, False, False)
        self.assertIn("hello", frag)
        self.assertIn("world", frag)
        self.assertIn("↑100", status)
        # the dollar total is deliberately not on the page any more
        self.assertNotIn("$", status)
        self.assertIn("重绘", foot)
        self.assertFalse(busy)
        self.assertEqual(len(items), 2)
        self.assertTrue(digest)

    def test_non_string_text_block_does_not_break_the_page(self):
        """A session row whose text block is a JSON object used to 500 the whole page."""
        path = write_session("--render-obj--", "s.jsonl", [
            {"type": "session", "id": SID, "cwd": "/tmp/demo"},
            {"type": "message", "id": "m1", "timestamp": "t1",
             "message": {"role": "assistant",
                         "content": [{"type": "text", "text": {"weird": "object"}}]}},
        ])
        try:
            _d, frag, _s, _f, _i, _b = server.render(path, 20, False, False)
            self.assertIn("weird", frag)      # rendered as JSON, not a crash
        finally:
            clean("--render-obj--")

    def test_limit_keeps_the_tail(self):
        _d, frag, _s, _f, items, _b = server.render(self.path, 1, False, False)
        self.assertEqual(len(items), 1)
        self.assertIn("world", frag)
        self.assertNotIn("hello", frag)

    def test_busy_state_replaces_the_last_agent_card_with_a_spinner(self):
        with mock.patch.object(server, "agent_state", lambda _p=0: {"idle": False}):
            _d, frag, _s, _f, _i, busy = server.render(self.path, 20, False, False, port=1)
        self.assertTrue(busy)
        self.assertTrue(any(frame in frag for frame in server.SPIN))

    def test_busy_digest_is_stable_so_the_client_animates_the_spinner(self):
        # The frame used to be baked in, which made every tick a server round trip plus a
        # whole-card rewrite. If the digest starts changing with time again, this breaks.
        with mock.patch.object(server, "agent_state", lambda _p=0: {"idle": False}):
            d1, frag, _s, _f, _i, _b = server.render(self.path, 20, False, False, port=1)
            d2 = server.render(self.path, 20, False, False, port=1)[0]
        self.assertEqual(d1, d2)
        self.assertIn('class="spin"', frag)

    def test_busy_with_no_agent_card_yet_appends_one(self):
        path = write_session("--render2--", "s.jsonl", [
            {"type": "session", "id": SID, "cwd": "/tmp/demo"},
            {"type": "message", "id": "u1", "timestamp": "t1",
             "message": {"role": "user", "content": [{"type": "text", "text": "go"}]}},
        ])
        try:
            with mock.patch.object(server, "agent_state", lambda _p=0: {"idle": False}):
                _d, frag, _s, _f, _i, busy = server.render(path, 20, False, False, port=1)
            self.assertTrue(busy)
            self.assertIn('class="role assistant"', frag)
        finally:
            clean("--render2--")

    def test_note_is_rendered_inside_the_fragment(self):
        _d, frag, _s, _f, items, _b = server.render(self.path, 20, False, False,
                                                    note="会话 deadbeef 已不可用")
        self.assertIn("已不可用", frag)
        self.assertEqual(items[-1][0], "gone")

    def test_english_labels_in_the_footer(self):
        _d, _frag, _s, foot, _i, _b = server.render(self.path, 20, False, False, lang="en")
        self.assertIn("Redraw", foot)
        self.assertIn("Night", foot)
        self.assertIn("中文", foot)

    def test_missing_path_renders_an_empty_page(self):
        _d, frag, _s, _f, _i, _b = server.render(pathlib.Path("/nope.jsonl"), 20, False, False)
        self.assertIn("no messages yet", frag)


class QueueRendering(unittest.TestCase):
    """The queue page: what the box typed while the agent was busy."""

    ITEMS = [{"i": 4, "text": "first", "at": 1767225600000},
             {"i": 8, "text": "second", "at": 1767225660000}]

    def test_each_row_offers_only_what_it_can_do(self):
        html = server.queue_html(self.ITEMS, False, SID)
        self.assertEqual(html.count("/act/qmove"), 2)   # two rows, one direction each
        self.assertNotIn('value="-1"', html.split("/act/qmove")[1])  # first row: no 上移
        self.assertEqual(html.count("/act/qdel"), 2)
        self.assertIn('value="4"', html)
        self.assertIn('value="8"', html)

    def test_a_readonly_network_gets_the_list_without_buttons(self):
        # the mutations would only 403: rendering them is the fake control this
        # project keeps warning about
        html = server.queue_html(self.ITEMS, False, SID, "readonly")
        self.assertIn("first", html)
        self.assertNotIn("/act/qdel", html)
        self.assertNotIn("/act/qmove", html)
        self.assertIn("只读", html)

    def test_an_empty_queue_says_how_to_fill_it(self):
        html = server.queue_html([], False, SID)
        self.assertIn("队列空着", html)
        self.assertIn("/act/qadd", html)
        self.assertNotIn("/act/qclear", html)   # nothing to clear

    def test_text_is_escaped_and_bounded(self):
        html = server.queue_html([{"i": 1, "text": "<b>x</b>" + "y" * 900, "at": 0}], False, SID)
        self.assertNotIn("<b>x</b>", html)
        self.assertIn("&lt;b&gt;", html)
        self.assertLess(html.count("y"), 500)

    def test_pi_side_pending_is_admitted_not_hidden(self):
        html = server.queue_html(self.ITEMS, True, SID)
        self.assertIn("电脑端", html)

    def test_no_time_is_shown_when_the_item_has_none(self):
        # epoch 0 used to render as 08:00, i.e. a made-up timestamp
        html = server.queue_html([{"i": 1, "text": "x", "at": 0}], False, SID)
        meta = html.split('class="mono dim">')[1].split("</div>")[0]
        self.assertEqual(meta, "1")   # position only: no made-up clock

    def test_a_restored_queue_shows_the_deliver_button(self):
        html = server.queue_html(self.ITEMS, False, SID, armed=False)
        self.assertIn("/act/qrun", html)
        self.assertIn("开始投递", html)
        self.assertIn("不会自己跑", html)

    def test_an_armed_queue_has_no_deliver_button(self):
        self.assertNotIn("/act/qrun", server.queue_html(self.ITEMS, False, SID))

    def test_editing_escapes_the_message_into_the_textarea(self):
        html = server.queue_edit_html(4, "</textarea><script>x</script>", SID)
        self.assertNotIn("</textarea><script>", html)
        self.assertIn("&lt;/textarea&gt;", html)


class SessionPicker(unittest.TestCase):
    def test_live_bridges_are_listed_first(self):
        entry = {"port": 8123, "pid": os.getpid(), "cwd": "/tmp/live", "sessionFile": "/tmp/live.jsonl"}
        with mock.patch.object(server, "live_bridges", lambda: {SID: entry}):
            html = server.sessions_html()
        self.assertIn(SID, html)
        self.assertIn("运行中", html)

    def test_empty_state_says_so(self):
        with mock.patch.object(server, "live_bridges", lambda: {}), \
             mock.patch.object(server, "SESSIONS", pathlib.Path("/nonexistent")):
            html = server.sessions_html("en")
        self.assertIn("(no sessions)", html)

    def test_only_a_running_session_offers_a_new_session(self):
        """The button belongs on the running rows: a file has no process to create a
        session in, so a page-wide button would be a control that cannot work."""
        entry = {"port": 8123, "pid": os.getpid(), "cwd": "/tmp/live",
                 "sessionFile": "/tmp/live.jsonl", "updatedAt": "2026-01-01T00:00:00Z"}
        with mock.patch.object(server, "live_bridges", lambda: {SID: entry}):
            html = server.sessions_html()
        self.assertIn('action="/act/new?s=%s"' % SID, html)
        self.assertEqual(html.count("/act/new"), 1)

    def test_files_without_a_session_header_are_not_listed(self):
        """`*_transcript.jsonl` (subagent artifacts) have no session row: listing them
        produced a row whose id was the file stem, and tapping it could only fall back
        to another conversation."""
        path = write_session("--picker--", "aaaa_abc_transcript.jsonl", [
            {"type": "message", "id": "m", "timestamp": "t",
             "message": {"role": "user", "content": "subagent log"}},
        ])
        self.addCleanup(clean, "--picker--")
        self.assertTrue(path.exists())
        with mock.patch.object(server, "live_bridges", lambda: {}):
            html = server.sessions_html()
        self.assertNotIn("transcript", html)
        self.assertNotIn("subagent log", html)

    def test_every_row_a_reader_can_tap_resolves(self):
        """The picker is only useful if `?s=` from it renders that session: a row whose
        id cannot be resolved answers "gone" and shows the running conversation again,
        which is what "tapping a session does nothing" looked like."""
        # the header id and the file name disagree here, the way they do for real
        # sessions that were written into an existing file, and this id appears in no
        # file name at all
        unique = "99990000-1111-2222-3333-444488889999"
        write_session("--picker--", "2026-01-01T00-00-00-000Z_ffffffff-0000-1111-2222-333333333333.jsonl", [
            {"type": "session", "id": unique, "cwd": "/tmp/picker"},
        ])
        self.addCleanup(clean, "--picker--")
        with mock.patch.object(server, "live_bridges", lambda: {}):
            html = server.sessions_html()
        links = re.findall(r'href="/\?s=([^"]+)"', html)
        self.assertIn(unique, links)
        for sid in links:
            self.assertTrue(server.session_selected(sid), sid)


class IndexMemo(unittest.TestCase):
    """One request resolves the same id several times; one walk must serve them all.
    Caching longer than a request would be worse than slow: a stale id reads as "gone",
    which is the failure that swaps the conversation."""

    def setUp(self):
        real = server.SESSIONS
        self.addCleanup(setattr, server, "SESSIONS", real)

        class CountingPath:
            walks = 0

            def __init__(self, inner):
                self.inner = inner

            def rglob(self, pattern):
                CountingPath.walks += 1
                return self.inner.rglob(pattern)

            def __getattr__(self, name):
                return getattr(self.inner, name)

        CountingPath.walks = 0
        self.walks = lambda: CountingPath.walks
        server.SESSIONS = CountingPath(real)
        # the per-request memo is only cleared by the HTTP handlers; a test that opens a
        # request scope has to close it, or the next test gets a stale index
        self.addCleanup(setattr, server._INDEX_MEMO, "active", False)
        self.addCleanup(setattr, server._INDEX_MEMO, "value", None)
        server._INDEX_MEMO.value = None

    def test_repeated_lookups_inside_one_request_walk_once(self):
        server.begin_request()
        for _ in range(4):
            server.session_path_by_id(SID)
        self.assertEqual(self.walks(), 1)

    def test_direct_calls_outside_a_request_always_rebuild(self):
        server._INDEX_MEMO.active = False
        server._INDEX_MEMO.value = None
        server.session_path_by_id(SID)
        server.session_path_by_id(SID)
        self.assertEqual(self.walks(), 2)


class NewSessionDiscovery(unittest.TestCase):
    """`/act/new` answers from the instance that is about to be replaced, so the new
    session is read from the registry: the replacement registers itself under the same
    pid with a fresh id."""

    def setUp(self):
        self.saved = server.BRIDGES_FILE.read_text() if server.BRIDGES_FILE.exists() else None
        self.addCleanup(self._restore)

    def _restore(self):
        if self.saved is None:
            server.BRIDGES_FILE.unlink(missing_ok=True)
        else:
            server.BRIDGES_FILE.write_text(self.saved)

    def _registry(self, entries):
        server.BRIDGES_FILE.parent.mkdir(parents=True, exist_ok=True)
        server.BRIDGES_FILE.write_text(json.dumps(entries))

    def test_returns_the_session_that_registered_under_the_same_pid(self):
        self._registry({"old-session": {"pid": 4242, "updatedAt": "2026-01-01T00:00:00Z"},
                        "new-session": {"pid": 4242, "updatedAt": "2026-01-01T00:00:05Z"},
                        "someone-else": {"pid": 9999, "updatedAt": "2026-01-01T00:00:09Z"}})
        self.assertEqual(server.wait_for_new_session(4242, "old-session", timeout=0), "new-session")

    def test_gives_up_instead_of_pointing_at_the_old_session(self):
        self._registry({"old-session": {"pid": 4242, "updatedAt": "2026-01-01T00:00:00Z"}})
        self.assertEqual(server.wait_for_new_session(4242, "old-session", timeout=0.2), "")


class SessionResolutionById(unittest.TestCase):
    def test_a_running_session_counts_before_its_file_lands(self):
        """A session created from the picker has a bridge and no jsonl for a second or
        two; answering "gone" would bounce the reader back where they came from."""
        new_sid = "99999999-8888-7777-6666-555555555555"
        entry = {"port": 8123, "pid": os.getpid(), "sessionFile": "/tmp/not-written-yet.jsonl"}
        with mock.patch.object(server, "live_bridges", lambda: {new_sid: entry}):
            self.assertTrue(server.session_selected(new_sid))
            self.assertEqual(server.newest_session(new_sid), pathlib.Path("/tmp/not-written-yet.jsonl"))

    def test_id_is_found_even_when_the_file_name_says_otherwise(self):
        """A session written into an existing file keeps the old name; the header row is
        the only authority. Resolving by `id[-8:] in name` missed these, so the picker's
        link fell through to "gone" and the page showed the running session instead."""
        write_session("--resolve--", "2026-01-01T00-00-00-000Z_ffffffff-1111-2222-3333-444455556666.jsonl", [
            {"type": "session", "id": SID, "cwd": "/tmp/resolve"},
        ])
        self.addCleanup(clean, "--resolve--")
        self.assertIsNotNone(server.session_path_by_id(SID))
        self.assertIsNone(server.session_path_by_id("ffffffff-1111-2222-3333-444455556666"))


class PendingBlock(unittest.TestCase):
    def setUp(self):
        self.saved = server.PENDING_FILE.read_text() if server.PENDING_FILE.exists() else None
        server.PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
        server.PENDING_FILE.write_text(json.dumps({
            "id": "p1", "pid": os.getpid(), "command": "rm -rf build", "reason": "destructive",
            "at": int(time.time() * 1000), "timeoutMs": 60000,
        }))
        # the block only renders when a live bridge with this pid *and* port exists
        self.bridge = {"port": 8123, "pid": os.getpid()}
        self.patch = mock.patch.object(server, "live_bridges", lambda: {SID: self.bridge})
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        if self.saved is None:
            server.PENDING_FILE.unlink(missing_ok=True)
        else:
            server.PENDING_FILE.write_text(self.saved)

    def test_block_shows_command_and_both_buttons(self):
        html = server.pending_html(SID, 8123, "full")
        self.assertIn("rm -rf build", html)
        self.assertIn("等待确认", html)
        self.assertIn("允许", html)
        self.assertIn("拒绝", html)

    def test_english_block(self):
        html = server.pending_html(SID, 8123, "full", "en")
        self.assertIn("Waiting for approval", html)
        self.assertIn("Allow", html)

    def test_long_command_is_truncated(self):
        server.PENDING_FILE.write_text(json.dumps({
            "id": "p2", "pid": os.getpid(), "command": "x" * 900,
            "at": int(time.time() * 1000), "timeoutMs": 60000,
        }))
        self.assertIn("已截断", server.pending_html(SID, 8123, "full"))

    def test_readonly_never_shows_command_text(self):
        self.assertEqual(server.pending_html(SID, 8123, "readonly"), "")

    def test_block_of_another_pid_is_not_shown(self):
        self.assertEqual(server.pending_html(SID, 9999, "full"), "")

    def test_stale_block_is_dropped(self):
        server.PENDING_FILE.write_text(json.dumps({
            "id": "p3", "pid": os.getpid(), "command": "old",
            "at": int(time.time() * 1000) - 600000, "timeoutMs": 1000,
        }))
        self.assertEqual(server.pending_html(SID, 8123, "full"), "")
        self.assertFalse(server.PENDING_FILE.exists())


class SmallHelpers(unittest.TestCase):
    def test_human(self):
        self.assertEqual(server.human(999), "999")
        self.assertEqual(server.human(1500), "2k")
        self.assertEqual(server.human(2_500_000), "2.5M")
        self.assertEqual(server.human(None), "0")

    def test_short_path_keeps_two_segments(self):
        self.assertEqual(server.short_path("/a/b/c/d"), "c/d")
        self.assertEqual(server.short_path(str(pathlib.Path.home()) + "/x/y"), "\u2026/x/y")

    def test_as_int_and_as_float_tolerate_garbage(self):
        self.assertEqual(server.as_int("12"), 12)
        self.assertEqual(server.as_int("x"), 0)
        self.assertAlmostEqual(server.as_float("1.5"), 1.5)
        self.assertEqual(server.as_float(None), 0.0)

    def test_usage_totals_sums_only_assistant_rows(self):
        rows = [
            {"type": "message", "message": {"role": "assistant",
                                            "usage": {"input": 10, "output": 2, "cost": 0.5}}},
            {"type": "message", "message": {"role": "user", "usage": {"input": 99}}},
        ]
        total = server.usage_totals(rows)
        self.assertEqual(total["input"], 10)
        self.assertAlmostEqual(total["cost"], 0.5)

    def test_parse_cookies_and_qint(self):
        self.assertEqual(server.parse_cookies("a=1; b=2")["b"], "2")
        self.assertEqual(server.qint({"n": ["7"]}, "n", 12, 1, 200), 7)
        self.assertEqual(server.qint({"n": ["999"]}, "n", 12, 1, 200), 200)
        self.assertEqual(server.qint({}, "n", 12, 1, 200), 12)

    def test_mask_text_leaves_short_fragments_alone(self):
        # only lines long enough to be a real credential are masked, otherwise the
        # page would gut ordinary words that happen to sit in masked_secrets
        self.assertEqual(server.mask_text("x short y"), "x short y")

    def test_atomic_write_leaves_no_temp_files(self):
        target = EINK / "atomic-test.json"
        server.atomic_write(target, "{}")
        self.assertEqual(target.read_text(), "{}")
        self.assertEqual(sorted(p.name for p in EINK.glob("atomic-test*")), ["atomic-test.json"])
        target.unlink()

    def test_trust_hours_from_env(self):
        with mock.patch.dict(os.environ, {"EINK_TRUST_HOURS": "6"}):
            self.assertEqual(server.trust_hours(), 6.0)
        with mock.patch.dict(os.environ, {"EINK_TRUST_HOURS": ""}):
            self.assertEqual(server.trust_hours(), 0.0)

    def test_client_ip_prefers_the_socket_peer(self):
        handler = types.SimpleNamespace(client_address=("10.0.0.9", 1234))
        self.assertEqual(server.client_ip(handler), "10.0.0.9")

    def test_ipv6_loopback_and_cidr(self):
        self.assertTrue(server.ip_allowed("::1", "")[0])
        (EINK / "allow_ips").write_text("10.9.0.0/16\n")
        try:
            self.assertTrue(server.ip_allowed("10.9.1.2", "")[0])
            self.assertFalse(server.ip_allowed("10.10.1.2", "")[0])
        finally:
            (EINK / "allow_ips").write_text("127.0.0.1\n")

    def test_git_branch_reads_head_without_spawning_git(self):
        repo = EINK / "fake-repo"
        (repo / ".git" / "refs" / "heads").mkdir(parents=True)
        (repo / ".git" / "HEAD").write_text("ref: refs/heads/topic\n")
        (repo / ".git" / "refs" / "heads" / "topic").write_text("deadbeef\n")
        try:
            server.BRANCH_CACHE.clear()
            self.assertEqual(server.git_branch(str(repo)), "topic")
            server.BRANCH_CACHE.clear()
            self.assertEqual(server.git_branch("/nonexistent"), "")
        finally:
            shutil.rmtree(repo, ignore_errors=True)
            server.BRANCH_CACHE.clear()


class SessionResolution(unittest.TestCase):
    def setUp(self):
        self.path = write_session("--resolve--", "2026-03-03T00-00-00-000Z_%s.jsonl" % SID, [
            {"type": "session", "id": SID, "cwd": "/tmp/resolve"},
        ])
        server.VIEW_FILE.unlink(missing_ok=True)

    def tearDown(self):
        clean("--resolve--")
        server.VIEW_FILE.unlink(missing_ok=True)

    def test_session_path_by_id_matches_the_tail(self):
        self.assertEqual(server.session_path_by_id(SID), self.path)

    def test_unknown_id_is_not_selected(self):
        self.assertFalse(server.session_selected("ffffffff-0000-1111-2222-333333333333"))
        self.assertTrue(server.session_selected(SID))
        self.assertTrue(server.session_selected(""))

    def test_remember_and_read_back(self):
        self.assertEqual(server.remembered_sid(), "")
        server.remember_sid(SID)
        self.assertEqual(server.remembered_sid(), SID)

    def test_corrupt_view_file_is_ignored(self):
        server.VIEW_FILE.write_text("{not json")
        self.assertEqual(server.remembered_sid(), "")

    def test_resolve_prefers_the_remembered_session(self):
        server.remember_sid(SID)
        path, sid, gone = server.resolve_view("", "full")
        self.assertEqual(sid, SID)
        self.assertEqual(path, self.path)
        self.assertEqual(gone, "")

    def test_resolve_reports_a_stale_id(self):
        _path, sid, gone = server.resolve_view("ffffffff-0000-1111-2222-333333333333", "full")
        self.assertEqual(sid, "")
        self.assertEqual(gone, "ffffffff-0000-1111-2222-333333333333")

    def test_resolve_drops_a_choice_on_a_readonly_network(self):
        server.remember_sid(SID)
        _path, sid, gone = server.resolve_view("ffffffff-0000-1111-2222-333333333333", "readonly")
        self.assertEqual(sid, SID)      # the remembered one, never the client's pick
        self.assertEqual(gone, "")

    def test_rendered_session_id_follows_the_pin(self):
        server.remember_sid(SID)
        self.assertEqual(server.rendered_session_id(), SID)


class CapabilityEdges(unittest.TestCase):
    def setUp(self):
        self.saved = (EINK / "trust.json").read_text() if (EINK / "trust.json").exists() else None

    def tearDown(self):
        if self.saved is None:
            (EINK / "trust.json").unlink(missing_ok=True)
        else:
            (EINK / "trust.json").write_text(self.saved)

    def test_corrupt_trust_file_means_readonly(self):
        (EINK / "trust.json").write_text("not json")
        handler = base.fake_handler(cookie="pieink=" + "a" * 32, ip="192.168.3.25", local="192.168.3.45")
        self.assertEqual(server.caps_for(handler), "readonly")

    def test_confirmation_expires_after_the_configured_window(self):
        (EINK / "trust.json").write_text(json.dumps({
            "confirmed": ["192.168.3.0/24"], "peers": ["192.168.3.0/24"],
            "readonly": False,
            "stamp": {"subnet": "192.168.3.0/24", "at": 0.5},   # unlocked in 1970
        }))
        handler = base.fake_handler(cookie="pieink=" + "a" * 32, ip="192.168.3.25", local="192.168.3.45")
        with mock.patch.dict(os.environ, {"EINK_TRUST_HOURS": "1"}):
            self.assertEqual(server.caps_for(handler), "readonly")


class InfraHelpers(unittest.TestCase):
    """The small pieces the page depends on but never shows directly."""

    def test_ua_allow_list_falls_back_to_the_env(self):
        saved = (EINK / "uas").read_text()
        (EINK / "uas").write_text("# everything commented out\n")
        try:
            with mock.patch.dict(os.environ, {"EINK_ALLOW_UA": "Firefox, Chrome"}):
                self.assertEqual(server.ua_allow_list(), ["firefox", "chrome"])
        finally:
            (EINK / "uas").write_text(saved)

    def test_append_bounded_rotates_past_the_limit(self):
        log = EINK / "bounded.log"
        log.unlink(missing_ok=True)
        (log.parent / "bounded.log.1").unlink(missing_ok=True)
        try:
            server.append_bounded(log, "x" * 200, limit=100)
            server.append_bounded(log, "second", limit=100)
            self.assertTrue((log.parent / "bounded.log.1").exists())
            self.assertIn("second", log.read_text())
        finally:
            log.unlink(missing_ok=True)
            (log.parent / "bounded.log.1").unlink(missing_ok=True)

    def test_diag_seen_reads_the_log_once(self):
        saved = server.DIAG_LOG.read_text() if server.DIAG_LOG.exists() else None
        server.DIAG_LOG.write_text("12:00:00\tip=1.2.3.4\tev=x\nnot a beacon line\n")
        server._DIAG_SEEN = None
        try:
            seen = server._diag_seen()
            self.assertIn("ip=1.2.3.4\tev=x", seen)
        finally:
            server._DIAG_SEEN = None
            if saved is None:
                server.DIAG_LOG.unlink(missing_ok=True)
            else:
                server.DIAG_LOG.write_text(saved)

    def test_load_fails_ignores_garbage_and_expired_entries(self):
        saved = server.FAILS_FILE.read_text() if server.FAILS_FILE.exists() else None
        server.FAILS_FILE.write_text(json.dumps({
            "fails": {"1.1.1.1": [2, time.time()], "2.2.2.2": ["x"], "3.3.3.3": [9, 0]},
            "blocked": {"4.4.4.4": time.time() + 60, "5.5.5.5": "soon", "6.6.6.6": 0},
        }))
        try:
            server._FAILS.clear()
            server._BLOCKED.clear()
            server.load_fails()
            self.assertIn("1.1.1.1", server._FAILS)
            self.assertNotIn("2.2.2.2", server._FAILS)
            self.assertNotIn("3.3.3.3", server._FAILS)      # older than 10 minutes
            self.assertIn("4.4.4.4", server._BLOCKED)
            self.assertNotIn("5.5.5.5", server._BLOCKED)
            self.assertNotIn("6.6.6.6", server._BLOCKED)
        finally:
            server._FAILS.clear()
            server._BLOCKED.clear()
            if saved is None:
                server.FAILS_FILE.unlink(missing_ok=True)
            else:
                server.FAILS_FILE.write_text(saved)

    def test_load_fails_with_a_corrupt_file_is_a_noop(self):
        saved = server.FAILS_FILE.read_text() if server.FAILS_FILE.exists() else None
        server.FAILS_FILE.write_text("{not json")
        try:
            server._FAILS.clear()
            server.load_fails()
            self.assertEqual(server._FAILS, {})
        finally:
            if saved is None:
                server.FAILS_FILE.unlink(missing_ok=True)
            else:
                server.FAILS_FILE.write_text(saved)

    def test_pid_alive_variants(self):
        self.assertTrue(server.pid_alive(os.getpid()))
        self.assertFalse(server.pid_alive(0))
        self.assertFalse(server.pid_alive(-5))
        self.assertFalse(server.pid_alive("nope"))
        self.assertFalse(server.pid_alive(999999))

    def test_git_head_reads_this_checkout(self):
        self.assertRegex(server.git_head(), r"^[0-9a-f?]{1,7}$")

    def test_git_branch_detached_head(self):
        repo = EINK / "detached"
        (repo / ".git").mkdir(parents=True)
        (repo / ".git" / "HEAD").write_text("abc1234def\n")
        try:
            server.BRANCH_CACHE.clear()
            self.assertEqual(server.git_branch(str(repo)), "abc1234")
        finally:
            shutil.rmtree(repo, ignore_errors=True)
            server.BRANCH_CACHE.clear()

    def test_git_branch_missing_dot_git(self):
        server.BRANCH_CACHE.clear()
        self.assertEqual(server.git_branch(str(EINK)), "")
        server.BRANCH_CACHE.clear()

    def test_session_meta_without_a_session_line(self):
        p = server.SESSIONS / "--meta--" / "plain.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"type": "message", "message": {"role": "user", "content": "first line"}}\n')
        try:
            meta = server.session_meta(p)
            self.assertEqual(meta["first"], "first line")
            self.assertEqual(meta["id"], "")
        finally:
            clean("--meta--")

    def test_session_meta_of_a_missing_file(self):
        self.assertEqual(server.session_meta(pathlib.Path("/nope.jsonl")), {})

    def test_newest_session_uses_pi_session_file_as_a_last_resort(self):
        p = server.SESSIONS / "--env--" / "s.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"type": "session"}\n')
        try:
            with mock.patch.dict(os.environ, {"PI_SESSION_FILE": str(p)}), \
                 mock.patch.object(server, "live_bridges", lambda: {}), \
                 mock.patch.object(server, "bridge_state", lambda: None):
                self.assertEqual(server.newest_session(""), p)
        finally:
            clean("--env--")

    def test_newest_session_follows_bridge_json(self):
        p = server.SESSIONS / "--bj--" / "s.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"type": "session"}\n')
        try:
            with mock.patch.object(server, "live_bridges", lambda: {}), \
                 mock.patch.object(server, "bridge_state",
                                   lambda: {"sessionFile": str(p), "port": 1, "pid": os.getpid()}):
                self.assertEqual(server.newest_session(""), p)
        finally:
            clean("--bj--")

    def test_bridge_state_needs_a_live_pid_and_a_session(self):
        saved = server.STATE_FILE.read_text() if server.STATE_FILE.exists() else None
        try:
            server.STATE_FILE.write_text(json.dumps({"pid": 0, "port": 1}))
            self.assertIsNone(server.bridge_state())
            server.STATE_FILE.write_text(json.dumps({"pid": os.getpid(), "port": 1}))
            self.assertIsNone(server.bridge_state())     # no sessionFile
            server.STATE_FILE.write_text(json.dumps(
                {"pid": os.getpid(), "port": 1, "sessionFile": "/tmp/x.jsonl"}))
            self.assertEqual(server.bridge_state()["port"], 1)
        finally:
            if saved is None:
                server.STATE_FILE.unlink(missing_ok=True)
            else:
                server.STATE_FILE.write_text(saved)

    def test_bridge_alive_is_false_without_a_listener(self):
        with mock.patch.object(server, "bridge_port", lambda: 1):
            self.assertFalse(server.bridge_alive())

    def test_ip_allowed_rejects_rubbish_entries(self):
        (EINK / "allow_ips").write_text("not-an-ip\n10.0.0.0/99\n")
        try:
            self.assertFalse(server.ip_allowed("10.0.0.5", "")[0])
        finally:
            (EINK / "allow_ips").write_text("127.0.0.1\n")

    def test_md_multiline_quote_and_list_continuation(self):
        html = server.md("> line one\n> line two\n\n- item **bold**\ncontinued\n")
        self.assertIn("line one", html)
        self.assertIn("line two", html)
        self.assertIn("<b>bold</b>", html)
        self.assertIn("continued", html)


class BridgeRegistry(unittest.TestCase):
    """live_bridges() decides which sessions the page may act on. Every entry that
    cannot be proven alive must be dropped: acting on a reused pid would send the
    button to an unrelated process."""

    def setUp(self):
        self.saved = server.BRIDGES_FILE.read_text() if server.BRIDGES_FILE.exists() else None
        server._LIVE_CACHE[0] = 0
        self.addCleanup(setattr, server._LIVE_CACHE, "__getitem__", None) if False else None

    def tearDown(self):
        if self.saved is None:
            server.BRIDGES_FILE.unlink(missing_ok=True)
        else:
            server.BRIDGES_FILE.write_text(self.saved)
        server._LIVE_CACHE[0] = 0
        server._LIVE_CACHE[1] = {}

    def _write(self, entries):
        server.BRIDGES_FILE.write_text(json.dumps(entries))
        server._LIVE_CACHE[0] = 0

    def test_only_entries_with_a_live_pid_and_a_recent_heartbeat_survive(self):
        now = time.time()
        def iso(offset):
            return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now + offset)) + ".000Z"
        self._write({
            "ok": {"pid": os.getpid(), "port": 8123, "updatedAt": iso(0)},
            "not-a-dict": "x",
            "zero-pid": {"pid": 0, "port": 1, "updatedAt": iso(0)},
            "dead-pid": {"pid": 999999, "port": 1, "updatedAt": iso(0)},
            "bad-time": {"pid": os.getpid(), "port": 1, "updatedAt": "yesterday"},
            "stale": {"pid": os.getpid(), "port": 1, "updatedAt": iso(-3600)},
        })
        live = server.live_bridges()
        self.assertEqual(list(live), ["ok"])

    def test_missing_registry_is_empty(self):
        server.BRIDGES_FILE.unlink(missing_ok=True)
        server._LIVE_CACHE[0] = 0
        self.assertEqual(server.live_bridges(), {})

    def test_newest_session_follows_the_latest_heartbeat(self):
        old = write_session("--live1--", "old.jsonl", [{"type": "session", "id": "old"}])
        new = write_session("--live2--", "new.jsonl", [{"type": "session", "id": "new"}])
        live = {
            "old": {"sessionFile": str(old), "updatedAt": "2026-01-01T00:00:00.000Z"},
            "new": {"sessionFile": str(new), "updatedAt": "2026-01-02T00:00:00.000Z"},
        }
        try:
            with mock.patch.object(server, "live_bridges", lambda: live):
                # str(): /var and /private/var are the same file but not the same Path
                self.assertEqual(str(server.newest_session("")), str(new))
            live["new"]["sessionFile"] = "/nonexistent.jsonl"
            with mock.patch.object(server, "live_bridges", lambda: live), \
                 mock.patch.object(server, "bridge_state", lambda: None), \
                 mock.patch.dict(os.environ, {"PI_SESSION_FILE": ""}):
                # the heartbeat points at a file that no longer exists: never hand
                # back a path the page cannot read
                fallback = server.newest_session("")
                self.assertTrue(fallback is None or fallback.exists())
        finally:
            clean("--live1--")
            clean("--live2--")

    def test_pending_block_and_status_marker_reach_the_page(self):
        server.PENDING_FILE.write_text(json.dumps({
            "id": "p", "pid": os.getpid(), "command": "rm -rf x",
            "at": int(time.time() * 1000), "timeoutMs": 60000}))
        self.addCleanup(server.PENDING_FILE.unlink, missing_ok=True)
        p = write_session("--pend--", "s.jsonl", [{"type": "session", "id": SID}])
        live = {SID: {"pid": os.getpid(), "port": 8123}}
        try:
            with mock.patch.object(server, "live_bridges", lambda: live):
                _d, frag, status, _f, items, _b = server.render(p, 20, False, False, port=8123)
            self.assertIn("等待确认", frag)
            self.assertEqual(items[-1][0], "pending")
            self.assertIn("待确认", status)
        finally:
            clean("--pend--")

    def test_branch_and_cache_write_reach_the_status_line(self):
        repo = EINK / "branchrepo"
        (repo / ".git" / "refs" / "heads").mkdir(parents=True)
        (repo / ".git" / "HEAD").write_text("ref: refs/heads/feature\n")
        (repo / ".git" / "refs" / "heads" / "feature").write_text("abc\n")
        p = write_session("--branch--", "s.jsonl", [
            {"type": "session", "id": SID, "cwd": str(repo)},
            {"type": "message", "id": "m", "timestamp": "t",
             "message": {"role": "assistant",
                         "usage": {"input": 1, "output": 2, "cacheRead": 3, "cacheWrite": 4},
                         "content": [{"type": "text", "text": "ok"}]}},
        ])
        try:
            server.BRANCH_CACHE.clear()
            _d, _f, status, _ft, _i, _b = server.render(p, 20, False, False)
            self.assertIn("feature", status)
            # cacheWrite/cacheRead/cache-hit were dropped from the status line: it is
            # one 9px line that also carries the footer links, and those numbers never
            # changed a decision. See render().
            self.assertNotIn("W4", status)
        finally:
            clean("--branch--")
            shutil.rmtree(repo, ignore_errors=True)
            server.BRANCH_CACHE.clear()

    def test_row_cache_is_cleared_when_it_grows_too_large(self):
        server.ROW_HTML.clear()
        for i in range(20001):
            server.ROW_HTML[("k", i)] = False
        p = write_session("--rows--", "s.jsonl", [{"type": "session", "id": SID}])
        try:
            server.row_html(str(p), {"type": "message", "id": "x",
                                     "message": {"role": "user", "content": "hi"}}, 0, False, False)
            self.assertLess(len(server.ROW_HTML), 20001)
        finally:
            clean("--rows--")
            server.ROW_HTML.clear()

    def test_log_action_rotates_past_a_megabyte(self):
        log = server.ACTION_LOG
        saved = log.read_text() if log.exists() else None
        rotated = pathlib.Path(str(log) + ".1")
        rotated.unlink(missing_ok=True)
        log.write_text("x" * (1_000_001))
        try:
            server.log_action("1.2.3.4", "full", "say", 200, "abcdef12")
            self.assertTrue(rotated.exists())
            self.assertIn("action=say", log.read_text())
        finally:
            log.unlink(missing_ok=True)
            rotated.unlink(missing_ok=True)
            if saved is not None:
                log.write_text(saved)

    def test_confirm_network_stamps_the_subnet(self):
        saved = server.TRUST_FILE.read_text() if server.TRUST_FILE.exists() else None
        server.TRUST_FILE.write_text(json.dumps({"confirmed": [], "peers": [], "readonly": True}))
        handler = base.fake_handler(cookie="pieink=" + "a" * 32, ip="192.168.3.25", local="192.168.3.45")
        try:
            with mock.patch.object(server, "host_subnet", lambda _h: "192.168.3.0/24"), \
                 mock.patch.object(server, "peer_subnet", lambda _h: "192.168.3.0/24"):
                server.confirm_network(handler)
            state = json.loads(server.TRUST_FILE.read_text())
            self.assertIn("192.168.3.0/24", state["confirmed"])
            self.assertFalse(state["readonly"])
            self.assertEqual(state["stamp"]["subnet"], "192.168.3.0/24")
            self.assertGreater(state["stamp"]["at"], 0)
        finally:
            if saved is None:
                server.TRUST_FILE.unlink(missing_ok=True)
            else:
                server.TRUST_FILE.write_text(saved)

    def test_git_branch_follows_a_worktree_gitdir_file(self):
        real = EINK / "realgit" / ".git"
        (real / "refs" / "heads").mkdir(parents=True)
        (real / "HEAD").write_text("ref: refs/heads/wt\n")
        (real / "refs" / "heads" / "wt").write_text("def\n")
        work = EINK / "worktree"
        work.mkdir()
        (work / ".git").write_text("gitdir: %s\n" % real)
        try:
            server.BRANCH_CACHE.clear()
            self.assertEqual(server.git_branch(str(work)), "wt")
        finally:
            shutil.rmtree(EINK / "realgit", ignore_errors=True)
            shutil.rmtree(work, ignore_errors=True)
            server.BRANCH_CACHE.clear()

    def test_short_path_of_home_itself(self):
        self.assertTrue(server.short_path(str(pathlib.Path.home())))


if __name__ == "__main__":
    unittest.main(verbosity=2)
