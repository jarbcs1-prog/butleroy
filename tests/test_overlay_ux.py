"""The chat window must be usable as a window, not only as a turn runner.

The user's dogfood report (D106): no right-click copy/paste anywhere, a
98-entry `file.list` dumped raw into the transcript, and no way to put a file
or folder into the conversation. Three affordances, each failing-first:

1. `_attach_edit_menu` — right-click Copy on read-only text, Cut/Copy/Paste
   on the entry. Tk ships no Button-3 menu; without one the transcript is
   write-only.
2. `_render_result` summarises `entries` lists — count plus the first names,
   never 98 raw rows. Truncation already exists; a mid-list cut is not a
   summary.
3. `@path` expansion — `@F:/theseas/foo.py what does this do` stages the file
   through the daemon's own T0 reads (metadata, then read or list), so an
   unapproved path refuses loudly instead of leaking, and a refusal aborts
   the turn before any VRAM is spent.
"""

from __future__ import annotations

import pytest

from assistant import overlay as O


# ---------------------------------------------------------------------------
# 1. entries lists are summarised, not dumped
# ---------------------------------------------------------------------------

def _big_list(n=98):
    return {
        "path": "F:/theseas",
        "count": n,
        "entries": [
            {"name": f"file_{i:03d}.py", "dir": False, "size": 100 + i}
            for i in range(n)
        ],
    }


def test_entries_list_shows_count_and_first_names():
    text = O._render_result("file.list", _big_list())
    assert "98" in text
    assert "file_000.py" in text


def test_entries_list_does_not_dump_everything():
    text = O._render_result("file.list", _big_list())
    assert "file_097.py" not in text
    assert "more" in text.lower()


def test_small_list_still_renders_whole():
    text = O._render_result("file.list", _big_list(3))
    assert "file_000.py" in text and "file_002.py" in text
    assert "more" not in text.lower()


# ---------------------------------------------------------------------------
# 2. @path expansion
# ---------------------------------------------------------------------------

class FakeReads:
    """A daemon-shaped call serving the three T0 reads expansion needs."""

    def __init__(self, *, refuse=None):
        self.calls = []
        self.refuse = refuse

    def __call__(self, op, **args):
        self.calls.append((op, args))
        if self.refuse:
            return {"ok": False, "error": self.refuse}
        if op == "tool_call" and args.get("tool") == "file.metadata":
            p = args["args"]["path"]
            return {"ok": True, "result": {
                "kind": "dir" if p.endswith("/") else "file"}}
        if op == "tool_call" and args.get("tool") == "file.read":
            return {"ok": True, "result": {
                "path": args["args"]["path"], "bytes": 42,
                "truncated": False, "text": "print('hi')"}}
        if op == "tool_call" and args.get("tool") == "file.list":
            return {"ok": True, "result": {
                "path": args["args"]["path"], "count": 2,
                "entries": [{"name": "a.py", "dir": False, "size": 1},
                            {"name": "b", "dir": True, "size": 0}]}}
        raise AssertionError(f"unexpected call: {op} {args}")


def test_no_at_sign_means_no_calls():
    call = FakeReads()
    text, error = O._expand_attachments("plain question", call)
    assert error is None and text == "plain question"
    assert call.calls == []


def test_email_like_text_is_not_an_attachment():
    call = FakeReads()
    text, error = O._expand_attachments("mail me at bob@example.com", call)
    assert error is None and text == "mail me at bob@example.com"
    assert call.calls == []


def test_file_attach_stages_content_before_the_question():
    call = FakeReads()
    text, error = O._expand_attachments(
        "@F:/theseas/foo.py what does this do", call)
    assert error is None
    assert "print('hi')" in text
    assert text.rstrip().endswith("what does this do")
    tools = [a.get("tool") for _, a in call.calls]
    assert tools[0] == "file.metadata" and "file.read" in tools


def test_dir_attach_lists_names():
    call = FakeReads()
    text, error = O._expand_attachments("@F:/theseas/ summarise", call)
    assert error is None
    assert "a.py" in text and "b" in text


def test_quoted_path_with_spaces():
    call = FakeReads()
    text, error = O._expand_attachments('@"C:/my docs/note.txt" read this', call)
    assert error is None
    assert "print('hi')" in text
    assert call.calls[0][1]["args"]["path"] == "C:/my docs/note.txt"


def test_refusal_aborts_with_the_reason():
    call = FakeReads(refuse="outside every approved workspace root")
    text, error = O._expand_attachments("@F:/nope/x.py go", call)
    assert text is None
    assert "outside every approved workspace" in error


