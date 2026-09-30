"""Tests for the VRAM arbiter.

The two tests that matter most are
``test_user_workload_is_never_terminated`` and
``test_fast_tier_is_preempted_last`` - together they encode the promise that
the assistant will never cost the user their machine.
"""

from __future__ import annotations

import pytest

from assistant.arbiter import (
    GIB,
    DEFAULT_RESERVE,
    Denial,
    Lease,
    Priority,
    VramArbiter,
    VramSnapshot,
    VramUnavailable,
    as_denial,
    read_vram,
    total_leased,
)

FAST = int(4.17 * GIB)
DEEP = int(5.96 * GIB)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeGpu:
    """A GPU that behaves like one: killing a process returns its memory.

    If the fake did not give the bytes back, a preemption loop could never
    succeed and the tests would be measuring a fiction. Modelling the return is
    the whole trick.
    """

    def __init__(self, total: int = int(12 * GIB), free: int = int(11 * GIB)) -> None:
        self.total = total
        self.free = free
        self.held: dict[str, int] = {}
        self.killed: list[str] = []

    def snapshot(self) -> VramSnapshot:
        return VramSnapshot(total=self.total, used=self.total - self.free, free=self.free)

    def terminator(self, holder: str) -> None:
        self.killed.append(holder)
        self.free = min(self.total, self.free + self.held.pop(holder, 0))

    def rent(self, holder: str, nbytes: int) -> None:
        self.held[holder] = nbytes
        self.free -= nbytes

    def user_starts_a_game(self, gigabytes: float) -> None:
        self.free -= int(gigabytes * GIB)
        self.held.pop("user", None)


@pytest.fixture()
def rig() -> tuple[FakeGpu, FakeClock, VramArbiter]:
    gpu, clock = FakeGpu(), FakeClock()
    arbiter = VramArbiter(snapshot_fn=gpu.snapshot, terminator=gpu.terminator, clock=clock)
    return gpu, clock, arbiter


def test_acquire_grants_when_there_is_room(rig) -> None:
    _, _, arbiter = rig
    lease = arbiter.acquire("fast", FAST, Priority.FAST_TIER)
    assert isinstance(lease, Lease)
    assert lease.need_bytes == FAST
    assert lease.priority is Priority.FAST_TIER
    assert total_leased(arbiter) == FAST


def test_reserve_is_enforced_to_the_byte(rig) -> None:
    gpu, _, arbiter = rig
    headroom = gpu.free - arbiter.reserve_bytes
    assert arbiter.acquire("exact", headroom, Priority.WORKER)  # exactly fits
    arbiter.release("exact")
    gpu.free = int(11 * GIB)
    denial = as_denial(arbiter.acquire("one_over", headroom + 1, Priority.WORKER))
    assert denial is not None
    assert "DENIED one_over" in str(denial)
    # The message must be actionable: it says how much is reserved and for whom.
    assert "reserved for you" in str(denial)


def test_default_reserve_is_one_and_a_half_gib() -> None:
    assert DEFAULT_RESERVE == int(1.5 * GIB)


def test_ttl_expiry_reaps_and_kills(rig) -> None:
    gpu, clock, arbiter = rig
    assert isinstance(arbiter.acquire("deep", DEEP, Priority.DEEP_TIER), Lease)
    gpu.rent("deep", DEEP)

    assert arbiter.sweep() == []  # not yet due
    clock.advance(89.0)
    assert arbiter.sweep() == []
    clock.advance(2.0)
    assert arbiter.sweep() == ["deep"]
    assert gpu.killed == ["deep"]
    assert arbiter.leases == []


def test_renew_extends_without_rechecking_capacity(rig) -> None:
    gpu, clock, arbiter = rig
    assert isinstance(arbiter.acquire("fast", FAST, Priority.FAST_TIER), Lease)
    # The user starts something big *after* we were granted the lease.
    gpu.user_starts_a_game(6.0)
    clock.advance(50.0)
    renewed = arbiter.renew("fast")
    assert isinstance(renewed, Lease)
    assert renewed.renewals == 1
    assert renewed.expires_at == clock.now + arbiter.ttl_seconds
    # Nothing was killed: a lease cannot be revoked by a foreign workload
    # appearing, or work would die mid-answer.
    assert gpu.killed == []


def test_reacquire_is_a_renewal_not_a_second_lease(rig) -> None:
    _, clock, arbiter = rig
    arbiter.acquire("fast", FAST, Priority.FAST_TIER)
    clock.advance(30.0)
    again = arbiter.acquire("fast", FAST, Priority.FAST_TIER)
    assert isinstance(again, Lease)
    assert again.renewals == 1
    assert len(arbiter.leases) == 1
    assert total_leased(arbiter) == FAST


def test_renew_without_a_lease_is_a_denial(rig) -> None:
    _, _, arbiter = rig
    denial = as_denial(arbiter.renew("ghost"))
    assert denial is not None
    assert "no live lease" in denial.reason


