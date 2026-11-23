import os
import re
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "src", "py"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fastenv  # noqa: E402,F401  (shorter delays in the servers tests start)
from sfh import tui  # noqa: E402
from sfh.tui import Editor, KeyParser, editor_view, render_transcript, str_width, visible_width, wrap  # noqa: E402

SGR = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def plain(lines):
    return [SGR.sub("", l) for l in lines]


def keys(ed, *ks):
    acts = []
    for k in ks:
        acts.append(ed.handle(k))
    return acts


def typ(ed, s):
    for c in s:
        ed.handle(c)


class KeyParserTests(unittest.TestCase):
    def test_basic(self):
        p = KeyParser()
        self.assertEqual(p.feed(b"a\r\n\x01\x05\x7f\x0b"), ["a", "enter", "C-j", "C-a", "C-e", "backspace", "C-k"])

    def test_sequences(self):
        p = KeyParser()
        self.assertEqual(p.feed(b"\x1b[A\x1b[B\x1b[C\x1b[D\x1b[H\x1b[F\x1b[3~\x1b[5~\x1b[6~\x1bOA\x1b[1;5C"),
                         ["up", "down", "right", "left", "home", "end", "del", "pgup", "pgdn", "up", "right"])

    def test_split_sequence(self):
        p = KeyParser()
        self.assertEqual(p.feed(b"\x1b"), [])
        self.assertTrue(p.pending_escape())
        self.assertEqual(p.feed(b"["), [])
        self.assertEqual(p.feed(b"5"), [])
        self.assertEqual(p.feed(b"~x"), ["pgup", "x"])
        self.assertFalse(p.pending_escape())

    def test_lone_escape(self):
        p = KeyParser()
        self.assertEqual(p.feed(b"\x1b"), [])
        self.assertEqual(p.flush(), ["esc"])

    def test_alt(self):
        self.assertEqual(KeyParser().feed(b"\x1bx"), ["M-x"])

    def test_utf8_split(self):
        p = KeyParser()
        b = "é漢".encode()
        self.assertEqual(p.feed(b[:1]), [])
        self.assertEqual(p.feed(b[1:]), ["é", "漢"])

    def test_bracketed_paste(self):
        p = KeyParser()
        self.assertEqual(p.feed(b"\x1b[200~line1\rline2\n"), [])
        self.assertEqual(p.feed(b"x\x1b[20"), [])
        self.assertEqual(p.feed(b"1~z"), [("paste", "line1\rline2\nx"), "z"])

    def test_sgr_mouse(self):
        p = KeyParser()
        self.assertEqual(p.feed(b"a\x1b[<64;10;5Mb\x1b[<65;1;2M\x1b[<68;3;4M\x1b[<0;7;8m"),
                         ["a", ("mouse", 64, 10, 5, True), "b", ("mouse", 65, 1, 2, True),
                          ("mouse", 68, 3, 4, True), ("mouse", 0, 7, 8, False)])
        self.assertEqual(p.feed(b"\x1b[<1;2X\x1b[<1;2;3;4Mz"), ["z"])  # malformed: swallowed, no text

    def test_mouse_split_across_reads(self):
        seq = b"\x1b[<64;120;33M"
        for cut in range(2, len(seq)):  # (a lone ESC followed by silence is the Esc key)
            p = KeyParser()
            got = p.feed(b"x" + seq[:cut])
            got += p.flush()  # a pause in the middle (slow link) must not turn it into Esc + text
            got += p.feed(seq[cut:] + b"y")
            self.assertEqual(got, ["x", ("mouse", 64, 120, 33, True), "y"], cut)
            self.assertFalse(p.pending_escape())

    def test_mouse_fragment_dropped_quietly(self):
        p = KeyParser()
        self.assertEqual(p.feed(b"\x1b[<64;1"), [])
        out = []
        for _ in range(KeyParser.MOUSE_PATIENCE):
            out += p.flush()
        self.assertEqual(out, [])  # given up on, without leaking "[<64;1"
        self.assertFalse(p.pending_escape())
        self.assertEqual(p.feed(b"\x1b[<64;1\x1b[Aq"), ["up", "q"])  # cut off by the next sequence

    def test_x10_mouse(self):
        p = KeyParser()
        self.assertEqual(p.feed(b"\x1b[M"), [])
        self.assertEqual(p.feed(b"`!"), [])
        self.assertEqual(p.feed(b"\"k"), [("mouse", 64, 1, 2, True), "k"])
        self.assertEqual(p.feed(b"\x1b[Ma!!\x1b[M#!!"), [("mouse", 65, 1, 1, True), ("mouse", 3, 1, 1, False)])


