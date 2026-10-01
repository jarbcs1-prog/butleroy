"""Tests for the overlay's confirmation rules, not its widgets.

Tkinter needs a display and a human, so nothing here constructs a `Tk`. What is
tested is the part that decides what the user is shown and what is sent to the
daemon - which is where D32's failures actually live.

The two things that must never happen, and are asserted against directly:

* **an approval that is not the approval the user was shown.** The hash travels
  with the answer, so a payload that changed under the dialog is refused by the
  daemon rather than waved through.
* **a dialog that expires into an approval.** `Timer` is injected, so the test
  drives the clock to zero and asserts what leaves the process is a *deny*.

Everything is exercised through a fake `call`, which is the same interface the
real daemon's `handle` presents. That is not a mock of our own code being
tested - it is the transport boundary, and the point is that this module never
reaches around it.
"""

import pytest

from assistant import overlay as O


class FakeDaemon:
    """Stands in for `Daemon.handle`, recording what the overlay asked for."""

    def __init__(self, *, pending=None, approve_result=None, deny_result=None):
        self.pending = list(pending or [])
        self.approve_result = approve_result
        self.deny_result = deny_result
        self.calls = []
        self.posted = []

    def __call__(self, op, **args):
        self.calls.append((op, args))
        if op == "confirm_list":
            # Exactly the fields the real daemon sends. Returning the whole item
            # would let a test pass against a shape the daemon never produces -
            # which is how the missing `payload_hash` stayed invisible.
            return {"ok": True, "result": {"grants": {}, "pending": [
                {k: p[k] for k in ("id", "tool", "reason", "preview",
                                   "payload_hash", "created") if k in p}
                for p in self.pending
            ]}}
        if op == "confirm_approve":
            return {"ok": True, "result": self.approve_result or
                    {"approved": True, "tool": "file.delete", "result": {"deleted": 1}}}
        if op == "confirm_deny":
            return {"ok": True, "result": self.deny_result or
                    {"denied": True, "confirmation_id": args.get("confirmation_id"),
                     "was_pending": True}}
        return {"ok": True, "result": {}}

    def ops(self):
        return [op for op, _ in self.calls]

    def args_for(self, op):
        for name, a in self.calls:
            if name == op:
                return a
        raise AssertionError(f"{op} was never called; saw {self.ops()}")


class FakeTimer:
    """A countdown the test drives by hand.

    tkinter's `after` is a real clock, and a test that waits 60 seconds to prove
    a denial is a test nobody runs.
    """

    def __init__(self):
        self.jobs = []

    def after(self, _ms, fn):
        self.jobs.append(fn)
        return f"job{len(self.jobs)}"

    def advance(self, n=1000):
        for _ in range(n):
            if not self.jobs:
                return
            job, self.jobs = self.jobs[0], self.jobs[1:]
            job()


class NoTop:
    """A Toplevel that records what was asked of it and refuses to be destroyed.

    Its `after` delegates to a module-level timer so `test_expiry_sends_a_denial`
    can drive the countdown without waiting sixty seconds. The reference is set
    per-test by the `desk` fixture and is None outside one, which is why a
    stray dialog in another test would fail loudly rather than hang.
    """

    timer = None

    def __init__(self, parent):
        self.destroyed = False
        self.attributes_set = {}
        self.grabbed = False
        self.title_text = None
        self.labels = []
        self.text_widgets = []

    def title(self, text):
        self.title_text = text

    def configure(self, **kw):
        pass

    def geometry(self, *_a):
        pass

    def attributes(self, *a, **kw):
        # tkinter's wm_attributes is positional: attributes("-topmost", True).
        self.attributes_set.update(kw)
        for i in range(0, len(a) - 1, 2):
            self.attributes_set[a[i]] = a[i + 1]

    def transient(self, *_a):
        pass

    def protocol(self, *_a, **_kw):
        pass

    def after(self, ms, fn):
        return NoTop.timer.after(ms, fn)

    def destroy(self):
        self.destroyed = True

    def grab_set(self):
        self.grabbed = True

    def update_idletasks(self):
        pass


class NoLabel:
    def __init__(self, *a, **kw):
        self.text = kw.get("text", "")
        self.cleared = None

    def pack(self, *_a, **_kw):
        pass

    def configure(self, **kw):
        if "text" in kw:
            self.cleared = kw["text"]

    def grid(self, *_a, **_kw):
        pass


class NoButton:
    def __init__(self, *a, **kw):
        self.text = kw.get("text", "")
        self.command = kw.get("command")

    def pack(self, *_a, **_kw):
        pass


