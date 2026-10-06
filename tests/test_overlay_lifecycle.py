"""The overlay's lifecycle, which had no tests at all.

The T3 confirmation logic in `test_overlay.py` is thorough - 24 tests on what the
dialog *says* and what an answer does. Not one of them called `show()`, `hide()`
or `destroy()`. Those three methods are what a user touches first, they are the
only ones that construct a real Tk root, and they were the ones that were broken.

What was broken, all three found by running it rather than by reading it:

1. **Nothing ever ran the Tk event loop.** No `mainloop()`, no `update()`, no
   pump. A `tk.Tk()` that is never pumped still maps a window - `winfo_viewable`
   is 1, the title is set, `geometry` is honoured - and it paints nothing,
   because painting *is* event processing. That is the whole of "an empty white
   window titled Local assistant 0.3.0": mapped, correct title, no content.

2. **`_build()` called `_poll()` while `self._root` was still `None`.** `_poll`
   opens with `if self._closing or self._root is None: return`, so it returned
   immediately and no `after()` job was ever registered. The one-second poll for
   pending T3 confirmations - the thing that makes a destructive action
   reachable - had never once been armed. Confirmed by instrumenting the call:
   `_poll` was entered with `self._root = None`.

3. **Tk was being touched from two threads.** The root was built on whichever
   thread called `show()` (the hotkey thread), while `_pump` ran on its own
   thread and called `_handle_event` -> `_append` -> widget methods. tkinter
   requires every call on one thread, the one that created the root, and says so
   loudly: `RuntimeError: main thread is not in main loop`.

A fourth defect came from using the thing: the prompt window was permanently
`-topmost`, so it sat over everything, all day, on a machine with a taskbar of
its own. Always-on-top is right for the confirmation dialog - a destructive
approval that can be buried is an approval nobody sees - and wrong for the
conversation window, which raises itself on demand anyway.

The fake-tkinter approach here is the one `test_overlay.py` already uses, and it
is what makes these assertable without a display. The two thread tests are the
part that needed faking at all: they assert *which thread* Tk was created on and
which thread ran the loop, which is the property the runtime actually broke.
"""

from __future__ import annotations

import queue
import threading
import time

import pytest

from assistant import overlay as O


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #


class FakeRoot:
    """A Tk root that *actually runs an event loop*.

    An earlier version of this fake only recorded `after` callbacks and left
    draining them to the test. That made these tests hollow: they would have
    passed against a real Tk whose loop was never started, because the test
    supplied the pumping that production does not. So the fake runs its own loop -
    due jobs every 5 ms, until destroyed - which is what makes "without anyone
    asking, the dialog appears" an assertion about the code rather than about the
    test.

    It records the thread it was built on and the thread its loop ran on, because
    that is the property the runtime actually broke.
    """

    def __init__(self, log, name="root"):
        self.log = log
        self.name = name
        self._jobs: list[tuple[float, object]] = []
        self._counter = 0
        #: The delays the code asked for, in order - 50 for the queue pump, 1000
        #: for the confirmation poll. Asserted on rather than on due-times, which
        #: are wall-clock and therefore say nothing about the interval.
        self.scheduled_ms: list[int] = []
        self.thread_created = threading.current_thread().name
        self.thread_mainloop: str | None = None
        self.mainloop_calls = 0
        self.attributes_set: dict = {}
        self.state_value = "normal"
        self.destroyed = False
        self._gone = threading.Event()

    def title(self, *_a):
        pass

    def configure(self, **_kw):
        pass

    def geometry(self, *_a):
        pass

    def minsize(self, *_a):
        pass

    def attributes(self, name, value=None):
        if value is None:
            return self.attributes_set.get(name)
        self.attributes_set[name] = value
        return value

    def after(self, ms, fn):
        self._counter += 1
        self.scheduled_ms.append(int(ms))
        self._jobs.append((time.monotonic() + ms / 1000.0, fn))
        return f"after#{self._counter}"

    def _run_due(self) -> None:
        now = time.monotonic()
        due = [j for j in self._jobs if j[0] <= now]
        self._jobs = [j for j in self._jobs if j[0] > now]
        for _t, fn in due:
            fn()

    def deiconify(self):
        self.state_value = "normal"

    def withdraw(self):
        self.state_value = "withdrawn"

    def lift(self):
        pass

    def focus_force(self):
        pass

    def state(self):
        return self.state_value

    def destroy(self):
        self.destroyed = True
        self._gone.set()

    def mainloop(self):
        self.mainloop_calls += 1
        self.thread_mainloop = threading.current_thread().name
        self.log.append("mainloop-entered")
        # A real loop: service due jobs until the root is destroyed.
        while not self._gone.is_set():
            try:
                self._run_due()
            except Exception:  # noqa: BLE001 - a fake loop must not hide errors
                raise
            self._gone.wait(0.005)