class EditorTests(unittest.TestCase):
    def test_insert_submit(self):
        ed = Editor()
        typ(ed, "hi")
        self.assertEqual(ed.handle("enter"), ("submit", "hi", None))
        self.assertEqual(ed.text, "")
        self.assertIsNone(ed.handle("enter"))

    def test_ctrl_a_e_and_newline(self):
        ed = Editor()
        typ(ed, "abc")
        ed.handle("C-a")
        typ(ed, "X")
        ed.handle("C-e")
        typ(ed, "Y")
        ed.handle("C-j")
        typ(ed, "def")
        self.assertEqual(ed.text, "XabcY\ndef")
        self.assertEqual((ed.row, ed.col), (1, 3))
        ed.handle("C-a")
        self.assertEqual(ed.col, 0)

    def test_left_right_across_lines(self):
        ed = Editor()
        ed.set_text("ab\ncd")
        ed.handle("C-a")
        ed.handle("left")
        self.assertEqual((ed.row, ed.col), (0, 2))
        ed.handle("right")
        self.assertEqual((ed.row, ed.col), (1, 0))

    def test_up_down_keep_column(self):
        ed = Editor()
        ed.set_text("abcdef\nx\nabcdef")
        ed.row, ed.col = 2, 5
        ed.handle("up")
        self.assertEqual((ed.row, ed.col), (1, 1))
        ed.handle("up")
        self.assertEqual((ed.row, ed.col), (0, 5))
        ed.handle("down")
        ed.handle("down")
        self.assertEqual((ed.row, ed.col), (2, 5))

    def test_history(self):
        ed = Editor()
        ed.history = ["first", "second\nmulti"]
        typ(ed, "draft")
        ed.handle("up")
        self.assertEqual(ed.text, "second\nmulti")
        self.assertEqual(ed.row, 0)  # still on the first line, so Up goes further back
        ed.handle("up")
        self.assertEqual(ed.text, "first")
        ed.handle("up")
        self.assertEqual(ed.text, "first")
        ed.handle("down")
        self.assertEqual(ed.text, "second\nmulti")
        self.assertEqual(ed.row, 1)
        ed.handle("down")
        self.assertEqual(ed.text, "draft")
        ed.handle("down")
        self.assertEqual(ed.text, "draft")

    def test_up_inside_multiline_moves_not_history(self):
        ed = Editor()
        ed.history = ["old"]
        ed.set_text("a\nb")
        ed.handle("up")
        self.assertEqual((ed.text, ed.row), ("a\nb", 0))
        ed.handle("up")
        self.assertEqual(ed.text, "old")

    def test_kills(self):
        ed = Editor()
        ed.set_text("hello big world")
        ed.col = 9
        ed.handle("C-k")
        self.assertEqual(ed.text, "hello big")
        ed.handle("C-w")
        self.assertEqual(ed.text, "hello ")
        ed.handle("C-u")
        self.assertEqual((ed.text, ed.col), ("", 0))

    def test_backspace_delete_join(self):
        ed = Editor()
        ed.set_text("ab\ncd")
        ed.row, ed.col = 1, 0
        ed.handle("backspace")
        self.assertEqual((ed.text, ed.col), ("abcd", 2))
        ed.handle("del")
        self.assertEqual(ed.text, "abd")
        ed.handle("C-d")
        self.assertEqual(ed.text, "ab")

    def test_ctrl_c_ctrl_d_exit(self):
        ed = Editor()
        typ(ed, "x")
        self.assertIsNone(ed.handle("C-c"))
        self.assertEqual(ed.text, "")
        self.assertEqual(ed.handle("C-c"), ("exit",))
        self.assertEqual(ed.handle("C-d"), ("exit",))

    def test_paste_does_not_submit(self):
        ed = Editor()
        ed.handle(("paste", "a\r\nb"))
        self.assertEqual(ed.text, "a\nb")

    def test_esc_scroll(self):
        ed = Editor()
        self.assertEqual(keys(ed, "esc", "pgup", "pgdn"), [("stop",), ("scroll", 1), ("scroll", -1)])

    def test_edit_mode(self):
        ed = Editor()
        ed.load_edit("orig", 42)
        typ(ed, "!")
        self.assertEqual(ed.handle("enter"), ("submit", "orig!", 42))
        self.assertIsNone(ed.edit_id)


class WrapTests(unittest.TestCase):
    def test_words(self):
        self.assertEqual(wrap("the quick brown fox", 10), ["the quick", "brown fox"])

    def test_long_word(self):
        self.assertEqual(wrap("abcdefghij", 4), ["abcd", "efgh", "ij"])

    def test_newlines(self):
        self.assertEqual(wrap("a\n\nb", 5), ["a", "", "b"])

    def test_wide(self):
        self.assertEqual(str_width("漢字"), 4)
        for l in wrap("漢字漢字漢字", 5):
            self.assertLessEqual(str_width(l), 5)
        self.assertEqual(wrap("漢字漢字漢字", 5), ["漢字", "漢字", "漢字"])

    def test_editor_view(self):
        ed = Editor()
        ed.set_text("abcdefgh\nxy")
        rows, cr, cc = editor_view(ed, 6, 10)
        self.assertEqual(rows, ["❯ abcd", "  efgh", "  xy"])
        self.assertEqual((cr, cc), (2, 4))
        rows, cr, cc = editor_view(ed, 6, 1)
        self.assertEqual((rows, cr), (["  xy"], 0))