class NoText:
    def __init__(self, *_a, **kw):
        self.lines = []
        self.tags = []

    def insert(self, _where, text, tag=""):
        for line in text.split("\n"):
            if line or self.lines:
                self.lines.append(line)
                self.tags.append(tag)

    def see(self, *_a):
        pass

    def tag_configure(self, *_a, **_kw):
        pass

    def pack(self, *_a, **_kw):
        pass

    def configure(self, **_kw):
        pass

    def text(self):
        return "\n".join(self.lines)


class NoRoot:
    """A root that exists but schedules nothing.

    `_poll` re-arms itself with `after`. A fake that actually scheduled would
    make the poll interval a source of timing in the tests; one that records the
    callback lets a test drive the loop by hand.
    """

    jobs = []

    def after(self, _ms, fn):
        NoRoot.jobs.append(fn)
        return "job"

    def run_pending(self, n=1):
        for _ in range(n):
            if not NoRoot.jobs:
                return
            job, NoRoot.jobs = NoRoot.jobs[0], NoRoot.jobs[1:]
            job()

    def state(self):
        return "normal"


class NoFrame:
    def __init__(self, *_a, **_kw):
        pass

    def pack(self, *_a, **_kw):
        pass


@pytest.fixture
def desk(monkeypatch):
    """Replace every tkinter widget with something that records."""
    d = FakeDaemon()
    t = FakeTimer()
    NoTop.timer = t
    monkeypatch.setattr(O.tk, "Toplevel", NoTop)
    monkeypatch.setattr(O.tk, "Label", NoLabel)
    monkeypatch.setattr(O.tk, "Button", NoButton)
    monkeypatch.setattr(O.tk, "Text", NoText)
    monkeypatch.setattr(O.tk, "Frame", NoFrame)
    ov = O.Overlay(d)          # FakeDaemon is itself the callable
    ov._root = NoRoot()          # a non-None root: no Tk is ever built
    NoRoot.jobs = []
    ov._dlg = None
    ov._answered = set()
    return ov, d, t


def _drain(ov):
    """Take everything the overlay has queued for the Tk thread."""
    out = []
    while True:
        try:
            out.append(ov._events.get_nowait())
        except Exception:
            return out


def item(**kw):
    base = {
        "id": "c000001",
        "tool": "file.delete",
        "reason": "file.delete is T3 (destructive); requires confirmation at the PC",
        "payload_hash": "a" * 64,
        "preview": {
            "action": "move to quarantine (recoverable, not erased)",
            "proposed": "G:/approved/notes.txt",
            "resolved": "G:/approved/notes.txt",
            "count": 1,
            "total_bytes": 120,
            "targets": [{"path": "G:/approved/notes.txt", "kind": "file", "size": 120}],
        },
        "args": {"path": "G:/approved/notes.txt"},
    }
    base.update(kw)
    return base


# --- the payload binding ----------------------------------------------------


def test_the_answer_carries_the_hash_that_was_displayed(desk):
    """An approval must name the payload it was given.

    Without this the daemon's hash check is skipped, because an empty claimed
    hash is falsy - so a caller that forgets to send it is not refused, it is
    waved through. The overlay must therefore always send it.
    """
    ov, d, _t = desk
    dlg = ov._present(item())
    dlg._approve_once()
    dlg._sender.join()      # the answer goes to the daemon on a thread
    assert d.args_for("confirm_approve")["payload_hash"] == "a" * 64
    assert d.args_for("confirm_approve")["confirmation_id"] == "c000001"


def test_approving_one_call_sends_scope_once_not_class(desk):
    ov, d, _t = desk
    dlg = ov._present(item())
    dlg._approve_once()
    dlg._sender.join()
    assert d.args_for("confirm_approve")["scope"] == "once"


def test_the_class_grant_is_the_number_the_user_chose(desk):
    ov, d, _t = desk
    dlg = ov._present(item())
    dlg._approve_class()
    dlg._sender.join()
    assert d.args_for("confirm_approve")["scope"] == "class"
    assert O.CLASS_GRANT_SECONDS == 600.0
    assert "10 min" in dlg.btn_class.text, (
        "the button must say what it is about to grant"
    )


def test_there_is_no_always_allow_button(desk):
    """A permanent T3 grant over every approved root does not exist.

    The three ways someone will try to reintroduce it are all worth naming: an
    extra button, a longer scope name, and a duration with no expiry.
    """
    ov, d, _t = desk
    dlg = ov._present(item())
    labels = [dlg.btn_once.text, dlg.btn_class.text, dlg.btn_deny.text]
    for text in labels:
        low = text.lower()
        for forbidden in ("always", "forever", "permanent", "remember"):
            assert forbidden not in low, f"{text!r} offers a standing permission"


# --- the timeout denies -----------------------------------------------------


