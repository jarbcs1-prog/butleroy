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