def test_preemption_sacrifices_the_least_important_first(rig) -> None:
    gpu, _, arbiter = rig
    assert isinstance(arbiter.acquire("deep", DEEP, Priority.DEEP_TIER), Lease)
    assert isinstance(arbiter.acquire("whisper", int(0.3 * GIB), Priority.WHISPER), Lease)
    gpu.rent("deep", DEEP)
    gpu.rent("whisper", int(0.3 * GIB))

    result = arbiter.acquire("worker", int(8.0 * GIB), Priority.WORKER)
    assert isinstance(result, Lease)
    # Whisper (priority 5) dies before the deep tier (priority 3), and the
    # request is still refused because the deep tier is the only thing left and
    # the fast tier is not ours to gamble with... except the fast tier *is*
    # ours, so it dies too if that is what it takes. Either way, order matters.
    assert gpu.killed[0] == "whisper"
    assert set(gpu.killed) <= {"whisper", "deep"}


def test_fast_tier_is_sacrificed_last(rig) -> None:
    """Order of sacrifice: transient first, the resident brain only if nothing
    else is left to kill."""
    gpu, _, arbiter = rig
    for holder, priority in (
        ("fast", Priority.FAST_TIER),
        ("deep", Priority.DEEP_TIER),
        ("vision", Priority.VISION),
    ):
        assert isinstance(arbiter.acquire(holder, int(1.0 * GIB), priority), Lease)
        gpu.rent(holder, int(1.0 * GIB))

    assert isinstance(arbiter.acquire("worker", int(8.0 * GIB), Priority.WORKER), Lease)
    assert gpu.killed == ["vision", "deep"]
    assert {lease.holder for lease in arbiter.leases} == {"fast", "worker"}


def test_tier_swap_kills_the_fast_tier_when_it_is_the_only_tenant(rig) -> None:
    """D22's actual mechanism, in the case that matters in production.

    With nothing else of ours loaded, loading the deep tier *is* the tier swap:
    the fast tier must die so its VRAM can be used. This is not a bug, it is the
    designed behaviour - and it is why the cost of an escalation is a reload.
    """
    gpu, _, arbiter = rig
    assert isinstance(arbiter.acquire("fast", FAST, Priority.FAST_TIER), Lease)
    gpu.rent("fast", FAST)
    assert gpu.free < arbiter.reserve_bytes + DEEP  # the deep tier does not fit

    assert isinstance(arbiter.acquire("deep", DEEP, Priority.DEEP_TIER), Lease)
    assert gpu.killed == ["fast"]
    assert {lease.holder for lease in arbiter.leases} == {"deep"}


def test_user_workload_is_never_terminated(rig) -> None:
    """The promise, in one test.

    Something the assistant does not own takes the memory. The arbiter must
    refuse, and must not have killed a single thing.
    """
    gpu, _, arbiter = rig
    gpu.user_starts_a_game(8.0)  # free drops to ~3 GiB
    denial = as_denial(arbiter.acquire("deep", DEEP, Priority.DEEP_TIER))
    assert denial is not None
    assert gpu.killed == []
    assert arbiter.leases == []


def test_user_workload_plus_our_leases_still_only_kills_ours(rig) -> None:
    gpu, _, arbiter = rig
    assert isinstance(arbiter.acquire("deep", DEEP, Priority.DEEP_TIER), Lease)
    gpu.rent("deep", DEEP)
    gpu.user_starts_a_game(4.0)

    arbiter.acquire("vision", int(1.0 * GIB), Priority.VISION)
    # The deep tier is ours, so it is the only acceptable sacrifice.
    assert set(gpu.killed) <= {"deep"}
    assert "user" not in gpu.killed


def test_release_does_not_kill(rig) -> None:
    """Killing on release would make a routine model swap look like a crash."""
    gpu, _, arbiter = rig
    arbiter.acquire("fast", FAST, Priority.FAST_TIER)
    assert arbiter.release("fast") is True
    assert arbiter.release("fast") is False
    assert gpu.killed == []


def test_summary_mentions_reserve_and_live_leases(rig) -> None:
    _, _, arbiter = rig
    arbiter.acquire("fast", FAST, Priority.FAST_TIER)
    text = arbiter.summary()
    assert "1.5 reserved" in text
    assert "fast@FAST_TIER" in text
    assert "denials 0" in text


def test_unreadable_gpu_refuses_to_schedule(rig) -> None:
    def broken() -> VramSnapshot:
        raise VramUnavailable("nvidia-smi is not on PATH")

    arbiter = VramArbiter(snapshot_fn=broken)
    with pytest.raises(VramUnavailable):
        arbiter.acquire("fast", FAST, Priority.FAST_TIER)
    assert arbiter.leases == []


def test_read_vram_parses_this_machine() -> None:
    """Integration, no mocks: there is a real GPU on this box.

    Phase 0 measured 12287 MiB total. If this ever fails, the arbiter is blind
    and must not be trusted, so the test fails loudly rather than mocking it away.
    """
    snap = read_vram()
    assert 10 * GIB < snap.total < 13 * GIB
    assert snap.used + snap.free <= snap.total + 64 * 1024 * 1024  # allow rounding
    assert 11.0 < snap.gib[0] < 13.0
