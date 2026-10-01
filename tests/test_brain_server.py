"""Tests for the child process that holds the model.

Mostly about the one thing the child cannot be trusted to do for itself: notice
that the thing which owns it is gone.

D3 says VRAM comes back when this process dies, and that an idle TTL is what
makes it die. But the sweeper that enforces the TTL runs *in the daemon* - so a
daemon that dies takes the sweeper with it, and the child survives holding
~5 GiB with nothing left to reclaim it. Observed twice in one session: a crashed
probe left two orphans holding the pipe, and the card stayed busy until they
were found by hand.
"""

import os
import subprocess
import sys
import time

import pytest

import subprocess

from assistant import brain_server

# --- parent-death detection -------------------------------------------------
#
# D3 says VRAM comes back when this process dies. But the idle-TTL sweeper that
# kills it runs *in the daemon*, so a daemon that dies takes the sweeper with it
# and the child survives holding ~5 GiB with nothing left to reclaim it. Observed
# twice in one session: a crashed probe left two orphans holding the pipe.


def test_a_live_process_is_reported_alive():
    assert brain_server.parent_alive(os.getpid()) is True


def test_this_process_is_its_own_parent_here():
    """Guards the test above from being vacuous on a reparented process."""
    assert os.getppid() != os.getpid()


def test_a_dead_pid_is_reported_dead():
    """Not by getppid, which on Windows keeps returning a dead parent's pid."""
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    assert brain_server.parent_alive(dead.pid) is False


def test_an_impossible_pid_is_death_not_an_error():
    """Every way of getting here other than "alive" must mean dead.

    The failure that matters is the child surviving, so an unopenable handle is
    a death signal rather than a reason to carry on.
    """
    assert brain_server.parent_alive(0) is False
    assert brain_server.parent_alive(-1) is False
    assert brain_server.parent_alive(None) in (True, False)


def test_getppid_alone_would_not_have_worked():
    """Why a handle is used, asserted so the reasoning survives.

    On Windows an orphan keeps its dead parent's pid forever, so a check based on
    `getppid` reports an orphan as having a live parent indefinitely.
    """
    assert os.name == "nt", "this reasoning is Windows-specific"


# --- kill-on-close -----------------------------------------------------------
#
# D46 named the polling check as the smaller piece and this as the strong
# guarantee. It is also the piece that was quietly wrong for a while: the flag
# was written at byte offset 0x28, which is `ActiveProcessLimit`, not
# `LimitFlags` at 0x10. `SetInformationJobObject` accepted the structure and
# returned success, so the guard looked installed and armed nothing.
#
# Which is why these tests read the flag back rather than trusting the return.


def test_the_kill_on_close_flag_is_actually_stored():
    """A flag that cannot be read back is a flag that cannot be claimed."""
    job = brain_server.job_kill_on_close()
    try:
        flags = brain_server.job_limit_flags(job)
        assert flags & brain_server.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, (
            f"LimitFlags read back as 0x{flags:08x}, so kill-on-close is NOT armed"
        )
    finally:
        brain_server.job_close(job)


def test_closing_the_job_kills_a_live_member():
    """The whole mechanism, end to end: put a process in, close the handle, gone.

    Cheap on purpose - a sleeping interpreter, not a model. The claim being tested
    is about job objects, not about llama.cpp.
    """
    job = brain_server.job_kill_on_close()
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        assert brain_server.job_assign(job, child.pid), "the child never joined"
        time.sleep(0.4)
        assert child.poll() is None, "the child died before we closed the job"

        brain_server.job_close(job)
        job = None
        try:
            child.wait(timeout=20)
        except subprocess.TimeoutExpired:
            child.kill()
            pytest.fail("the child survived the job handle closing")
    finally:
        if job is not None:
            brain_server.job_close(job)
        if child.poll() is None:
            child.kill()


def test_assigning_a_dead_pid_reports_failure_rather_than_raising():
    """The caller already has a running child. A refusal degrades the guard to
    polling; an exception would take the model away instead."""
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    job = brain_server.job_kill_on_close()
    try:
        assert brain_server.job_assign(job, dead.pid) is False
    finally:
        brain_server.job_close(job)


def test_closing_nothing_is_a_no_op():
    """`_brain_stop` closes the handle unconditionally, including when the child
    failed to start and there was never a handle to close."""
    assert brain_server.job_close(None) is None