class RenderTests(unittest.TestCase):
    def test_thinking_tool_text(self):
        items = [
            ("msg", {"id": 1, "role": "user", "content": [{"type": "text", "text": "do it"}]}),
            ("msg", {"id": 2, "role": "assistant", "content": [
                {"type": "thinking", "thinking": "pondering deeply"},
                {"type": "tool_use", "id": "t1", "name": "bash", "input": {"command": "echo hi"}}]}),
            ("msg", {"id": 3, "role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "hi\n2\n3\n4\n5\n6"}]}),
            ("msg", {"id": 4, "role": "assistant", "content": [{"type": "text", "text": "**Mock reply** `x`"}]}),
        ]
        lines = render_transcript(items, None, 40)
        p = plain(lines)
        self.assertIn("❯ do it", p)
        think = [l for l in lines if "pondering" in l][0]
        self.assertIn("\x1b[2m\x1b[3m", think)  # dim + italic
        self.assertIn("⏺ bash(echo hi)", p)
        self.assertIn("  ⎿ hi", p)
        self.assertIn("    … +2 lines", p)
        self.assertIn("  Mock reply x", p)
        self.assertTrue(any("\x1b[1mMock reply" in l for l in lines))
        for l in lines:
            self.assertLessEqual(visible_width(l), 40)

    def test_draft_and_boxes(self):
        items = [("notice", {"level": "info", "text": "hello notice"}),
                 ("btw", {"btw_id": "b", "question": "why?", "text": "because", "done": True})]
        draft = {"content": [{"type": "thinking", "thinking": "hmm"},
                             {"type": "tool_use", "id": "t", "name": "bash", "partial_json": '{"command": "ls'}]}
        p = plain(render_transcript(items, draft, 50))
        self.assertTrue(any("not in context" in l and "notice" in l for l in p))
        self.assertTrue(any("/btw why?" in l for l in p))
        self.assertIn("│   because", p)
        self.assertIn("┊ hmm", p)
        self.assertTrue(any(l.startswith("⏺ bash(") for l in p))

    def test_artifact_placeholder(self):
        text = ('before\n\n```artifact title="Sorting demo"\n<!doctype html>\n<script>go()</script>\n```\n\nafter\n\n'
                '~~~~artifact\n```\nx\n```\n~~~~\n```html\n<b>shown</b>\n```')
        p = plain(tui.render_markdown(text, 60))
        self.assertIn("  [artifact: Sorting demo — open in web UI]", p)
        self.assertIn("  [artifact: Artifact — open in web UI]", p)
        self.assertFalse(any("doctype" in l or "go()" in l or l.strip() == "│ x" for l in p))
        self.assertIn("  after", p)
        self.assertTrue(any("<b>shown</b>" in l for l in p))
        self.assertEqual(tui.artifact_title("title='Q'"), "Q")
        self.assertEqual(tui.artifact_title("My chart"), "My chart")
        # streaming: an unclosed artifact fence still shows only the placeholder
        p = plain(tui.render_markdown("```artifact\n<html>\n<body>", 60))
        self.assertEqual([l for l in p if l.strip()], ["  [artifact: Artifact — open in web UI]"])


class DurationEffortRenderTests(unittest.TestCase):
    def test_fmt(self):
        self.assertEqual([tui.fmt_dur(x) for x in (3400, 42000, 125000, 3780000)], ["3.4s", "42s", "2m 05s", "1h 03m"])
        self.assertEqual([tui.live_dur(1000, 1000 + x) for x in (4999, 12000, 66000)], ["0s", "10s", "1m 05s"])

    def items(self):
        return [
            ("msg", {"id": 1, "role": "user", "created_at": 1000, "content": [{"type": "text", "text": "go"}]}),
            ("msg", {"id": 2, "role": "assistant", "model": "m1", "provider": "p", "started_at": 1000, "created_at": 15000,
                     "content": [
                         {"type": "thinking", "thinking": "hmm", "started_at": 2000, "ended_at": 14000},
                         {"type": "tool_use", "id": "t1", "name": "bash", "input": {"command": "ls"}}]}),
            ("msg", {"id": 3, "role": "user", "created_at": 18400, "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "x", "started_at": 15000, "ended_at": 18400}]}),
            ("msg", {"id": 4, "role": "assistant", "model": "m1", "provider": "p", "effort": "high", "started_at": 18400,
                     "created_at": 30000, "content": [
                         {"type": "text", "text": "done", "started_at": 25000, "ended_at": 29200}]}),
        ]

    def test_durations_and_changes(self):
        p = plain(render_transcript(self.items(), None, 60))
        self.assertIn("┊ thought for 12s", p)
        self.assertIn("⏺ bash(ls) · 3.4s", p)
        self.assertIn("  ⏱ text 4.2s · response 11s", p)
        self.assertIn("  ⇄ effort high", p)
        self.assertEqual(p[-1], "  ── turn took 29s")
        # while the turn runs: no turn total yet; a running tool and the streaming thinking tick in 5s steps
        items = self.items()[:2]
        live = {"now": 27000, "tools": {"t1": {"started_at": 15000}}}
        draft = {"content": [{"type": "thinking", "thinking": "more", "started_at": 20000}]}
        p = plain(render_transcript(items, draft, 60, {}, live, running=True))
        self.assertIn("⏺ bash(ls) · 10s…", p)
        self.assertIn("┊ thinking… 5s", p)
        self.assertFalse(any("turn took" in l for l in p))
        # stored without timing (older messages): nothing shown
        p = plain(render_transcript([("msg", {"id": 1, "role": "assistant", "content": [{"type": "text", "text": "a"}]})],
                                    None, 60))
        self.assertFalse(any("⏱" in l for l in p))

    def test_effort_header_and_tab(self):
        app = tui.App("/x.html", {"cwd": "/"})
        app.session = {"id": "s1", "status": "idle", "provider": "p", "model": "m", "effort": "high",
                       "effort_wanted": "xhigh", "effort_override": "xhigh", "efforts": ["low", "high"]}
        app.rows, app.cols = 20, 100
        rows, _ = app.frame()
        self.assertIn("effort xhigh→high", rows[0])
        calls = []

        class R:
            def call(self, method, params):
                calls.append((method, params))
                return {"effort_override": params["effort"], "effort_wanted": params["effort"],
                        "effort": params["effort"]}
        app.rpc = R()
        app.handle_key("tab")
        self.assertEqual(calls[-1], ("sessions.set_model", {"session_id": "s1", "effort": None}))
        app.handle_key("tab")
        self.assertEqual(calls[-1][1]["effort"], "low")
        app.handle_key("S-tab")
        self.assertEqual(calls[-1][1]["effort"], None)
        self.assertEqual(app.editor.text, "")
        self.assertEqual(tui.effort_label({"effort_wanted": None}), "default")


class SubagentRenderTests(unittest.TestCase):
    def test_tool_use_and_result_box(self):
        res = ('<subagent-result session_id="s_child1" description="find &quot;bugs&quot;" status="completed">'
               'line one\n' + "\n".join("more %d" % i for i in range(10)) + '</subagent-result>')
        items = [
            ("msg", {"id": 1, "role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "subagent",
                 "input": {"description": "find bugs", "prompt": "look around"}},
                {"type": "tool_use", "id": "t2", "name": "webfetch", "input": {"url": "https://example.com/a"}}]}),
            ("msg", {"id": 2, "role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": '{"session_id": "s_child1", "status": "started"}'},
                {"type": "tool_result", "tool_use_id": "t2", "content": "page"}]}),
            ("msg", {"id": 3, "role": "user", "content": [{"type": "text", "text": res}]}),
        ]
        lines = render_transcript(items, None, 60)
        p = plain(lines)
        self.assertIn("⏺ subagent(find bugs) → s_child1", p)
        self.assertIn("⏺ webfetch(https://example.com/a)", p)
        self.assertTrue(any(l.startswith('╭─ ⤷ subagent find "bugs" [completed]') for l in p), p)
        self.assertIn("│   line one", p)
        self.assertTrue(any("… +" in l for l in p))
        self.assertFalse(any("<subagent-result" in l or l.startswith("❯") for l in p))
        for l in lines:
            self.assertLessEqual(visible_width(l), 60)

    def test_error_status_and_not_history(self):
        r = tui.parse_subagent_result('<subagent-result session_id="s_x" description="d" status="error">boom</subagent-result>')
        self.assertEqual((r["session_id"], r["status"], r["body"]), ("s_x", "error", "boom"))
        self.assertIsNone(tui.parse_subagent_result("hello <subagent-result>"))
        app = tui.App("/x.html", {"cwd": "/"})
        app.session = {"id": "s1", "status": "idle"}
        app.on_event({"event": "message.added", "data": {"id": 1, "session_id": "s1", "role": "user", "content": [
            {"type": "text", "text": '<subagent-result session_id="s_x" description="d" status="error">boom</subagent-result>'}]}})
        self.assertEqual(app.editor.history, [])

    def test_nested_sessions(self):
        lst = [{"id": "s_kid", "parent_id": "s_p", "title": "kid task", "message_count": 2},
               {"id": "s_p", "title": "parent", "children": 1, "message_count": 3},
               {"id": "s_orphan", "parent_id": "s_gone", "title": "orphan"},
               {"id": "s_grand", "parent_id": "s_kid", "title": "grandkid"}]
        self.assertEqual([(s["id"], d) for s, d in tui.nest_sessions(lst)],
                         [("s_p", 0), ("s_kid", 1), ("s_grand", 2), ("s_orphan", 0)])
        out = tui.format_sessions(lst)
        self.assertIn("  parent  (", out[0])
        self.assertIn("↳ kid task", out[1])
        self.assertIn("  ↳ grandkid", out[2])
        p = tui.SessionPicker(lst, now_ms=0)
        rows = plain(p.render(80, 6))
        self.assertTrue(rows[1].startswith("❯ parent") and "1 subagent" in rows[1], rows)
        self.assertTrue(rows[2].startswith("  ↳ kid task"), rows)
        self.assertTrue(rows[3].startswith("    ↳ grandkid"), rows)
        p.handle("down")
        self.assertEqual(p.handle("enter"), ("resume", "s_kid"))