class FakeWidget:
    """A widget that accepts everything and records what matters.

    It started with only the methods `test_overlay.py`'s fakes had, and `_Confirm`
    turned out to need more: it calls `attributes`, `title`, `geometry`,
    `protocol`, `transient` and `after` on its Toplevel before it registers
    itself. A fake missing any of those raises, which looks exactly like the
    production defect under test - a dialog that silently fails to appear. A
    permissive fake is the honest one here: it fails on *behaviour*, not on
    method coverage.
    """

    def __init__(self, *_a, **_kw):
        self.scheduled_ms: list[int] = []

    def pack(self, *_a, **_kw):
        pass

    def grid(self, *_a, **_kw):
        pass

    def configure(self, *_a, **_kw):
        pass

    def config(self, *_a, **_kw):
        pass

    def insert(self, *_a, **_kw):
        pass

    def see(self, *_a, **_kw):
        pass

    def bind(self, *_a, **_kw):
        pass

    def delete(self, *_a, **_kw):
        pass

    def get(self):
        return ""

    def focus_set(self):
        pass

    def destroy(self):
        pass

    def tag_configure(self, *_a, **_kw):
        pass

    # -- what _Confirm needs, which test_overlay.py's fakes never had to offer --
    def after(self, ms, fn=None):
        self.scheduled_ms.append(int(ms))
        return "widget-after"

    def after_cancel(self, *_a):
        pass

    def attributes(self, name, value=None):
        return value

    def title(self, *_a):
        pass

    def geometry(self, *_a):
        pass

    def protocol(self, *_a, **_kw):
        pass

    def transient(self, *_a):
        pass

    def grab_set(self):
        pass

    def state(self):
        return "normal"


@pytest.fixture
def tkdesk(monkeypatch):
    """Every widget faked, and a `Tk()` that hands back a recording FakeRoot."""
    log: list[str] = []
    roots: list[FakeRoot] = []

    def fake_tk(*_a, **_kw):
        r = FakeRoot(log)
        roots.append(r)
        return r

    monkeypatch.setattr(O.tk, "Tk", fake_tk)
    for name in ("Toplevel", "Label", "Button", "Text", "Frame", "Entry"):
        monkeypatch.setattr(O.tk, name, FakeWidget)
    # `overlay.py` does `from tkinter import font as tkfont`, so the name is bound
    # at import. Patching `tk.font` would miss it, and the real nametofont needs
    # a live default root - which the faked `Tk()` deliberately does not provide.
    monkeypatch.setattr(
        O, "tkfont",
        type("f", (), {"nametofont": staticmethod(lambda _n: "font")})(),
    )
    ov = O.Overlay(lambda op, **kw: {"ok": True, "result": {"pending": []}},
                   title="Local assistant 0.3.0")
    ov._dlg = None
    ov._answered = set()
    yield ov, roots, log
    ov.destroy()


# --------------------------------------------------------------------------- #
# 1. the event loop
# --------------------------------------------------------------------------- #


def test_the_window_runs_an_event_loop(tkdesk) -> None:
    """A mapped window with no loop behind it is a white rectangle.

    The symptom users reported: the window opens, carries the right title, and is
    empty. Every one of those is true without a single event being processed.
    """
    ov, roots, _log = tkdesk

    ov.show()

    assert len(roots) == 1, "show() must build a root"
    assert roots[0].mainloop_calls >= 1, (
        "the Tk thread must run an event loop; without one the window maps but "
        "never paints and never reads a keypress"
    )


def test_the_loop_runs_on_the_thread_that_built_the_root(tkdesk) -> None:
    """tkinter's rule, and the reason for the whole design.

    `root.update()` from any thread but the one that created the root raises
    `RuntimeError: main thread is not in main loop` - measured, not remembered.
    So the thread that builds the root has to be the thread that runs the loop,
    and every other thread has to go through the queue.
    """
    ov, roots, _log = tkdesk

    ov.show()

    root = roots[0]
    assert root.thread_mainloop == root.thread_created, (
        f"root was built on {root.thread_created} but the loop ran on "
        f"{root.thread_mainloop}; tkinter only tolerates one thread"
    )
    assert root.thread_created != "MainThread", (
        "show() is called from the hotkey thread, so building on the main thread "
        "would be the same defect in a different place"
    )


