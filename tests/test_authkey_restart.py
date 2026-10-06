"""The daemon must survive being started more than once (D64).

Before this, `Daemon.__init__` minted a fresh authkey on every construction and
`store_authkey` opened the file with `os.O_EXCL`, and nothing ever removed the
file. So the *second* start on a state directory raised `FileExistsError` and the
daemon died on startup. The project is specified as a 24/7 resident service, so
every restart, crash or reboot took it down permanently until a human deleted
`state/authkey.bin` by hand.

This is D61's shape exactly. The `O_EXCL` was a correct decision protecting a
real thing - never silently overwrite a key a *live* daemon is using - and the
missing half of that decision is what broke it. 1025 tests passed with the bug
present, because no test ever started two daemons against one state directory.

The fix splits the two questions `O_EXCL` was conflating:

* *May I write this file?* is answered by **binding the pipe**, which the OS
  makes atomic and which fails with ``PermissionError`` on rebind. That is the
  real proof that no other daemon owns this state directory.
* *Is there a key on disk?* is then just a fact to read. A key left by a daemon
  that is gone is ours to reuse, and reusing it is what keeps a restart
  seamless: anything that read the key before the restart can still
  authenticate afterwards.

So ``acquire_authkey`` still refuses by default, and only adopts when the caller
has proven ownership by holding the pipe. The original protective default is
kept; it is simply no longer the thing that decides whether a restart works.

Two seams are exercised, and they are the two the production path touches:
``ipc.acquire_authkey`` (the storage decision) and ``Daemon.start_serving`` /
``shutdown`` (the lifecycle, against a real pipe and a real state dir).

Nothing here is mocked. A mock of the key file would have passed against the
broken implementation, which is the entire reason for recording it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant import ipc
from assistant.daemon import Daemon

# Named pipes vanish when the last handle closes, but a crashed run can leave one
# behind briefly, so every test binds a name nothing else will ask for.
_COUNTER = iter(range(1, 10_000))


def unique_pipe(tag: str) -> str:
    return rf"\\.\pipe\locala.test.restart.{tag}.{next(_COUNTER)}"


# --------------------------------------------------------------------------- #
# seam 1: the storage decision
# --------------------------------------------------------------------------- #


def test_a_key_is_minted_when_none_exists(tmp_path: Path) -> None:
    """First ever start on a clean state dir: write a key and say so."""
    path = tmp_path / "authkey.bin"

    key, minted = ipc.acquire_authkey(path)

    assert minted is True
    assert len(key) == 32
    assert ipc.load_authkey(path) == key


def test_an_unproven_caller_may_not_write_over_an_existing_key(tmp_path: Path) -> None:
    """The original `O_EXCL` promise, kept as the default.

    A caller that has not proven it owns this state directory gets the old
    behaviour: refuse. This is the property `test_authkey_is_never_silently_
    overwritten` in test_ipc.py protects, expressed through the new entry point.
    """
    path = tmp_path / "authkey.bin"
    incumbent = ipc.generate_authkey()
    ipc.store_authkey(path, incumbent)

    with pytest.raises(FileExistsError):
        ipc.acquire_authkey(path)

    assert ipc.load_authkey(path) == incumbent, "the incumbent key must survive"


def test_the_owner_adopts_an_existing_key_rather_than_replacing_it(tmp_path: Path) -> None:
    """The restart case, at the storage seam.

    Reusing the key rather than minting a new one is what makes a restart
    seamless: anything that read the key before the restart can still
    authenticate afterwards. Rotating it on every start would silently break
    exactly those callers - the same class of defect as D38's grammar bug, where
    the new value works for the writer and fails for the reader.
    """
    path = tmp_path / "authkey.bin"
    first = ipc.generate_authkey()
    ipc.store_authkey(path, first)

    key, minted = ipc.acquire_authkey(path, owned=True)

    assert minted is False, "an existing key must be adopted, not replaced"
    assert key == first


def test_the_owner_mints_when_the_state_dir_is_clean(tmp_path: Path) -> None:
    """Holding the pipe makes no difference on a clean dir: normal first start."""
    path = tmp_path / "authkey.bin"

    key, minted = ipc.acquire_authkey(path, owned=True)

    assert minted is True
    assert ipc.load_authkey(path) == key


def test_a_corrupt_key_is_refused_rather_than_silently_replaced(tmp_path: Path) -> None:
    """Even for the owner. A corrupt key means something is wrong.

    Quietly minting over it would hide that, and the symptom would surface much
    later as an unauthenticated connection to a daemon nobody knows is
    misconfigured.
    """
    path = tmp_path / "authkey.bin"
    path.write_bytes(b"corrupt")

    with pytest.raises(ipc.ProtocolError):
        ipc.acquire_authkey(path, owned=True)


# --------------------------------------------------------------------------- #
# seam 2: the lifecycle, against a real daemon and a real pipe
# --------------------------------------------------------------------------- #


def test_a_daemon_is_stopped_and_started_again(tmp_path: Path) -> None:
    """The regression test for D64, at the seam the bug actually lived at.

    Start, serve, shut down, then serve again on the same state directory. This
    is what happens every time the machine reboots, and before the fix the
    second start raised ``FileExistsError``.

    The second daemon gets a *different* key, and that is correct rather than a
    regression: a clean shutdown removes the credential, so there is nothing to
    adopt and a fresh one is minted. Key continuity is only owed across a crash,
    which is the test below.
    """
    state = tmp_path / "state"

    first = Daemon(state=state)
    first.start_serving(unique_pipe("cycle-a"))
    assert ipc.load_authkey(state / "authkey.bin") == first.authkey
    first.shutdown()

    second = Daemon(state=state)
    second.start_serving(unique_pipe("cycle-b"))
    try:
        assert ipc.load_authkey(state / "authkey.bin") == second.authkey
        assert second.authkey != first.authkey, (
            "a clean shutdown leaves no key to reuse, so the restart mints a "
            "fresh one"
        )
    finally:
        second.shutdown()


def test_restarting_does_not_corrupt_the_audit_chain(tmp_path: Path) -> None:
    """A refused start must leave the state directory byte-identical.

    Found while writing D64: `start_serving` appended its ``daemon.start`` audit
    record *before* binding the pipe. A second daemon contending for the same
    state directory therefore appended a record to the shared hash chain and
    then raised on the bind. Its record committed to the losing daemon's
    in-memory chain state, so the incumbent's chain no longer verified - and the
    incumbent could not restart either. One refused daemon turned a working
    service into a permanently broken one.

    The ordering that prevents it is prove-ownership-then-write.
    """
    state = tmp_path / "state"
    pipe = unique_pipe("chain")

    first = Daemon(state=state)
    first.start_serving(pipe)
    try:
        before = (state / "audit.jsonl").read_bytes()

        second = Daemon(state=state)
        with pytest.raises(OSError):
            second.start_serving(pipe)

        assert (state / "audit.jsonl").read_bytes() == before, (
            "a refused daemon must not have written to the audit chain"
        )
    finally:
        first.shutdown()

    # And the incumbent is still verifiable afterwards.
    assert first.audit.verify().ok


def test_a_crash_without_shutdown_still_recovers(tmp_path: Path) -> None:
    """The hard case: no ``shutdown()`` runs, so the key is left behind.

    This is what a power cut or a ``TerminateProcess`` looks like. The next start
    must still succeed, which only works because a stale key is adopted rather
    than treated as an error.
    """
    state = tmp_path / "state"

    first = Daemon(state=state)
    first.start_serving(unique_pipe("crash-a"))
    crashed_key = first.authkey
    first.server.stop()  # the pipe dies; no shutdown(), no key removal
    assert (state / "authkey.bin").exists(), "a crash leaves the key behind"

    second = Daemon(state=state)
    second.start_serving(unique_pipe("crash-b"))
    try:
        assert second.authkey == crashed_key
    finally:
        second.shutdown()


def test_two_daemons_cannot_share_a_state_directory(tmp_path: Path) -> None:
    """The ``O_EXCL`` promise, end to end, and the ordering that carries it.

    Two daemons on one machine is a bug we want to hear about loudly, and that
    intent must not be traded away to make restarts work. The pipe is bound
    *before* the key is claimed, so the second daemon is refused by the OS and
    the incumbent's key is never even considered.
    """
    state = tmp_path / "state"
    pipe = unique_pipe("contended")

    first = Daemon(state=state)
    first.start_serving(pipe)
    key_after_first = ipc.load_authkey(state / "authkey.bin")
    try:
        second = Daemon(state=state)
        with pytest.raises(OSError):
            second.start_serving(pipe)

        assert ipc.load_authkey(state / "authkey.bin") == key_after_first
    finally:
        first.shutdown()

    # The state directory is still usable afterwards, which is the part that
    # would silently rot if the refused daemon had half-written the key. A fresh
    # key here is correct: the incumbent shut down cleanly, which removes the
    # credential by design.
    third = Daemon(state=state)
    third.start_serving(unique_pipe("contended-after"))
    try:
        assert ipc.load_authkey(state / "authkey.bin") == third.authkey
    finally:
        third.shutdown()


def test_a_clean_shutdown_leaves_no_credential_on_disk(tmp_path: Path) -> None:
    """A stopped daemon leaves nothing behind that looks like a running one.

    Not only hygiene: a key file that outlives the process it belongs to is the
    reason a stale-key decision was needed at all. Removing it keeps the
    invariant honest - *a key on disk means a daemon is running.*
    """
    state = tmp_path / "state"
    d = Daemon(state=state)
    d.start_serving(unique_pipe("cleanup"))
    assert (state / "authkey.bin").exists()

    d.shutdown()

    assert not (state / "authkey.bin").exists()


def test_a_daemon_that_never_served_removes_nothing(tmp_path: Path) -> None:
    """Shutdown must not delete a key it did not write.

    A daemon built for ``--check`` or for a test never bound the pipe, so it
    never owned the file. Removing it anyway would destroy another daemon's
    credential from a process that merely happens to share the state directory.
    """
    state = tmp_path / "state"
    state.mkdir(parents=True)
    foreign = ipc.generate_authkey()
    ipc.store_authkey(state / "authkey.bin", foreign)

    Daemon(state=state).shutdown()

    assert ipc.load_authkey(state / "authkey.bin") == foreign