class AppEventTests(unittest.TestCase):
    def test_events_drive_state(self):
        app = tui.App("/x.html", {"cwd": "/"})
        app.session = {"id": "s1", "status": "idle"}
        ev = lambda n, d: app.on_event({"event": n, "data": d})  # noqa: E731
        ev("message.added", {"id": 1, "session_id": "s1", "role": "user", "content": [{"type": "text", "text": "q"}]})
        ev("draft.start", {"session_id": "s1", "turn_id": "t"})
        ev("draft.block", {"session_id": "s1", "turn_id": "t", "index": 0, "block": {"type": "text", "text": ""}})
        ev("draft.delta", {"session_id": "s1", "turn_id": "t", "index": 0, "text": "par"})
        ev("draft.delta", {"session_id": "s1", "turn_id": "t", "index": 0, "text": "tial"})
        ev("message.added", {"id": 9, "session_id": "other", "role": "user", "content": [{"type": "text", "text": "z"}]})
        self.assertEqual(app.draft["content"][0]["text"], "partial")
        self.assertEqual(app.editor.history, ["q"])
        app.rows, app.cols = 20, 60
        rows, _ = app.frame()
        self.assertTrue(any("partial" in r for r in rows))
        ev("messages.truncated", {"session_id": "s1", "from_id": 1})
        self.assertEqual(app.items, [])
        self.assertEqual(app.editor.history, [])


class MouseTests(unittest.TestCase):
    def app(self, n=60):
        app = tui.App("/x.html", {"cwd": "/"})
        app.session = {"id": "s1", "status": "idle", "title": "t"}
        app.rows, app.cols = 20, 60
        app.items = [("local", "line %d" % i) for i in range(n)]
        app.frame()
        return app

    def view(self, app):
        return plain(app.frame()[0])

    def test_wheel_scrolls_and_indicator(self):
        app = self.app()
        self.assertIn("line 59", "\n".join(self.view(app)))
        app.handle_key(("mouse", 64, 5, 5, True))  # wheel up: back in history
        self.assertEqual(app.scroll, tui.WHEEL_LINES)
        rows = self.view(app)
        self.assertNotIn("line 59", "\n".join(rows))
        self.assertIn("↓ %d more lines" % tui.WHEEL_LINES, "\n".join(rows))
        app.handle_key(("mouse", 64 | 4, 5, 5, True))  # shift+wheel: a page
        self.assertEqual(app.scroll, tui.WHEEL_LINES + app.tlines_h - 2)
        app.handle_key(("mouse", 65, 5, 5, True))
        self.assertEqual(app.scroll, app.tlines_h - 2)
        for _ in range(10):
            app.handle_key(("mouse", 65, 5, 5, True))
        self.assertEqual(app.scroll, 0)
        self.assertNotIn("more lines", "\n".join(self.view(app)))
        for _ in range(100):
            app.handle_key(("mouse", 64, 5, 5, True))
        self.view(app)
        self.assertIn("line 0", "\n".join(self.view(app)))  # clamped at the top

    def test_clicks_ignored(self):
        app = self.app()
        for k in [("mouse", 0, 5, 5, True), ("mouse", 0, 5, 5, False), ("mouse", 2, 1, 1, True),
                  ("mouse", 66, 1, 1, True), ("mouse", 64 | 32, 1, 1, True)]:
            app.handle_key(k)
        self.assertEqual(app.scroll, 0)
        self.assertEqual(app.editor.text, "")

    def test_scrolled_view_holds_while_output_arrives(self):
        app = self.app()
        app.handle_key(("mouse", 64, 5, 5, True))
        before = self.view(app)[1:-4]
        for i in range(5):
            app.items.append(("local", "new %d" % i))
        after = self.view(app)
        self.assertEqual(after[1:-4], before)
        self.assertIn("↓ %d more lines" % (tui.WHEEL_LINES + 5), "\n".join(after))
        app.scroll = 0  # at the bottom: follows output
        app.items.append(("local", "newest"))
        self.assertIn("newest", "\n".join(self.view(app)))

    def test_wheel_in_picker_moves_selection(self):
        app = self.app()
        app.picker = tui.SessionPicker(PickerTests().sessions())
        app.handle_key(("mouse", 65, 1, 1, True))
        self.assertEqual(app.picker.sel, 1)
        app.handle_key(("mouse", 64, 1, 1, True))
        self.assertEqual(app.picker.sel, 0)
        self.assertEqual(app.scroll, 0)

    def test_setting_and_toggle(self):
        app = tui.App("/x.html", {"cwd": "/"})
        self.assertTrue(app.mouse)
        written = []
        app.out_fd = 99
        orig = tui.os.write
        tui.os.write = lambda fd, b: written.append((fd, b)) or len(b)
        try:
            app.submit("/mouse", None)  # not in the terminal yet: nothing written
            self.assertFalse(app.mouse)
            self.assertEqual(written, [])
            self.assertIn("mouse off", app.flash)
            app.term_active = True
            app.submit("/mouse on", None)
            self.assertTrue(app.mouse)
            self.assertEqual(written, [(99, tui.MOUSE_ON)])
            app.on_event({"event": "settings.updated", "data": {"tui_mouse": False}})
            self.assertFalse(app.mouse)
            self.assertEqual(written[-1], (99, tui.MOUSE_OFF))
            app.on_event({"event": "settings.updated", "data": {"tui_mouse": False, "model": "x"}})
            self.assertEqual(len(written), 2)  # unchanged: not re-sent
        finally:
            tui.os.write = orig

        class FakeRPC:
            def call(self, method, params=None):
                return {"settings": {"tui_mouse": False}}
        orig_ensure, orig_connect = tui.ensure_server, tui.wsclient.RPC.connect
        tui.ensure_server = lambda path: 1
        tui.wsclient.RPC.connect = staticmethod(lambda *a, **k: FakeRPC())
        orig_tok = tui.htmlfile.read_token
        tui.htmlfile.read_token = lambda p: ""
        try:
            app = tui.App("/x.html", {"cwd": "/"})
            app.connect()
            self.assertFalse(app.mouse)
        finally:
            tui.ensure_server, tui.wsclient.RPC.connect, tui.htmlfile.read_token = orig_ensure, orig_connect, orig_tok
        from sfh import store
        self.assertIs(store.SETTINGS_DEFAULTS["tui_mouse"], True)

    def test_terminal_modes_in_step(self):
        """Mouse reporting is enabled with the alternate screen and always disabled on leaving,
        also around a suspend (Ctrl-Z)."""
        import termios
        app = tui.App("/x.html", {"cwd": "/"})
        app.in_fd, app.out_fd = 0, 1
        app.saved_tty = "saved"
        written, tc, kills = [], [], []
        patches = [(tui.os, "write", lambda fd, b: written.append(b) or len(b)),
                   (termios, "tcgetattr", lambda fd: [0, 0, 0, 0, 0, 0, [0] * 32]),
                   (termios, "tcsetattr", lambda fd, when, a: tc.append(a)),
                   (tui.os, "killpg", lambda pg, sig: kills.append(sig)),
                   (tui.os, "get_terminal_size", lambda fd: os.terminal_size((80, 24)))]
        saved = [(o, n, getattr(o, n)) for o, n, _ in patches]
        for o, n, f in patches:
            setattr(o, n, f)
        try:
            app.enter_terminal()
            self.assertIn(tui.MOUSE_ON, written[-1])
            self.assertIn(b"\x1b[?1049h", written[-1])
            app.handle_key("C-z")
            self.assertEqual(kills, [tui.signal.SIGTSTP])
            self.assertIn(tui.MOUSE_OFF, written[-2])
            self.assertIn(b"\x1b[?1049l", written[-2])
            self.assertEqual(tc[-2], "saved")
            self.assertIn(tui.MOUSE_ON, written[-1])  # resumed
            self.assertTrue(app.term_active)
            app.leave_terminal("saved")
            self.assertIn(tui.MOUSE_OFF, written[-1])
            app.mouse = False
            written.clear()
            app.enter_terminal()
            self.assertNotIn(tui.MOUSE_ON, written[-1])
            app.leave_terminal("saved")
            self.assertIn(tui.MOUSE_OFF, written[-1])  # always, whatever the state
        finally:
            for o, n, f in saved:
                setattr(o, n, f)