def test_the_main_loop_thread_is_named_so_a_hang_is_findable(tkdesk) -> None:
    """A wedged Tk thread with a generic name is a wedge nobody diagnoses."""
    ov, roots, _log = tkdesk

    ov.show()

    assert any(t.name == "overlay-tk" and t.is_alive()
               for t in threading.enumerate()), (
        "the Tk thread needs a name; 'Thread-7 (run)' helps nobody at 3am"
    )


# --------------------------------------------------------------------------- #
# 2. the confirmation poll has to be armed
# --------------------------------------------------------------------------- #


def test_building_the_window_arms_the_confirmation_poll(tkdesk) -> None:
    """The one-second poll for pending T3 confirmations.

    `_build()` used to call `_poll()` while `self._root` was still None. `_poll`
    opens with `if self._closing or self._root is None: return`, so it returned
    at once and no `after()` job existed. The result: a destructive action queued
    from Telegram or the scheduler would never raise a dialog, and nothing would
    say so. This is the worst shape a bug in this module can take - the safe path
    silently stops existing.
    """
    ov, roots, _log = tkdesk

    ov.show()

    root = roots[0]
    armed = list(root.scheduled_ms)
    assert armed, (
        "no after() job was registered, so the confirmation poll never ran; "
        "_poll was being called before self._root existed"
    )
    assert 1000 in armed, (
        f"expected the 1000ms confirmation poll, got {armed}"
    )


def test_the_poll_is_not_armed_before_a_root_exists(tkdesk) -> None:
    """The guard in `_poll` is right; calling it too early was the bug.

    Asserted directly so the ordering cannot be reintroduced by moving a line:
    with no root, `_poll` must do nothing at all rather than half-arm.
    """
    ov, _roots, _log = tkdesk
    ov._root = None
    ov._poll()
    assert ov._poll_job is None, "with no root there is nothing to schedule on"


def test_a_pending_confirmation_appears_without_anyone_asking(tkdesk) -> None:
    """End to end through the loop: queue a T3 request, and it must surface.

    Nobody calls `show()`, nobody calls `_poll()`. The poll has to be armed at
    build time and driven by the event loop, because that is the only way a
    destructive action queued from Telegram or the scheduler ever becomes
    visible. Driven by waiting, not by pumping - a test that supplies its own
    pumping proves nothing about who supplies it in production.
    """
    ov, roots, _log = tkdesk
    pending = [{
        "id": "c1", "tool": "file.delete", "payload_hash": "a" * 64,
        "preview": {"action": "move to quarantine", "proposed": "G:/x.txt",
                    "resolved": "G:/x.txt", "count": 1, "total_bytes": 1},
        "args": {"path": "G:/x.txt"},
    }]
    calls: list[str] = []

    def call(op, **_kw):
        calls.append(op)
        return {"ok": True, "result": {"pending": pending if op == "confirm_list" else []}}

    ov._call = call
    ov.show()

    deadline = time.monotonic() + 10.0
    while ov._dlg is None and time.monotonic() < deadline:
        time.sleep(0.02)

    assert "confirm_list" in calls, "the poll never asked the daemon"
    assert ov._dlg is not None, (
        "a queued destructive action must raise a dialog on its own; it did not"
    )


# --------------------------------------------------------------------------- #
# 3. one thread touches Tk
# --------------------------------------------------------------------------- #


def test_no_widget_is_touched_from_another_thread(tkdesk) -> None:
    """`say()` arrives from the brain's worker thread and must be marshalled.

    The original `_pump` was its own thread and called `_handle_event` directly,
    which reaches `Text.insert`. With the root built on the hotkey thread, that
    is a cross-thread Tk call - the exact thing tkinter refuses.
    """
    ov, roots, _log = tkdesk
    ov.show()

    touched: list[str] = []

    def record(text, tag=""):
        touched.append(threading.current_thread().name)

    original_append = ov._append
    ov._append = record
    try:
        # From a thread that is emphatically not the Tk thread.
        worker = threading.Thread(target=lambda: ov.say("hello"), name="brain-worker")
        worker.start()
        worker.join(timeout=5)

        deadline = time.monotonic() + 10.0
        while not touched and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        ov._append = original_append

    assert touched, "say() was queued but never reached a widget"
    assert touched == ["overlay-tk"], (
        f"widgets were touched from {touched}; Tk allows only its own thread"
    )


