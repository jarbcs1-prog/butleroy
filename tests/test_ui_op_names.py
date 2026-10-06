"""Every op a UI calls must be an op that exists. D77.

This file exists because **four of the tray's six menu items had never worked.**

`tray.py` called `arbiter.summary`, `audit.verify`, `session.set_attended` and
`workspace.list`. The daemon's ops are named after the methods with `op_` stripped,
so the real names are `arbiter_summary`, `audit_verify`, `session_set_attended` and
`workspace_list`. Every one of those menu items returned

    unknown op 'workspace.list'

and the tray's own `notify` swallowed it into a toast saying "unknown op ...".

**Why no test caught it, which is the part worth keeping.** `test_tray.py` drives the
menu against a fake daemon, and a fake that answers any string answers the wrong
string too. The failure was not in the code the tests replaced - it was in the *name*,
and a name is exactly what a fake does not check. This is the same sentence as D69
("a fake agrees with the code about everything it does not check") and D70 ("the fake
did its own LOGIN"), one level up: the fake did the op lookup.

So the check here is not against a fake. It is against `Daemon._OPS`, the real table,
which is what the daemon actually dispatches on.

**The generalisable rule**: any string a caller sends that a *different* module
resolves should be asserted against that module's own table, not against a stand-in.
The stand-in cannot know.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from assistant.daemon import Daemon

ROOT = Path(__file__).resolve().parent.parent
UI_FILES = ("tray.py", "overlay.py", "settings.py", "launcher.py", "voice_loop.py")

#: Every way a UI module can name an op.
#:
#: Two patterns, not one, and the second was added after the first missed a real
#: defect: `tray.py`'s `Presence` builds `ipc.Request(op="session.set_attended", ...)`
#: directly rather than going through `Tray.call`, so a scan for `.call("...")` alone
#: reported the file clean while a fifth broken name sat in it. The suite caught it -
#: `test_presence_marks_attended_when_someone_is_typing` asserted the op it had
#: recorded - but this file is supposed to catch that class, and it did not.
_CALL = re.compile(r'\.?call\(\s*"([a-z][a-z0-9_.]*)"')
_REQUEST = re.compile(r'op\s*=\s*"([a-z][a-z0-9_.]*)"')

REAL_OPS = frozenset(Daemon._OPS)


def _called_ops(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    return set(_CALL.findall(text)) | set(_REQUEST.findall(text))


@pytest.mark.parametrize("name", UI_FILES)
def test_every_op_a_ui_module_calls_actually_exists(name: str) -> None:
    """The check that would have caught four dead menu items.

    Deliberately reports *all* the offenders rather than the first, because these
    arrive in batches - four were wrong at once, and a test that stops at the first
    turns one fix into four round trips.
    """
    path = ROOT / "src" / "assistant" / name
    if not path.is_file():
        pytest.skip(f"{name} is not present")

    called = _called_ops(path)
    # Not every string in a `call(...)` is an op: the settings window's fake in tests
    # is not here, but `call` is also used for a couple of non-op helpers. Only
    # strings that *look like* op names are judged, which is what makes this a check
    # rather than a guessing game.
    suspicious = {
        op for op in called
        if op not in REAL_OPS and "_" in op.replace(".", "_")
    }
    offenders = sorted(suspicious)
    assert not offenders, (
        f"{name} calls ops that do not exist: {offenders}\n"
        f"  real ops are: {sorted(REAL_OPS)}\n"
        f"  a dotted name is almost always a mistake - ops are named after the "
        f"methods with 'op_' stripped, so it is `workspace_list`, not "
        f"`workspace.list`"
    )


def test_the_check_would_notice_a_wrong_name() -> None:
    """Guards the guard, by feeding it the exact bug that prompted this file.

    Without this, the test above passes trivially if `_CALL` stops matching anything -
    and a regex that matches nothing is the failure mode this session has now hit
    three times. Asserted on the regex, not on the files, so it cannot be satisfied by
    the files happening to be correct.
    """
    sample = ('res = self.call("workspace.list")\n'
              'res = self._call("audit.verify")\n'
              'req = ipc.Request(op="session.set_attended", args={}, id=1)\n')
    found = set(_CALL.findall(sample)) | set(_REQUEST.findall(sample))

    assert found == {"workspace.list", "audit.verify", "session.set_attended"}, (
        f"the extractor found {found}; it no longer sees the calls it is for"
    )
    assert not (found & REAL_OPS), (
        "the sample names are real ops, so this guard proves nothing"
    )


def test_the_ui_files_are_the_ones_we_think() -> None:
    """A renamed module would silently drop out of the parametrised check.

    Only the three modules that are *expected* to call ops directly. `launcher.py`
    and `voice_loop.py` are in the check above so that a call appearing in them is
    validated, but they reach the daemon through `Tray` rather than calling ops
    themselves - so requiring them to contain op calls would be asserting something
    false about how they are built. The first version did, and failed on `launcher.py`
    for that reason.
    """
    for name in ("tray.py", "overlay.py", "settings.py"):
        path = ROOT / "src" / "assistant" / name
        assert path.is_file(), f"{name} is missing"
        assert _called_ops(path), (
            f"{name} calls no ops at all, which cannot be right for a UI module "
            f"- and it means the check above is vacuous for it"
        )


def test_every_op_has_a_handler_that_is_callable() -> None:
    """The other direction: an op in the table must be a real method.

    `_install_tables` builds the table from `dir(Daemon)`, so this cannot currently
    fail - which is the point of asserting it. It is the invariant that makes the
    table trustworthy, and it is one refactor away from not holding.
    """
    for op, handler in sorted(Daemon._OPS.items()):
        assert callable(handler), f"op {op!r} is not callable"
        assert hasattr(handler, "__name__"), f"op {op!r} has no name"