class PickerTests(unittest.TestCase):
    NOW = 10_000_000

    def sessions(self):
        return [{"id": "s_a", "title": "fix login bug", "cwd": "/src/app", "updated_at": self.NOW - 30_000,
                 "message_count": 4},
                {"id": "s_b", "title": "", "cwd": "/tmp", "updated_at": self.NOW - 7_200_000, "message_count": 0},
                {"id": "s_c", "title": "write docs", "cwd": "/src/app", "updated_at": self.NOW - 3 * 86_400_000,
                 "message_count": 12}]

    def test_navigation_and_enter(self):
        p = tui.SessionPicker(self.sessions(), now_ms=self.NOW)
        self.assertIsNone(p.handle("down"))
        self.assertIsNone(p.handle("C-n"))
        self.assertIsNone(p.handle("down"))  # clamped at the end
        self.assertEqual(p.sel, 2)
        p.handle("up")
        p.handle("C-p")
        self.assertEqual(p.handle("enter"), ("resume", "s_a"))
        self.assertEqual(p.handle("esc"), ("cancel",))

    def test_filter(self):
        p = tui.SessionPicker(self.sessions(), now_ms=self.NOW)
        p.handle("down")
        for c in "src docs":
            p.handle(c)
        self.assertEqual([s["id"] for s in p.matches], ["s_c"])
        self.assertEqual(p.sel, 0)
        self.assertEqual(p.handle("enter"), ("resume", "s_c"))
        for _ in range(8):
            p.handle("backspace")
        self.assertEqual(len(p.matches), 3)
        p.handle(("paste", "zzz"))
        self.assertEqual(p.matches, [])
        self.assertIsNone(p.handle("enter"))

    def test_render(self):
        p = tui.SessionPicker(self.sessions(), now_ms=self.NOW)
        rows = p.render(80, 3)
        self.assertEqual(len(rows), 3)
        txt = plain(rows)
        self.assertIn("resume:", txt[0])
        self.assertIn("fix login bug", txt[1])
        self.assertIn("s_a", txt[1])
        self.assertIn("4 msgs", txt[1])
        self.assertIn("/src/app", txt[1])
        self.assertIn("(untitled)", txt[2])
        self.assertIn("2h ago", txt[2])
        p.handle("down")
        p.handle("down")  # scrolls so the selection stays visible
        self.assertIn("write docs", plain(p.render(80, 3))[2])
        self.assertIn("3d ago", plain(p.render(80, 3))[2])
        self.assertTrue(all(tui.visible_width(r) <= 80 for r in p.render(80, 10)))

    def test_relative_time(self):
        self.assertEqual(tui.relative_time(self.NOW - 5000, self.NOW), "just now")
        self.assertEqual(tui.relative_time(self.NOW - 120_000, self.NOW), "2m ago")
        self.assertEqual(tui.relative_time(None, self.NOW), "")

    def test_app_picker_flow(self):
        app = tui.App("/x.html", {"cwd": "/"})
        app.session = {"id": "s1", "status": "idle"}
        opened = []

        class FakeRPC:
            def call(self, method, params=None):
                return PickerTests().sessions() if method == "sessions.list" else None

        app.rpc = FakeRPC()
        app.open_session = lambda sid: opened.append(sid)
        app.submit("/resume", None)
        self.assertIsNotNone(app.picker)
        app.rows, app.cols = 20, 70
        rows, cur = app.frame()
        self.assertTrue(any("fix login bug" in r for r in rows))
        self.assertEqual(cur[0], 1)
        app.handle_key("down")
        app.handle_key("enter")
        self.assertIsNone(app.picker)
        self.assertEqual(opened, ["s_b"])
        app.submit("/resume", None)
        app.handle_key("esc")
        self.assertIsNone(app.picker)
        app.submit("/resume s_c", None)
        self.assertEqual(opened, ["s_b", "s_c"])


