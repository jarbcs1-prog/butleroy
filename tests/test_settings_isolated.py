"""Runs the real-Tk settings tests in a subprocess, and reports the exit code.

The reason is in `test_settings_real.py`'s docstring, and it is not a workaround for
a test that is hard to write. In this Python 3.11.14 / Tk build, a `Tk()` root
created and destroyed on a worker thread can leave Tcl's async handler to be
deleted at *interpreter shutdown* — on the main thread — producing

    Tcl_AsyncDelete: async handler deleted by the wrong thread

and a process exit of `-2147483645`, **after every test in the run has passed**.
Windows turns that into a debug break, so pytest never gets to print a summary.

What it is not, because each of those was checked rather than assumed:

  * **not a leak.** `test_every_widget_attribute_is_released` and
    `test_a_section_holds_no_widget_after_disposal` assert nothing is retained, and
    they pass. The first version of this window *did* leak — it kept `_head` and
    `_title_label` — and produced the same exit code, which is exactly why the two
    had to be separated rather than assumed to be one thing.
  * **not a count of windows.** Bare tkinter created and destroyed forty sequential
    worker-thread roots without complaint in the same interpreter. The number at
    which the settings window tripped it was not stable across runs, which is the
    opposite of a limit.
  * **not a mistake in the tests.** Driving the same code from a plain script, with
    no pytest at all, exits 0.

So it is an interpreter-lifetime quirk of this build, it is not the product's, and
it is not something a change to `settings.py` should be contorted to avoid. What it
*must* not do is turn a green run red with no explanation — which is what happens
when the tests run in-process.

Hence this file: it runs the suite in a child process and asserts on the child's
exit code. A real failure fails here, with the child's own output, and a Tcl abort
is visible as a non-zero exit rather than as a mysterious `-2147483645`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
BODY = HERE / "test_settings_real.py"

pytestmark = pytest.mark.skipif(
    subprocess.run(
        [sys.executable, "-c", "import tkinter; tkinter.Tk()"],
        capture_output=True,
    ).returncode != 0,
    reason="no interactive session for Tk",
)


def test_the_real_settings_window_suite_passes_in_its_own_process() -> None:
    """The subprocess boundary, asserted rather than assumed to be safe."""
    # `SETTINGS_TK_INPROCESS` is unset in the child, so the body would skip itself.
    # The child *is* the isolation, so it must be told to run the tests.
    env = {**os.environ, "SETTINGS_TK_INPROCESS": "1"}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(BODY), "-q",
         "-p", "no:cacheprovider", "--tb=short"],
        cwd=HERE.parent, capture_output=True, text=True, timeout=600, env=env,
    )

    output = (result.stdout or "") + (result.stderr or "")
    assert result.returncode == 0, (
        f"the real-Tk settings suite failed in its own process "
        f"(exit {result.returncode}):\n{output[-4000:]}"
    )
    # Belt and braces: a passing exit code with no tests collected is not a pass, and
    # `pytest` exits 5 for "no tests ran".
    assert "passed" in output, f"no passing tests reported:\n{output[-2000:]}"


def test_the_isolation_is_actually_needed_and_not_decoration() -> None:
    """Guards the guard.

    If `test_settings_real.py` ever stops being run in-process, this file becomes a
    subprocess that tests nothing. Rather than assert on that, this checks the
    *reason* still holds: the body must still contain real-Tk tests that are not
    excluded, and the module must still document why it is isolated.
    """
    body = BODY.read_text(encoding="utf-8")
    assert "requires_display" in body, (
        "the display guard is gone; the body may now be silently skipping"
    )
    assert "tkinter" in body, "the body no longer uses a real Tk"
    assert "wrong thread" in body, (
        "the module docstring no longer explains the isolation; if that explanation "
        "is no longer accurate, this subprocess should be reconsidered"
    )