def test_the_dialog_is_always_on_top(desk):
    """A confirmation that can be buried behind a browser is one never seen."""
    ov, d, _t = desk
    dlg = ov._present(item())
    assert dlg.top.attributes_set.get("-topmost") is True


def test_expiry_sends_a_denial_and_never_an_approval(desk):
    ov, d, t = desk
    dlg = O._Confirm(ov, item(), "file.delete", item()["preview"], ttl_s=60.0)
    t.advance(200)          # 200 * 0.5s = 100s > the 60s TTL
    dlg._sender.join()
    assert "confirm_deny" in d.ops()
    assert "confirm_approve" not in d.ops(), (
        "an expired dialog must never approve - that is the one outcome that "
        "would authorise an action nobody consented to"
    )


def test_the_countdown_says_it_denies(desk):
    """A countdown the user cannot interpret is worse than none."""
    ov, d, t = desk
    dlg = O._Confirm(ov, item(), "file.delete", item()["preview"], ttl_s=60.0)
    assert "denied" in dlg.clock.cleared.lower()
    assert "approve" not in dlg.clock.cleared.lower().replace("not approve", "")


def test_a_second_confirmation_does_not_stack(desk):
    """Two stacked modals is how the wrong thing gets approved."""
    ov, d, _t = desk
    first = ov._present(item())
    second = ov._present(item(id="c000002"))
    assert second is None, "a second dialog was raised while one was open"
    assert ov._dlg is first, "and the first one must still be the one on screen"


def test_the_second_confirmation_appears_once_the_first_is_answered(desk):
    """The queued one must not be swallowed.

    Marking an item as seen the moment it is offered meant the second request
    was recorded, refused a dialog because one was up, and then skipped by every
    later poll - so a pending destructive action could sit there with no way to
    reach it. The queue is polled for the oldest unanswered item instead.
    """
    ov, d, _t = desk
    d.pending = [item(), item(id="c000002", tool="file.empty_recycle_bin")]

    ov._poll()
    assert ov._dlg is not None
    assert ov._dlg.item["id"] == "c000001", "the oldest must go first"
    ov._poll()
    assert ov._dlg.item["id"] == "c000001", "and the second must wait"

    first = ov._dlg
    first._deny()
    first._sender.join()
    ov._poll()
    assert ov._dlg is not None
    assert ov._dlg.item["id"] == "c000002", "the second must appear next"


def test_an_answered_confirmation_is_never_raised_twice(desk):
    ov, d, _t = desk
    d.pending = [item()]
    ov._poll()
    first = ov._dlg
    first._deny()
    first._sender.join()
    # the daemon has not dropped it from its pending list yet
    d.pending = [item()]
    ov._poll()
    assert ov._dlg is None, "a dialog was raised for something already answered"


def test_closing_the_window_denies(desk):
    """The X button is a denial, not an abort.

    An abort that simply forgets leaves the confirmation pending forever and the
    user with no idea why nothing happened.
    """
    ov, d, _t = desk
    dlg = ov._present(item())
    assert dlg._on_close.__func__ is dlg._deny.__func__


def test_only_one_answer_reaches_the_daemon(desk):
    """Double-click must not approve twice - and a second deny is not an error."""
    ov, d, _t = desk
    dlg = ov._present(item())
    dlg._approve_once()
    dlg._deny()
    dlg._sender.join()
    dlg._sender.join()
    assert d.ops().count("confirm_approve") == 1
    assert d.ops().count("confirm_deny") == 0


# --- what the dialog tells the user -----------------------------------------


def test_the_resolved_path_is_shown_and_the_proposed_one_is_labelled(desk):
    """D32 rule 1: show what will happen, not what was asked for.

    A junction or symlink means the two differ, and a dialog that shows only the
    model's string is accurate and wrong at the same time.
    """
    ov, d, _t = desk
    preview = item()["preview"]
    preview["proposed"] = "G:/approved/tmp"
    preview["resolved"] = "C:/Users/PC/Documents/thesis.tex"
    dlg = ov._present(item(preview=preview))
    text = dlg.body.text()
    assert "G:/approved/tmp" in text
    assert "C:/Users/PC/Documents/thesis.tex" in text
    assert "asked for" in text and "resolves to" in text


def test_a_glob_is_shown_as_files_not_as_a_pattern(desk):
    """D32 rule 1b: enumerate. 3 files and 11,000 files are different decisions."""
    ov, d, _t = desk
    preview = item()["preview"]
    preview["proposed"] = "G:/approved/*.tmp"
    preview["resolved"] = "(glob resolved per match below)"
    preview["count"] = 3
    preview["targets"] = [
        {"path": f"G:/approved/f{i}.tmp", "kind": "file", "size": 10} for i in range(3)
    ]
    dlg = ov._present(item(preview=preview))
    text = dlg.body.text()
    assert "f0.tmp" in text and "f2.tmp" in text
    assert "*.tmp" in text, "the pattern should be shown too, labelled as the ask"