class UsageContextCompactionTests(unittest.TestCase):
    def test_labels(self):
        s = {"usage": {"calls": 2, "priced_calls": 2, "cost": 0.5, "input_tokens": 1000, "cache_read_tokens": 500,
                       "output_tokens": 200},
             "usage_subagents": {"calls": 1, "priced_calls": 0, "cost": 0, "input_tokens": 100, "output_tokens": 50},
             "context": {"used": 45200, "window": 200000, "estimated": False}}
        self.assertEqual(tui.usage_label(s), "$0.50+ · 1.6k in · 250 out")
        self.assertEqual(tui.usage_label({}), "")
        self.assertEqual(tui.usage_label({"usage": {"calls": 1, "priced_calls": 1, "estimated_calls": 1, "cost": 0.5,
                                                    "input_tokens": 10, "output_tokens": 5}}), "~$0.50 · 10 in · 5 out")
        self.assertEqual(tui.context_label(s), "ctx 45k/200k 23%")
        self.assertEqual(tui.context_label({"context": {"used": 900, "window": None, "estimated": True}}), "ctx ~900")

    def test_footer_shows_cost_and_context_at_the_right(self):
        app = tui.App("/x.html", {"cwd": "/"})
        app.cols, app.rows = 100, 12
        app.session = {"id": "s1", "status": "idle", "title": "t", "context": {"used": 1500, "window": 10000},
                       "usage": {"calls": 1, "priced_calls": 0, "input_tokens": 1000, "output_tokens": 500}}
        app.hello = {"provider": "mock"}
        foot = re.sub(r"\x1b\[[0-9;]*m", "", app.frame()[0][-1])
        self.assertTrue(foot.endswith("ctx 1.5k/10k 15% · cost n/a · 1.0k in · 500 out"), foot)

    def test_read_only_without_provider(self):
        calls = []

        class FakeRPC:
            def call(self, method, params=None):
                calls.append((method, params))
                return {}
        app = tui.App("/x.html", {"cwd": "/"})
        app.cols, app.rows = 120, 12
        app.rpc = FakeRPC()
        app.session = {"id": "s1", "status": "idle", "title": "t"}
        app.hello = {"onboarding": {"has_history": True, "has_provider": False, "moved": False}}
        foot = re.sub(r"\x1b\[[0-9;]*m", "", app.frame()[0][-1])
        self.assertIn("read only: no LLM provider", foot)
        app.submit("hello", None)
        self.assertEqual(calls, [])                      # nothing sent
        self.assertEqual(app.editor.text, "hello")       # the prompt is kept
        self.assertIn("no LLM provider", app.flash)
        app.editor.clear()
        app.submit("/retitle", None)
        self.assertEqual(calls, [])
        app.submit("/help", None)                        # commands that don't call an LLM still work
        self.assertEqual(calls, [("chat.send", {"session_id": "s1", "text": "/help"})])
        app.on_event({"event": "onboarding", "data": {"has_history": True, "has_provider": True, "moved": False}})
        self.assertTrue(app.provider_ready())

    def test_compaction_is_a_box_not_a_prompt(self):
        msg = {"role": "user", "content": [{"type": "text", "text": "summary here", "compaction": True, "compacted": 6}]}
        self.assertFalse(tui.is_prompt_msg(msg))
        text = re.sub(r"\x1b\[[0-9;]*m", "", "\n".join(tui.render_message(msg, 80, {})))
        self.assertIn("conversation compacted (6 messages)", text)
        self.assertIn("summary here", text)


class ModelPickerTests(unittest.TestCase):
    def test_filter_and_choose(self):
        mp = tui.ModelPicker([{"id": "claude-haiku-4-5", "name": "Claude Haiku 4.5"}, {"id": "claude-opus-5-5"},
                              {"id": "gpt-x"}], current="claude-opus-5-5", provider="env")
        self.assertEqual(mp.matches[mp.sel]["id"], "claude-opus-5-5")      # starts on the model in use
        for ch in "haiku":
            mp.handle(ch)
        self.assertEqual([m["id"] for m in mp.matches], ["claude-haiku-4-5"])  # display names match too
        self.assertEqual(mp.handle("enter"), ("model", "claude-haiku-4-5"))
        mp.handle("C-u")
        mp.sel = 0
        self.assertEqual(mp.handle("enter"), ("model", ""))                 # "provider default"
        self.assertEqual(mp.handle("esc"), ("cancel",))
        rows = mp.render(60, 5)
        self.assertEqual(len(rows), 5)
        self.assertIn("model: ", rows[0])

    def test_slash_model_opens_picker_and_sets(self):
        app = tui.App("/x.html", {"cwd": "/"})
        app.session = {"id": "s1", "status": "idle", "title": "t", "model": "m1"}
        calls = []

        class FakeRPC:
            def call(self, method, params=None):
                calls.append((method, params))
                if method == "models.list":
                    return {"provider": "env", "models": [{"id": "m1"}, {"id": "m2"}]}
                return {"id": "s1", "model": params.get("model"), "provider": "env"}
        app.rpc = FakeRPC()
        app.submit("/model", None)
        self.assertIsInstance(app.picker, tui.ModelPicker)
        app.handle_key("down")
        app.handle_key("enter")
        self.assertIsNone(app.picker)
        self.assertEqual(calls[-1], ("sessions.set_model", {"session_id": "s1", "model": "m2"}))
        self.assertEqual(app.session["model"], "m2")


class ProviderAndVersionsTests(unittest.TestCase):
    def test_provider_label(self):
        self.assertEqual(tui.provider_label({"id": "env", "type": "openai", "model": "m1", "cheap_model": "m0"}),
                         "env:m1")
        self.assertEqual(tui.provider_label("mock"), "mock")
        self.assertEqual(tui.provider_label(None), "")

    def test_header_shows_provider(self):
        app = tui.App("/x.html", {"cwd": "/"})
        app.session = {"id": "s1", "status": "idle", "title": "t"}
        app.hello = {"provider": {"id": "env", "type": "openai", "model": "gpt-x"}}
        app.rows, app.cols = 10, 80
        self.assertIn("env:gpt-x", plain(app.frame()[0])[0])

    def test_versions_marker(self):
        m = {"id": 3, "role": "user", "content": [{"type": "text", "text": "hi"}], "versions": 2}
        self.assertIn("(edited, 2 versions)", "\n".join(plain(tui.render_message(m, 60, {}))))
        m["versions"] = 1
        self.assertNotIn("edited", "\n".join(plain(tui.render_message(m, 60, {}))))


