"""`python -m assistant` is the product's front door.

Two things are asserted here, and neither is the GUI: that the module exists and
is wired, and that the one mode which must work without a display works. A
launcher that cannot start headless is a launcher nobody can check, which is how
`tray.py` and `overlay.py` ended up complete, tested, and unreachable.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from assistant import launcher

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_check_mode_needs_no_display_and_exits_zero(tmp_path: Path) -> None:
    """The preflight is the mode that has to work with nothing attached.

    It is what a user runs to ask "is this thing healthy?", so it must not
    require a tray, a chord, or an interactive session - and it must not leave a
    credential on disk, because nothing was started.
    """
    state = tmp_path / "state"
    code = launcher.main(["--state", str(state), "--check"])

    assert code == 0
    assert not (state / "authkey.bin").exists(), (
        "a check must not claim to be a running daemon"
    )


def test_check_mode_reports_the_measured_facts(capsys) -> None:
    """It has to say what it found, not merely that it found nothing wrong."""
    assert launcher.main(["--check"]) == 0
    out = capsys.readouterr().out
    for key in ("audit_chain", "no_listening_ports", "vram", "version"):
        assert key in out, f"preflight must report {key}"


def test_an_unknown_flag_is_refused() -> None:
    """Fail loudly on a typo rather than starting a daemon that ignores it."""
    with pytest.raises(SystemExit):
        launcher.main(["--definitely-not-a-flag"])


def test_the_package_is_runnable_as_a_module() -> None:
    """`python -m assistant --check` in a real subprocess.

    The in-process tests prove the function works; this proves the *entry point*
    is wired, which is the thing a user types and the thing that was missing.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "assistant", "--check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        env={**_env(), "PYTHONPATH": str(REPO_ROOT / "src")},
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "audit_chain" in proc.stdout


def _env() -> dict:
    import os

    return dict(os.environ)