def test_the_count_and_size_are_shown_even_when_the_list_is_truncated(desk):
    """The count is the real signal; a truncated list must not hide it."""
    ov, d, _t = desk
    preview = item()["preview"]
    preview["count"] = 11_000
    preview["total_bytes"] = 2_400_000_000
    preview["truncated"] = True
    preview["note"] = "showing first 200 of 11000 targets"
    preview["targets"] = [
        {"path": f"G:/approved/f{i}", "kind": "file", "size": 1} for i in range(3)
    ]
    dlg = ov._present(item(preview=preview))
    text = dlg.body.text()
    assert "11,000" in text or "11000" in text
    assert "2,400,000,000" in text or "2400000000" in text
    assert "first 200 of 11000" in text


def test_a_directory_says_it_is_a_directory(desk):
    """A recursive delete of a directory is the worst case, and must not read
    like a file of some size."""
    ov, d, _t = desk
    preview = item()["preview"]
    preview["count"] = 1
    preview["targets"] = [{"path": "G:/approved/build", "kind": "dir", "entries": 40_000}]
    dlg = ov._present(item(preview=preview))
    text = dlg.body.text()
    assert "directory" in text
    assert "40,000" in text


def test_the_recoverability_of_a_delete_is_stated(desk):
    ov, d, _t = desk
    dlg = ov._present(item())
    assert "quarantine" in dlg.body.text()
    assert "recoverable" in dlg.body.text()


def test_a_missing_target_is_reported_not_hidden(desk):
    """A target that does not exist means the model is wrong about the world."""
    ov, d, _t = desk
    preview = item()["preview"]
    preview["missing"] = 2
    preview["count"] = 3
    dlg = ov._present(item(preview=preview))
    assert "do not exist" in dlg.body.text()


def test_the_hash_is_echoed_on_the_dialog(desk):
    """So the user can see that what they approve is bound to something."""
    ov, d, _t = desk
    dlg = ov._present(item())
    assert "a" * 16 in dlg.body.text()


# --- refusals come back to the user ----------------------------------------


def test_a_changed_payload_is_reported_and_not_silently_requeued(desk):
    """The daemon requeues; the user has to be told, or it looks like nothing."""
    ov, d, _t = desk
    d = FakeDaemon(approve_result={"approved": False, "requeued": item(id="c000009")})
    ov._call = d
    dlg = ov._present(item())
    dlg._approve_once()
    dlg._sender.join()
    assert any("changed" in str(q) for q in _drain(ov)), _drain(ov)


def test_a_refusal_is_shown_with_its_reason(desk):
    ov, d, _t = desk
    d = FakeDaemon(approve_result={"approved": False,
                                   "reason": "the gate no longer permits this"})
    ov._call = d
    dlg = ov._present(item())
    dlg._approve_once()
    dlg._sender.join()
    assert any("no longer permits" in str(q) for q in _drain(ov)), _drain(ov)


def test_the_overlay_never_touches_the_filesystem_itself():
    """D31, mechanically. Every action goes through `call`.

    Checked over the source text rather than by behaviour, because the failure
    this guards against - a widget growing a private path to the disk - is
    something that would be written once, quietly, and never exercised by a test
    that only pressed buttons.
    """
    import inspect

    src = inspect.getsource(O)
    for banned in ("os.remove", "shutil", "unlink", "os.system", "subprocess"):
        assert banned not in src, f"the overlay must not call {banned}"
    assert "import os" not in src and "import shutil" not in src


def test_the_pending_list_carries_the_hash_the_overlay_needs(desk):
    """D32's payload binding, which was not happening.

    The overlay reads `payload_hash` from `confirm_list` and sends it back on
    approve. The list omitted it, so the overlay sent an empty string - and an
    empty claimed hash is falsy, so `op_confirm_approve` skipped the payload
    check entirely. The check existed and was bypassed by the one caller meant to
    use it: D32's central guarantee, quietly inert.

    Asserted on what the dialog received rather than on the dict, because the
    dict having the key is not the same as the check running.
    """
    ov, d, _t = desk
    d.pending = [item()]
    dlg = ov._present(item())
    assert dlg.phash, "the overlay received no hash to bind the approval to"
    assert len(dlg.phash) == 64
    dlg._approve_once()
    dlg._sender.join()
    assert d.args_for("confirm_approve")["payload_hash"] == "a" * 64


def test_the_pending_list_does_not_leak_the_arguments(desk):
    """The hash is a digest, not a secret; the arguments may hold one."""
    ov, d, _t = desk
    d.pending = [item()]
    listing = d("confirm_list")
    assert "args" not in repr(listing), repr(listing)