class StubEntry:
    def __init__(self, text):
        self._text = text
        self.deleted = False

    def get(self):
        return self._text

    def delete(self, a, b):
        self.deleted = True


class StubTranscript:
    def __init__(self):
        self.lines = []

    def configure(self, **kw):
        pass

    def insert(self, at, text, tags=None):
        self.lines.append(text)

    def see(self, at):
        pass


def test_submit_with_refused_attach_sends_no_turn():
    """The refusal aborts before any VRAM is spent: no worker thread, no
    `brain_turn`, `_busy` back to False, and the reason on screen."""
    call = FakeReads(refuse="outside every approved workspace root")
    ov = O.Overlay(call)
    ov._root = object()
    ov.entry = StubEntry("@F:/nope/x.py go")
    ov.transcript = StubTranscript()
    ov._submit()
    assert ov.entry.deleted
    assert any("outside every approved workspace" in line
               for line in ov.transcript.lines)
    assert not any(op == "brain_turn" for op, _ in call.calls)
    assert ov._busy is False


def test_submit_without_attach_still_turns():
    seen = {}

    class ThreadCatcher:
        def __init__(self, *a, **k):
            seen["started"] = True

        def start(self):
            seen["started"] = True

    call = FakeReads()
    ov = O.Overlay(call)
    ov._root = object()
    ov.entry = StubEntry("plain question")
    ov.transcript = StubTranscript()
    real_thread = O.threading.Thread
    O.threading.Thread = ThreadCatcher
    try:
        ov._submit()
    finally:
        O.threading.Thread = real_thread
    assert seen.get("started") is True
    assert call.calls == []  # no reads: nothing to stage


def test_attach_token_quotes_spaces():
    assert O._attach_token("C:/plain/x.py") == "@C:/plain/x.py"
    assert O._attach_token("C:/my docs/x.py") == '@"C:/my docs/x.py"'


# ---------------------------------------------------------------------------
# 3. right-click menus
# ---------------------------------------------------------------------------

class StubWidget:
    """The widget protocol the menu callbacks need — no Tk required."""

    def __init__(self, text="hello world"):
        self.text = text
        self.clipboard = None
        self.deleted = []
        self.inserted = []
        self._sel = (0, 5)

    def selection_get(self):
        return self.text[self._sel[0]:self._sel[1]]

    def clipboard_clear(self):
        self.clipboard = None

    def clipboard_append(self, s):
        self.clipboard = (self.clipboard or "") + s

    def delete(self, a, b=None):
        self.deleted.append((a, b))

    def insert(self, at, s):
        self.inserted.append((at, s))

    def selection_present(self):
        return True


def test_copy_puts_the_selection_on_the_clipboard():
    w = StubWidget()
    O._copy_selection(w)
    assert w.clipboard == "hello"


def test_cut_copies_then_deletes():
    w = StubWidget()
    O._cut_selection(w)
    assert w.clipboard == "hello"
    assert w.deleted != []


def test_paste_inserts_clipboard_content():
    w = StubWidget()
    w.clipboard = None
    O._paste_clipboard(w, get=lambda: "PASTED")
    assert w.inserted and w.inserted[0][1] == "PASTED"


def test_copy_with_no_selection_is_a_silent_noop():
    class Empty(StubWidget):
        def selection_get(self):
            raise Exception("no selection")
    w = Empty()
    O._copy_selection(w)  # must not raise
    assert w.clipboard is None


def _menu_labels(menu):
    last = menu.index("end")
    return [menu.entrycget(i, "label") for i in range(last + 1)]


def test_menu_factory_registers_button3():
    tk = pytest.importorskip("tkinter")
    root = tk.Tk()
    root.withdraw()
    try:
        text = tk.Text(root)
        O._attach_edit_menu(text, editable=False)
        assert text.bind("<Button-3>") != ""
        # Labels asserted on the builder, where widgets are real: the bind
        # only stores a holder, because the menu itself is built on the first
        # right-click (eager construction aborts the worker-thread suite).
        assert _menu_labels(O._build_edit_menu(text, editable=False)) == [
            "Copy", "Select all"]
        entry = tk.Entry(root)
        O._attach_edit_menu(entry, editable=True)
        assert entry.bind("<Button-3>") != ""
        assert getattr(entry, "_edit_menu", None) is not None
        assert _menu_labels(O._build_edit_menu(entry, editable=True)) == [
            "Cut", "Copy", "Paste", "Select all"]
    finally:
        root.destroy()


def test_menu_attach_on_imaginary_widgets_is_a_noop():
    class NoBind:
        pass
    O._attach_edit_menu(NoBind(), editable=False)  # must not raise
    assert getattr(NoBind(), "_edit_menu", None) is None