def test_show_from_inside_the_tk_thread_does_not_deadlock(tkdesk) -> None:
    """`_ask` calls `self.show()` while already on the Tk thread.

    If `show()` unconditionally posted to the Tk thread and waited, that call
    would wait for itself forever. The window that opens on a Telegram prompt
    would hang the assistant - and it would hang *silently*, because the caller
    is a queue drain.
    """
    ov, roots, _log = tkdesk
    ov.show()

    done = threading.Event()
    box: dict = {}

    def job():
        try:
            ov.show()
            box["ok"] = True
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc
        finally:
            done.set()

    # Post it the way _ask's path would, and let the loop run it.
    ov._post("call", job)

    assert done.wait(timeout=10), "show() from the Tk thread deadlocked"
    assert box.get("ok"), f"show() from the Tk thread failed: {box.get('error')}"


# --------------------------------------------------------------------------- #
# 4. the window that covers the desktop
# --------------------------------------------------------------------------- #


def test_the_prompt_window_is_not_permanently_on_top(tkdesk) -> None:
    """A 24/7 always-on-top window is hostile on a machine with other work on it.

    Reported as a real annoyance: the assistant's chat window sat over everything
    and had to be closed to see the desktop. `show()` already raises the window
    on demand with `lift()` and `focus_force()`, so the permanent flag buys
    nothing for this window.
    """
    ov, roots, _log = tkdesk

    ov.show()

    assert roots[0].attributes_set.get("-topmost") is not True, (
        "the prompt window must not be permanently always-on-top; show() raises "
        "it on demand, and the user cannot use their own desktop otherwise"
    )


def test_but_it_is_still_raised_when_asked_for(tkdesk) -> None:
    """Not-topmost must not become invisible-behind-everything."""
    ov, roots, _log = tkdesk
    ov.show()                      # build first, so there is a root to instrument
    root = roots[0]
    raised: list[str] = []
    root.lift = lambda: raised.append("lift")
    root.focus_force = lambda: raised.append("focus")

    ov.show()                      # now the summon

    assert "lift" in raised and "focus" in raised, (
        "show() must still bring the window to the front when it is summoned"
    )


def test_the_confirmation_dialog_stays_on_top(tkdesk) -> None:
    """The safety property that must not be traded away with the other one.

    `overlay.py` justifies topmost on the T3 dialog and the reasoning is sound:
    a destructive approval that can be buried behind a browser is an approval
    nobody sees. Fixing the chat window must not quietly relax this one - a test
    that cannot fail when the property is broken is not a test of it.
    """
    from assistant import overlay as om

    recorded: dict = {}

    class Top(FakeWidget):
        def __init__(self, *_a, **_kw):
            super().__init__()
            recorded["made"] = True

        def title(self, *_a):
            pass

        def geometry(self, *_a):
            pass

        def attributes(self, name, value=None):
            recorded[name] = value
            return value

        def protocol(self, *_a, **_kw):
            pass

        def transient(self, *_a):
            pass

        def grab_set(self):
            pass

        def after(self, ms, fn):
            return "job"

    import tkinter as tk

    original = om.tk.Toplevel
    om.tk.Toplevel = Top
    try:
        ov = O.Overlay(lambda op, **kw: {"ok": True, "result": {"pending": []}})
        ov._root = object()
        ov._dlg = None
        ov._answered = set()
        dlg = om._Confirm(ov, {
            "id": "c1", "tool": "file.delete", "payload_hash": "b" * 64,
            "preview": {"action": "quarantine", "proposed": "G:/y", "resolved": "G:/y",
                        "count": 1, "total_bytes": 1},
            "args": {},
        }, "file.delete", {"action": "quarantine", "proposed": "G:/y",
                           "resolved": "G:/y", "count": 1, "total_bytes": 1},
            ttl_s=5)
    finally:
        om.tk.Toplevel = original

    assert recorded.get("-topmost") is True, (
        "the T3 dialog must stay always-on-top; burying a destructive approval is "
        "how it gets approved without being read"
    )
    assert dlg is not None