def _server_available():
    return os.path.exists(os.path.join(ROOT, "src", "py", "sfh", "server.py")) and \
        os.path.exists(os.path.join(ROOT, "src", "py", "sfh", "__main__.py"))


@unittest.skipUnless(_server_available(), "server not built yet")
class EndToEndTest(unittest.TestCase):
    def test_tui_under_pty(self):
        import pty
        from sfh import common, htmlfile
        tmp = tempfile.mkdtemp()
        html = os.path.join(tmp, "t.html")
        with open(html, "w") as f:
            f.write('<meta name="sfh-token" content="">\n' + htmlfile.DATA_BEGIN + htmlfile.DATA_OPEN + htmlfile.DATA_CLOSE + htmlfile.DATA_END)
        env = dict(os.environ, XDG_CONFIG_HOME=tmp, XDG_STATE_HOME=tmp, TERM="xterm", SFH_MOCK="1",
                   PYTHONPATH=os.path.join(ROOT, "src", "py") + os.pathsep + os.path.join(ROOT, "vendor"))
        for k in list(env):
            if k.startswith("AI_") or k == "ANTHROPIC_API_KEY":
                del env[k]
        master, slave = os.openpty()
        import fcntl
        import struct
        import termios
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
        proc = subprocess.Popen([sys.executable, "-m", "sfh", html, "tui"], stdin=slave, stdout=slave,
                                stderr=slave, env=env, cwd=tmp, start_new_session=True)
        os.close(slave)
        out = b""

        def read_until(needle, timeout=20, start=0):
            nonlocal out
            deadline = time.time() + timeout
            while time.time() < deadline:
                r, _, _ = select.select([master], [], [], 0.2)
                if r:
                    try:
                        out += os.read(master, 65536)
                    except OSError:
                        break
                if needle in out[start:]:
                    return True
            return False

        try:
            self.assertTrue(read_until(b"\x1b[?1049h"), out[-2000:])
            time.sleep(0.3)
            os.write(master, b"hello there\r")
            self.assertTrue(read_until(b"Mock reply"), out[-3000:])
            self.assertTrue(htmlfile.read_token(html))  # generated by the spawned server
            self.assertIn(tui.MOUSE_ON, out)
            for i in range(25):
                os.write(master, b"filler %d\r" % i)
                time.sleep(0.05)
            self.assertTrue(read_until(b"to: filler 24"), out[-3000:])
            time.sleep(0.3)
            os.write(master, b"\x1b[<64;10;10M")  # wheel up, half of the next one split across writes
            os.write(master, b"\x1b[<64;1")
            time.sleep(0.2)
            os.write(master, b"0;10Mzz")
            self.assertTrue(read_until("more lines · scroll down".encode()), out[-3000:])
            self.assertTrue(read_until("❯ zz".encode()), out[-3000:])  # nothing leaked into the prompt
            os.write(master, b"\x15")  # C-u clears the prompt
            n = len(out)
            os.write(master, b"\x1a")  # C-z: leaves and (orphaned process group, not stopped) re-enters
            self.assertTrue(read_until(b"\x1b[?1049h", start=n), out[n:])
            self.assertLess(out.index(tui.MOUSE_OFF, n), out.index(b"\x1b[?1049l", n))
            self.assertIn(tui.MOUSE_ON, out[out.index(b"\x1b[?1049h", n):])
            os.write(master, b"/resume\r")
            self.assertTrue(read_until(b"Enter resume"), out[-3000:])
            os.write(master, b"\x1b")
            time.sleep(0.3)
            os.write(master, b"\x04")
            proc.wait(10)
            read_until(b"\x1b[?1049l", 3)
            self.assertIn(b"\x1b[?1049l", out)
            self.assertIn(tui.MOUSE_OFF, out[out.rindex(tui.MOUSE_ON):])  # off again on exit
        finally:
            if proc.poll() is None:
                proc.kill()
            os.close(master)
            port = common.find_server(os.path.abspath(html))
            if port:
                info = common.health(port)
                if info and info.get("pid"):
                    try:
                        os.kill(info["pid"], 15)
                    except OSError:
                        pass
            time.sleep(0.5)
            shutil.rmtree(tmp, ignore_errors=True)


@unittest.skipUnless(_server_available(), "server not built yet")
class FirstRunTest(unittest.TestCase):
    """No history, no provider: the TUI shows the setup screen (questions/034), sets up a provider through it,
    then opens a session. Leaving the screen unconfigured stops the server the TUI started."""

    def spawn(self, html, env):
        import fcntl
        import pty  # noqa: F401
        import struct
        import termios
        master, slave = os.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 110, 0, 0))
        proc = subprocess.Popen([sys.executable, "-m", "sfh", html, "tui"], stdin=slave, stdout=slave,
                                stderr=slave, env=env, cwd=os.path.dirname(html), start_new_session=True)
        os.close(slave)
        return proc, master

    def setUp(self):
        from sfh import htmlfile
        self.tmp = tempfile.mkdtemp()
        self.html = os.path.join(self.tmp, "f.html")
        with open(self.html, "w") as f:
            f.write('<meta name="sfh-token" content="">\n' + htmlfile.DATA_BEGIN + htmlfile.DATA_OPEN +
                    htmlfile.DATA_CLOSE + htmlfile.DATA_END)
        self.env = dict(os.environ, XDG_CONFIG_HOME=self.tmp, XDG_STATE_HOME=self.tmp, TERM="xterm",
                        PYTHONPATH=os.path.join(ROOT, "src", "py") + os.pathsep + os.path.join(ROOT, "vendor"))
        for k in list(self.env):
            if k.startswith("AI_") or k in ("ANTHROPIC_API_KEY", "SFH_MOCK"):
                del self.env[k]

    def tearDown(self):
        from sfh import common
        port = common.find_server(os.path.abspath(self.html))
        info = common.health(port) if port else None
        if info and info.get("pid"):
            try:
                os.kill(info["pid"], 15)
            except OSError:
                pass
        time.sleep(0.5)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def reader(self, master):
        buf = {"out": b""}

        def read_until(needle, timeout=20, start=0):
            deadline = time.time() + timeout
            while time.time() < deadline:
                r, _, _ = select.select([master], [], [], 0.2)
                if r:
                    try:
                        buf["out"] += os.read(master, 65536)
                    except OSError:
                        break
                if needle in buf["out"][start:]:
                    return True
            return False
        return buf, read_until

    def test_setup_screen_then_session(self):
        from test_server import fake_openai_server
        from sfh import common
        seen = {}
        httpd = fake_openai_server(seen)
        proc, master = self.spawn(self.html, self.env)
        buf, read_until = self.reader(master)
        try:
            self.assertTrue(read_until(b"Connect an LLM"), buf["out"][-3000:])
            self.assertTrue(read_until("Base URL ❯".encode()), buf["out"][-3000:])
            os.write(master, b"http://127.0.0.1:%d/v1\r" % httpd.server_port)
            self.assertTrue(read_until("API key ❯".encode()), buf["out"][-3000:])
            os.write(master, b"sk-tui\r")
            self.assertTrue(read_until(b"2 models available"), buf["out"][-3000:])
            self.assertTrue(read_until(b"big-2, small-1"), buf["out"][-3000:])
            os.write(master, b"small-1\r")
            self.assertTrue(read_until(b"Provider ready"), buf["out"][-3000:])
            self.assertEqual(seen["auth"][-1], "Bearer sk-tui")
            with open(os.path.join(self.tmp, "singlefile", "credentials")) as f:
                self.assertIn("PROVIDER_127_0_0_1_KEY=sk-tui", f.read())
            os.write(master, b"\x04")
            proc.wait(10)
            self.assertTrue(common.find_server(os.path.abspath(self.html)))  # configured: the server keeps running
        finally:
            if proc.poll() is None:
                proc.kill()
            os.close(master)
            httpd.shutdown()

    def test_leaving_unconfigured_stops_the_server(self):
        from sfh import common
        proc, master = self.spawn(self.html, self.env)
        buf, read_until = self.reader(master)
        try:
            self.assertTrue(read_until("Base URL ❯".encode()), buf["out"][-3000:])
            self.assertTrue(read_until(b"server this TUI started"), buf["out"][-3000:])
            os.write(master, b"\x03")   # C-c quits
            proc.wait(10)
            deadline = time.time() + 5
            while common.find_server(os.path.abspath(self.html)) and time.time() < deadline:
                time.sleep(0.2)
            self.assertIsNone(common.find_server(os.path.abspath(self.html)))
        finally:
            if proc.poll() is None:
                proc.kill()
            os.close(master)


