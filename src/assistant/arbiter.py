"""VRAM arbiter - the single authority on GPU memory (D1).

A budget is a wish. This is the mechanism. Models do not ask permission; they
acquire a *lease*, and this module is the only thing in the system that grants one.

Two properties matter more than anything else here, and both are tested:

1. **Preemption only ever touches what we own.** The arbiter may kill the
   assistant's own models without asking. It must never touch the user's game,
   their training run, or their CUDA job. External pressure is therefore
   *measured* (by reading free VRAM) and never *acted upon* - it has no lease,
   so it can never be selected for preemption. This is a structural property,
   not a code path someone has to remember to take.

2. **VRAM comes back because a process dies, not because a lease expired.** So
   this module never frees anything itself. It decides; the process manager
   (D3) kills. That keeps the arbiter pure and testable, and keeps the only
   operation with real teeth in one place.

Nothing here imports a model runtime or touches the network.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field, replace
from enum import IntEnum
from typing import Callable, Iterable

MIB = 1024 * 1024
GIB = 1024 * MIB

#: Held back for the OS, the display, and whatever the user is actually doing.
#: Measured on an RTX 3060 where median output throughput falls from ~200 tok/s
#: below 6.5 GB to ~109 tok/s above 8 GB - the card is not merely full, it is
#: slow, and the user must never feel the assistant's cost as a stutter.
DEFAULT_RESERVE = int(1.5 * GIB)

#: D3/D22: a model that has not been used for this long is killed, which is what
#: actually returns its VRAM on Windows.
DEFAULT_TTL = 90.0


class Priority(IntEnum):
    """Preemption order, lowest number survives longest.

    Only assistant-owned holders appear here. The user's workload is not a
    tenant in this system and is never assigned a priority - it is not ours to
    schedule.
    """

    FAST_TIER = 1  # resident brain; preemption is expensive
    WORKER = 2  # a delegated Plane D worker
    DEEP_TIER = 3  # one-shot planning turn; cheap to lose, expensive to hold
    VISION = 4  # short-lived mmproj residency
    WHISPER = 5  # only resident while listening


@dataclass(frozen=True)
class VramSnapshot:
    total: int
    used: int
    free: int

    @property
    def gib(self) -> tuple[float, float, float]:
        return (self.total / GIB, self.used / GIB, self.free / GIB)


class VramUnavailable(RuntimeError):
    """nvidia-smi could not be read. Never guess - refuse to schedule."""


def read_vram(index: int = 0) -> VramSnapshot:
    """Read live GPU memory from nvidia-smi.

    Parsing text output rather than using a library keeps the dependency count
    at zero (D18's spirit) and, more importantly, keeps a third party's claim
    about our machine out of the trust path (D25's lesson). If this returns
    something unparseable we raise rather than fall back to a stale number,
    because a stale number is how an assistant quietly eats a user's VRAM.
    """
    exe = shutil.which("nvidia-smi")
    if exe is None:
        raise VramUnavailable("nvidia-smi is not on PATH; refusing to guess VRAM")
    out = subprocess.run(
        [exe, f"--id={index}", "--query-gpu=memory.total,memory.used,memory.free",
         "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    if out.returncode != 0:
        raise VramUnavailable(f"nvidia-smi exited {out.returncode}: {out.stderr.strip()}")
    line = out.stdout.strip().splitlines()[0] if out.stdout.strip() else ""
    nums = re.findall(r"\d+", line)
    if len(nums) != 3:
        raise VramUnavailable(f"unparseable nvidia-smi output: {line!r}")
    total, used, free = (int(n) * MIB for n in nums)
    return VramSnapshot(total=total, used=used, free=free)


@dataclass
class Lease:
    """A time-bounded right to occupy VRAM. Ownership is explicit and total.

    ``holder`` is an identifier the process manager can map to a PID. The
    arbiter never resolves it to a process itself; that indirection is what
    stops this module from being able to kill something it does not own.
    """

    holder: str
    need_bytes: int
    priority: Priority
    granted_at: float
    expires_at: float
    renewals: int = 0

    def expired(self, now: float) -> bool:
        return now >= self.expires_at


@dataclass(frozen=True)
class Denial:
    """A refusal, with a reason a human can act on. Never retried silently."""

    holder: str
    need_bytes: int
    reason: str
    free_bytes: int
    reserve_bytes: int
    preempted: tuple[str, ...] = ()

    def __str__(self) -> str:
        return (
            f"DENIED {self.holder}: needs {self.need_bytes / GIB:.2f} GiB, "
            f"only {self.free_bytes / GIB:.2f} GiB free with "
            f"{self.reserve_bytes / GIB:.2f} GiB reserved for you "
            f"({self.reason})"
        )


@dataclass
class ArbiterState:
    """Everything a 24/7 daemon would want to show in a status line."""

    reserve_bytes: int
    leases: list[Lease] = field(default_factory=list)
    denials: int = 0
    preemptions: int = 0
    expiries: int = 0


class VramArbiter:
    """Serialises access to a finite, shared resource.

    ``snapshot_fn`` is injectable so the tests can simulate a busy machine, a
    GPU disappearing, and a user starting a game, with no GPU required.
    ``terminator`` is injectable for the same reason: the arbiter decides *who*
    dies, and the process manager performs it.
    """

    def __init__(
        self,
        *,
        reserve_bytes: int = DEFAULT_RESERVE,
        ttl_seconds: float = DEFAULT_TTL,
        snapshot_fn: Callable[[], VramSnapshot] = read_vram,
        terminator: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.reserve_bytes = reserve_bytes
        self.ttl_seconds = ttl_seconds
        self._snapshot = snapshot_fn
        self._terminate = terminator
        self._clock = clock
        self._leases: dict[str, Lease] = {}
        self._denials = 0
        self._preemptions = 0
        self._expiries = 0

    # -- introspection ----------------------------------------------------

    @property
    def leases(self) -> list[Lease]:
        return sorted(self._leases.values(), key=lambda lease: lease.priority)

    def free_bytes(self) -> int:
        """VRAM we could take *right now*, ignoring leases.

        This is the honest number: it is measured, not inferred from our own
        bookkeeping, so it already accounts for the user's workloads.
        """
        return self._snapshot().free

    # -- the lease lifecycle ---------------------------------------------

    def acquire(self, holder: str, need_bytes: int, priority: Priority) -> Lease | Denial:
        """Grant a lease, or refuse with a reason.

        Order of operations matters: reap what is already dead, then preempt
        *our own* worst tenant if we must, then re-measure, and only then
        decide. We never re-measure from a cached snapshot, because the whole
        point is to see the machine as it is at the moment of the request.
        """
        self.sweep()

        if holder in self._leases:
            # Re-acquiring is a renewal, not a second lease. Two entries for one
            # process would make the preemption arithmetic a lie.
            return self.renew(holder, need_bytes=need_bytes, priority=priority)

        self._preempt_until_fits(need_bytes)

        snap = self._snapshot()
        headroom = snap.free - self.reserve_bytes
        if need_bytes > headroom:
            self._denials += 1
            return Denial(
                holder=holder,
                need_bytes=need_bytes,
                reason="not enough VRAM after preempting our own models",
                free_bytes=snap.free,
                reserve_bytes=self.reserve_bytes,
                preempted=tuple(lease.holder for lease in self.leases),
            )

        now = self._clock()
        lease = Lease(
            holder=holder,
            need_bytes=need_bytes,
            priority=priority,
            granted_at=now,
            expires_at=now + self.ttl_seconds,
        )
        self._leases[holder] = lease
        return lease

    def renew(
        self, holder: str, *, need_bytes: int | None = None, priority: Priority | None = None
    ) -> Lease | Denial:
        """Extend a live lease. A renewal never re-checks capacity: the VRAM is
        already ours, and re-checking would mean a lease could be revoked by a
        *user's* workload appearing - which would kill work mid-answer."""
        current = self._leases.get(holder)
        if current is None:
            return Denial(
                holder=holder,
                need_bytes=need_bytes or 0,
                reason="no live lease to renew",
                free_bytes=self.free_bytes(),
                reserve_bytes=self.reserve_bytes,
            )
        now = self._clock()
        updated = replace(
            current,
            need_bytes=need_bytes if need_bytes is not None else current.need_bytes,
            priority=priority if priority is not None else current.priority,
            expires_at=now + self.ttl_seconds,
            renewals=current.renewals + 1,
        )
        self._leases[holder] = updated
        return updated

    def release(self, holder: str) -> bool:
        """Give up a lease. Does not kill anything - the caller owns the process
        lifecycle, and killing on release would make a routine model swap look
        like a crash to everything upstream."""
        return self._leases.pop(holder, None) is not None

    def sweep(self, now: float | None = None) -> list[str]:
        """Reap leases whose TTL has passed, and return who to kill.

        This is the mechanism by which the GPU comes back to baseline (D3).
        Call it on a timer; the daemon does.
        """
        moment = self._clock() if now is None else now
        dead = [lease for lease in self._leases.values() if lease.expired(moment)]
        victims = []
        for lease in sorted(dead, key=lambda item: item.priority):
            self._leases.pop(lease.holder, None)
            self._expiries += 1
            victims.append(lease.holder)
            self._terminate(lease.holder)
        return victims

    # -- preemption -------------------------------------------------------

    def _preempt_until_fits(self, need_bytes: int) -> list[str]:
        """Sacrifice our own least important tenant, repeatedly, until the
        measured headroom suffices or we run out of things we own.

        Only leases are candidates. Whatever the user is running is invisible
        to this function by construction - it has no lease and cannot be named
        here. If the user's workload is what is filling VRAM, the only outcome
        available to us is the denial at the end of ``acquire``, which is the
        correct and intended behaviour: we report, we do not compete.
        """
        killed: list[str] = []
        while self._snapshot().free - self.reserve_bytes < need_bytes:
            candidates = [
                lease
                for lease in self.leases
                if lease.priority >= Priority.FAST_TIER
            ]
            if not candidates:
                break
            victim = candidates[-1]  # highest priority number = least important
            self._leases.pop(victim.holder, None)
            self._preemptions += 1
            killed.append(victim.holder)
            self._terminate(victim.holder)
        return killed

    def preempt(self, holder: str) -> bool:
        """Drop a specific lease we own. Used when the user asks for the GPU back."""
        return self.release(holder)

    # -- reporting --------------------------------------------------------

    def summary(self) -> str:
        """One human-readable line for the tray tooltip and the status command."""
        try:
            snap = self._snapshot()
            used, free, total = (f"{v / GIB:.1f}" for v in (snap.used, snap.free, snap.total))
        except VramUnavailable as exc:
            used = free = total = "?"
            snap = VramSnapshot(0, 0, 0)  # type: ignore[assignment]
            _ = exc
        live = ", ".join(f"{lease.holder}@{lease.priority.name}" for lease in self.leases)
        return (
            f"VRAM {used}/{total} GiB used, {free} free, "
            f"{self.reserve_bytes / GIB:.1f} reserved | leases: {live or 'none'} | "
            f"preemptions {self._preemptions}, expiries {self._expiries}, "
            f"denials {self._denials}"
        )


def format_denial(denial: Denial) -> str:
    """Denials are user-facing text, not exception messages."""
    return str(denial)


def as_denial(result: Lease | Denial) -> Denial | None:
    """Narrow a result for callers that do not want an isinstance ladder."""
    return result if isinstance(result, Denial) else None


def total_leased(arbiter: VramArbiter) -> int:
    return sum(lease.need_bytes for lease in arbiter.leases)


def iter_ours(arbiter: VramArbiter) -> Iterable[str]:
    """Every holder this arbiter is responsible for. The daemon's shutdown path
    walks exactly this list - which is why nothing outside the arbiter may kill
    a process holding a lease."""
    return (lease.holder for lease in arbiter.leases)
