"""A confirmation dialog, built on a REAL Tk - because 24 tests said it was fine.

D69's defect was a seven-digit hex colour on one button's hover state.
`tk.Button(...)` raised `TclError: invalid color name "#3182ce6"` while
constructing the dialog, and every T3 confirmation in the product died on that
line. The whole suite was green, because `test_overlay.py`'s `NoButton` ignored
every colour it was handed - 24 tests on the confirmation rules, including the
payload binding and the expiry-denies behaviour, passing against a dialog that
cannot be built at all.

So this file exists for one narrow reason: **to construct the real thing.** The
fakes in the other file are still the right tool for "what does the dialog say",
and this is not a replacement for them. It is the check they structurally cannot
perform, and the whole point is that it needs no assertions about wording - it
fails by raising.

Every widget option `_Confirm` passes is checked against a real Tk: colours,
fonts, reliefs, padding. A typo in any of them is a TclError at construction, and
a construction failure is silent in production because `_poll` calls `_present`
from an `after` callback, where a Tk exception is printed and swallowed - leaving
a pending destructive action with no way to reach it. That is D67 defect 2's
shape exactly, one layer over: the safe path quietly stops existing.

Skipped without a display, because tkinter needs one and CI does not have one.
A skipped test here is a skipped test; on this machine it runs, and that is the
only place the claim is checked.
"""

from __future__ import annotations

import pytest

from assistant import overlay as O

pytestmark = pytest.mark.skipif(
    not O.tk, reason="tkinter unavailable"
)


def _can_display() -> bool:
    """Can a Tk root actually be created here?

    Probed rather than assumed, so an unavailable display skips instead of erroring
    halfway through a test that has already built half a dialog.
    """
    try:
        root = O.tk.Tk()
    except Exception:  # noqa: BLE001 - any failure means no display
        return False
    root.destroy()
    return True


pytestmark = [
    pytest.mark.skipif(not _can_display(), reason="no display available"),
]


ITEM = {
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


@pytest.fixture
def live_overlay():
    """A real Overlay with a real Tk root and no daemon behind it."""
    ov = O.Overlay(lambda op, **a: {"ok": True, "result": {
        "pending": [], "grants": {}}})
    ov.show()
    yield ov
    ov.destroy()


def test_the_confirmation_dialog_can_actually_be_built(live_overlay):
    """D69, restated as a test.

    One line, and it is the whole defect: `_Confirm(...)` must not raise. The
    assertion is the absence of a TclError, because that is what the bug produced -
    there was no wrong colour to assert *about*, only a dialog that never appeared.
    """
    dlg = live_overlay._present(dict(ITEM))
    assert dlg is not None, "the dialog refused to be presented at all"
    assert dlg.cid == "c000001"
    # And it is on screen, which is the part that matters for a human decision.
    assert live_overlay._root is not None
    dlg._deny()
    dlg._sender.join(timeout=10)


def test_the_poll_raises_a_pending_confirmation_on_its_own():
    """Nobody calls `_poll` or `show` here. The armed 1s job must do it.

    D67 defect 2 was `_poll` running while `_root` was still None, so the one
    second poll that makes a destructive action reachable had never once been
    armed. Every test of `_poll` drove it by hand, which is the test supplying the
    behaviour production does not - so this one waits and watches.
    """
    seen = []

    def call(op, **a):
        if op == "confirm_list":
            return {"ok": True, "result": {"pending": [ITEM], "grants": {}}}
        return {"ok": True, "result": {}}

    ov = O.Overlay(call)
    ov.show()
    try:
        import time
        deadline = time.time() + 10
        while time.time() < deadline:
            box = {}
            ov._invoke(lambda: box.__setitem__("d", ov._dlg))
            if box.get("d") is not None:
                seen.append(box["d"])
                break
            time.sleep(0.2)
        assert seen, (
            "nothing raised a pending confirmation with nobody asking for one - "
            "the user would be left holding a pending deletion and no way to reach it"
        )
        seen[0]._deny()
        seen[0]._sender.join(timeout=10)
    finally:
        ov.destroy()


def test_the_dialog_stays_topmost(live_overlay):
    """A destructive approval that can be buried is an approval nobody sees.

    Cheap to lose: D67 took `-topmost` off the *prompt* window to stop it covering
    the desktop, and it would have been easy to take it off the dialog in the same
    sweep. It is asserted here against a real Tk because `attributes()` on a fake
    is a dict update and proves nothing about whether the flag was ever honoured.
    """
    dlg = live_overlay._present(dict(ITEM))
    assert dlg.top.attributes("-topmost") in (1, True, "1"), (
        "the T3 dialog lost -topmost; a buried approval is an approval nobody sees"
    )
    dlg._deny()
    dlg._sender.join(timeout=10)


def test_a_dialog_is_presented_on_the_overlays_own_window(live_overlay):
    """Transient against the prompt window, so it cannot outlive or orphan it."""
    dlg = live_overlay._present(dict(ITEM))
    # `transient()` with no argument returns the master's path as a string.
    master = str(dlg.top.transient())
    assert master == str(live_overlay._root), (
        f"the dialog is transient for {master!r}, not the prompt window"
    )
    dlg._deny()
    dlg._sender.join(timeout=10)


def test_a_settled_dialog_drops_its_widgets_on_the_tk_thread():
    """D67's exit-code bug, one level down: a `_Confirm` held six Tk widgets.

    `top`, `body`, `clock` and three buttons are widget objects, each holds
    `.tk`, and that is the Tcl interpreter. D67 fixed this for the Overlay and
    left it in place for the dialog, so a *settled* dialog kept the interpreter
    alive through attributes on an object the caller may still be holding.

    The failure is at process exit and it is not visible from Python:

        Tcl_AsyncDelete: async handler deleted by the wrong thread

    which on Windows raises a debug break, so the daemon exits -2147483645 after
    a clean shutdown and a scheduled task records that as failure.

    Asserted on the *attributes* rather than on the exit code, because the exit
    code is a property of the process and this is a property of the object. The
    exit-code form is verified by running the suite - this file did not exist, and
    adding it made pytest itself exit -2147483645, which is the measurement.
    """
    def call(op, **a):
        if op == "confirm_list":
            return {"ok": True, "result": {"pending": [ITEM], "grants": {}}}
        return {"ok": True, "result": {}}

    ov = O.Overlay(call)
    ov.show()
    try:
        import time
        deadline = time.time() + 10
        dlg = None
        while time.time() < deadline and dlg is None:
            box = {}
            ov._invoke(lambda: box.__setitem__("d", ov._dlg))
            dlg = box.get("d")
            time.sleep(0.2)
        assert dlg is not None, "no dialog appeared, so there is nothing to settle"

        held = [
            name for name in ("top", "body", "clock", "btn_once", "btn_class",
                              "btn_deny")
            if getattr(dlg, name, None) is not None
        ]
        assert held, "the dialog was built with no widgets, which is not what happened"

        ov._invoke(dlg._deny)
        dlg._sender.join(timeout=10)

        still = [name for name in held if getattr(dlg, name, None) is not None]
        assert not still, (
            f"a settled dialog still holds {still}; the Tcl interpreter stays "
            "alive through them and is released at process shutdown on the wrong "
            "thread, which exits -2147483645"
        )
    finally:
        ov.destroy()