class BranchTests(unittest.TestCase):
    def test_branch_result_and_fork_marker(self):
        br = tui.parse_branch_result('<thread-message from="b_123" title="port check" kind="subagent" status="completed">All good</thread-message>')
        self.assertEqual(br, {"branch_id": "b_123", "title": "port check", "kind": "subagent", "status": "completed", "body": "All good"})
        msg_u = {"id": 1, "role": "user", "content": [{"type": "text", "text": "hello"}]}
        msg_a = {"id": 2, "role": "assistant", "content": [{"type": "text", "text": "world"}]}
        msg_br = {"id": 3, "role": "user", "content": [{"type": "text", "text": '<thread-message from="b_123" title="port check" kind="subagent" status="completed">All good</thread-message>'}]}
        self.assertFalse(tui.is_prompt_msg(msg_br))
        self.assertTrue(tui.starts_turn(msg_br))
        binfo = {
            "active_branch_id": "b_main",
            "main_branch_id": "b_main",
            "branches": [
                {"id": "b_main", "name": "Main", "kind": "main", "is_active": True, "is_main": True, "origin_id": None, "own_messages": 2},
                {"id": "b_123", "name": "port check", "kind": "aside", "is_active": False, "is_main": False, "origin_id": 2, "own_messages": 2},
            ],
            "forks": {"2": [{"branch_id": "b_123", "name": "port check", "kind": "aside", "status": "idle", "own_messages": 2, "is_main": False, "nested": 0}]},
        }
        lines = tui.render_transcript([("msg", msg_u), ("msg", msg_a), ("msg", msg_br)], None, 80, branches=binfo)
        plain = "\n".join(tui._SGR_RE.sub("", l) for l in lines)
        self.assertIn("⑂ 1 branch: port check", plain)
        self.assertIn("subagent port check [completed]", plain)
        self.assertIn("All good", plain)

    def test_branch_picker_and_switch_command(self):
        binfo = {
            "active_branch_id": "b_main",
            "main_branch_id": "b_main",
            "branches": [
                {"id": "b_main", "name": "Main", "kind": "main", "parent_branch_id": None, "is_active": True, "is_main": True, "own_messages": 4, "status": "idle"},
                {"id": "b_side", "name": "port hash check", "kind": "aside", "parent_branch_id": "b_main", "is_active": False, "is_main": False, "own_messages": 2, "status": "idle"},
            ],
            "forks": {},
        }
        p = tui.BranchPicker(binfo)
        self.assertEqual(p.sel, 0)
        p.handle("down")
        self.assertEqual(p.handle("enter"), ("branch", "b_side"))
        rows = p.render(80, 5)
        plain = "\n".join(tui._SGR_RE.sub("", r) for r in rows)
        self.assertIn("port hash check", plain)
        self.assertIn("↳ port hash check", plain)

        # App /branch opens picker and Enter calls branches.switch
        calls = []

        class FakeRPC:
            def call(self, m, params=None):
                calls.append((m, params))
                if m == "branches.list":
                    return binfo
                if m == "branches.switch":
                    return {"session": {"id": "s_1", "title": "t", "status": "idle", "branch_count": 2,
                                        "active_branch_id": "b_side", "active_branch_name": "port hash check"},
                            "branch_id": "b_side", "branches": binfo,
                            "messages": [{"id": 1, "role": "user", "content": [{"type": "text", "text": "hi"}]}],
                            "draft": None, "queue": [], "tools": {}}
                return {}

        app = tui.App("/tmp/h.html", {})
        app.rpc = FakeRPC()
        app.session = {"id": "s_1", "title": "t", "status": "idle", "branch_count": 2,
                       "active_branch_id": "b_main", "active_branch_name": "Main"}
        app.branch_id = "b_main"
        app.branches = binfo
        app.submit("/branch", None)
        self.assertIsInstance(app.picker, tui.BranchPicker)
        app.handle_key("down")
        app.handle_key("enter")
        self.assertIsNone(app.picker)
        self.assertEqual(app.branch_id, "b_side")
        self.assertIn(("branches.switch", {"session_id": "s_1", "branch_id": "b_side"}), calls)
        rows, _ = app.frame()
        self.assertIn("port hash check (2)", tui._SGR_RE.sub("", rows[0]))


if __name__ == "__main__":
    unittest.main()
