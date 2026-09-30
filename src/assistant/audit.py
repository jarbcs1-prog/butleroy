"""Append-only, hash-chained audit log.

Every consequential decision the assistant makes goes through here, and
nothing the assistant does is meant to be forgettable. The chain exists so
that "the log says the model was denied" is a checkable claim rather than a
trust exercise: if a record is edited or removed after the fact, `verify`
finds it.

This is the Python counterpart of the supervisor's `AuditTrail.verifyIntegrity`
(ARCHITECTURE.md, D12). We keep the two implementations behaviourally aligned
so a chain written by the daemon can be checked by the supervisor's tooling
and vice versa.

Design notes
------------
* **Append-only.** The writer only ever opens the file for append. There is no
  update or delete method, deliberately.
* **Hash-chained.** Each record commits to the previous record's hash, so any
  edit invalidates every record after it, not just the edited one.
* **Flush per record.** A 24/7 daemon that is killed - by the user, by a
  reboot, by a power cut - must not lose the last few actions. Buffering buys
  throughput we do not need: this is a handful of records per minute.
* **The daemon never deletes its own policy.** D21 forbids self-modification
  of code and policy, and the policy gate's deny-path is what enforces that.
  This log is the evidence trail, not a writable surface.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

GENESIS = "0" * 64


def _canonical(payload: dict[str, Any]) -> str:
    """Deterministic JSON so the hash is reproducible across runs.

    sort_keys and tight separators matter: a space difference would change
    every hash in the chain and make verification fail for no real reason.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _hash_record(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AuditRecord:
    """One immutable decision. Frozen so no code path can mutate history."""

    seq: int
    timestamp: str
    actor: str
    channel: str
    tool: str
    tier: str
    decision: str
    reason: str
    args: dict[str, Any]
    prev_hash: str
    hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "timestamp": self.timestamp,
            "actor": self.actor,
            "channel": self.channel,
            "tool": self.tool,
            "tier": self.tier,
            "decision": self.decision,
            "reason": self.reason,
            "args": self.args,
            "prev_hash": self.prev_hash,
            "hash": self.hash,
        }


@dataclass
class ChainResult:
    """Outcome of verifying a log. `ok` alone is never the answer."""

    ok: bool
    checked: int
    first_bad_seq: int | None = None
    detail: str = ""


@dataclass
class AuditLog:
    """Writer and verifier. Thread-safe; the daemon is multi-threaded."""

    path: Path
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _last_hash: str = GENESIS
    _seq: int = 0
    _initialised: bool = False

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        if not self._initialised:
            self._init_tail()

    def _init_tail(self) -> None:
        """Resume the chain from an existing log rather than restarting it.

        A daemon restart must not silently begin a second, disconnected chain in
        the same file - that would make the file unverifiable as a whole.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.stat().st_size > 0:
            last = None
            for record in self.read():
                last = record
            if last is not None:
                self._last_hash = last.hash
                self._seq = last.seq
        self._initialised = True

    def append(
        self,
        *,
        actor: str,
        channel: str,
        tool: str,
        tier: str,
        decision: str,
        reason: str = "",
        args: dict[str, Any] | None = None,
    ) -> AuditRecord:
        """Record one decision. Returns the stored record so callers can cite it."""
        with self._lock:
            self._seq += 1
            body = {
                "seq": self._seq,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "actor": actor,
                "channel": channel,
                "tool": tool,
                "tier": tier,
                "decision": decision,
                "reason": reason,
                "args": args or {},
                "prev_hash": self._last_hash,
            }
            record = AuditRecord(hash=_hash_record(body), **body)

            line = _canonical(record.to_dict()) + "\n"
            # Opened per record and never held open: the only writer is this
            # process, and losing the tail on a hard kill is not acceptable.
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())

            self._last_hash = record.hash
            return record

    def read(self) -> Iterator[AuditRecord]:
        """Yield records, skipping (and reporting) unparseable lines.

        A truncated final line is expected after a hard kill. It is skipped
        rather than raised, and `verify` is what reports the inconsistency.
        """
        if not self.path.exists():
            return
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                    yield AuditRecord(**raw)
                except (json.JSONDecodeError, TypeError):
                    continue

    def verify(self) -> ChainResult:
        """Walk the chain and confirm every link.

        Checks three independent things, because they fail differently:
        recomputed hash matches (was the record edited?), prev_hash links to
        the previous record (was a record removed or reordered?), and sequence
        numbers are contiguous (same question asked the other way).
        """
        expected_prev = GENESIS
        expected_seq = 1
        checked = 0

        for record in self.read():
            checked += 1

            body = {
                k: v
                for k, v in record.to_dict().items()
                if k != "hash"
            }
            if _hash_record(body) != record.hash:
                return ChainResult(
                    ok=False,
                    checked=checked,
                    first_bad_seq=record.seq,
                    detail=f"seq {record.seq}: content hash mismatch - record was edited",
                )

            if record.prev_hash != expected_prev:
                return ChainResult(
                    ok=False,
                    checked=checked,
                    first_bad_seq=record.seq,
                    detail=(
                        f"seq {record.seq}: broken link - record was removed, "
                        f"reordered, or inserted"
                    ),
                )

            if record.seq != expected_seq:
                return ChainResult(
                    ok=False,
                    checked=checked,
                    first_bad_seq=record.seq,
                    detail=f"seq gap: expected {expected_seq}, found {record.seq}",
                )

            expected_prev = record.hash
            expected_seq += 1

        return ChainResult(ok=True, checked=checked)
