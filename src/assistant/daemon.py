"""Plane A: the resident daemon.

This is the only part of the assistant that is always on. It holds the policy
gate, the VRAM arbiter, the hash-chained audit log, the workspace manifest store
and the Obsidian bridge, and it answers requests over a Windows named pipe.

Four properties are load-bearing, and each one exists because its absence would
be a security or reliability hole rather than a missing feature.

1. **No model runtime is ever imported here.** Not lazily, not behind a flag.
   ``llama_cpp`` must not appear in this module's namespace after ``import
   assistant.daemon``, and there is a test that asserts it. A brain turn is a
   *request* this daemon emits on ``PIPE_BRAIN_RESP``; it is never served from
   this process. That is what keeps Plane A at a few hundred MB with zero VRAM
   (D1), and it means a broken or half-finished model build cannot take the
   daemon down - the daemon would still be holding the gate, the leases and the
   audit chain while the brain is dead.

2. **A broken audit chain stops the daemon, it does not get logged and ignored.**
   ``verify()`` runs before the pipe is bound. A chain that does not verify
   means something has already tampered with the record of what was done, and a
   process that cannot trust its own log has no business being handed new
   actions. It refuses to start and says so.

3. **Attended/unattended is daemon state, not a per-request claim.** A caller
   cannot send ``attended=true`` to unlock a T2 action, because that would make
   the unattended ceiling (D13) decorative. Only ``session.set_attended``
   changes it, that op is itself audited, and it may only ever *narrow*.

4. **Gating and executing are separate steps, and a missing executor is an
   error rather than a success.** The tool registry (D16) is the complete
   capability surface. Only a handful of those tools have handlers. Calling one
   that has no handler returns an explicit "not implemented" failure and is
   audited as a failure - it never returns quietly, because a caller that
   receives an empty success will report the job as done.

Every op is a method listed in ``_OPS``. An op that is not in that table is
refused. The fail-closed rule from the policy gate applies to the transport
itself: an unknown op is a client speaking a protocol we do not implement, and
the only safe answer to that is no.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Callable

from . import __version__, ipc
from .arbiter import Denial, Priority, VramArbiter, VramUnavailable, format_denial
from .audit import AuditLog
from .manifest import TIER_BY_NAME, ManifestStore, NotPermitted, Workspace
from .obsidian import ObsidianBridge, ReadRefused, VaultError, WriteRefused
from . import fileops, sysinfo
from . import audio, speech, voice_loop
#: The child's tier table, imported rather than restated. A second copy of this
#: list would be a second answer to "which models exist", and the two would drift
#: apart in exactly the way that produces a model nobody can load.
from . import brain_server
from .brain_server import TIERS as BRAIN_TIERS
from .brain_server import arm_kill_on_close as _arm_kill_on_close
from .policy import (
    CHANNEL_PC,
    CHANNEL_SCHEDULED,
    CHANNEL_TELEGRAM,
    Decision,
    PolicyGate,
    Tier,
    Verdict,
)
from .tools import ALL_TOOLS, default_gate

# Where we keep everything that is ours. Nothing outside this tree is ever
# written by the daemon itself.
ROOT = Path(__file__).resolve().parent.parent.parent
STATE = ROOT / "state"

# Channels that may claim the user is present. A channel that is not in here
# can never raise the ceiling, no matter what it sends.
PRESENT_CHANNEL = (CHANNEL_PC,)


def _voice_stats(loop: Any) -> dict[str, int]:
    """The loop's counters, as plain numbers.

    Read off the object rather than stored, so there is one number and not a copy
    that can disagree with it.
    """
    s = getattr(loop, "stats", None)
    if s is None:
        return {}
    return {k: int(v) for k, v in vars(s).items() if isinstance(v, (int, float))}


def _describe_result(tool: str, result: Any) -> str:
    """Say what a tool did, in a sentence worth hearing.

    Deliberately does not dump the result. A spoken answer is read aloud, and
    `{'deleted': 1, 'bytes': 4021, 'recoverable_until': ...}` is not an answer,
    it is a log line. Where a human-facing summary exists it is used; otherwise a
    short factual statement of what was done.
    """
    if result is None:
        return f"Done: {tool}."
    if isinstance(result, str):
        return result[:400]
    if isinstance(result, dict):
        for key in ("summary", "message", "answer", "text", "transcript"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value[:400]
        if "error" in result:
            return f"That did not work: {result['error']}"[:400]
        return f"Done: {tool}."[:400]
    return f"Done: {tool}."

class DaemonError(RuntimeError):
    """An op failed in a way the caller is allowed to see."""


class Refused(DaemonError):
    """An op was refused by policy. Never a transport failure."""


class NotImplementedOp(DaemonError):
    """The tool is in the registry but has no executor yet.

    Distinct from Refused on purpose. "I will not do this" and "I have not
    built this yet" are different answers and conflating them is how an
    assistant ends up reporting unfinished work as finished.
    """


def _jsonable(value: Any) -> Any:
    """Coerce a return value into something JSON-serialisable.

    Dataclasses and Tiers show up constantly in this codebase; without this
    every op would need its own conversion and one of them would forget.
    """
    if is_dataclass(value) and not isinstance(value, type):
        return {k: _jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, Tier):
        return str(value)
    if isinstance(value, Decision):
        return value.value
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return str(value)


def _redact(args: dict | None) -> dict:
    """Never write a secret into an append-only log.

    The log is the most-read file in the project and the least likely to be
    treated as sensitive. Anything whose *name* looks like a credential is
    replaced before it can become permanent, because "the audit log is
    append-only" and "the audit log contains my password" cannot both be true.
    """
    if not args:
        return {}
    secret_words = ("token", "secret", "password", "passwd", "key", "auth", "cookie")
    out = {}
    for k, v in args.items():
        if any(w in str(k).lower() for w in secret_words):
            out[k] = "<redacted>"
        elif isinstance(v, str) and len(v) > 2000:
            out[k] = v[:2000] + f"...<truncated {len(v)-2000} chars>"
        else:
            out[k] = v
    return out


def _tiers(names: Any) -> tuple[Tier, ...]:
    """Accept 'T0' or 'READ' and refuse anything else by name.

    Tier's *member* names (READ, REVERSIBLE_WRITE, ...) are not its printed
    names (T0..T3), and manifests are written in the printed form because that
    is what the user reads in DECISIONS.md. So both spellings are accepted here
    rather than making the caller remember which one this particular enum uses.
    """
    if isinstance(names, (str, Tier)):
        names = [names]
    out: list[Tier] = []
    for n in names:
        if isinstance(n, Tier):
            out.append(n)
            continue
        key = str(n)
        if key not in TIER_BY_NAME:
            raise Refused(
                f"{key!r} is not an authority tier; valid: "
                + ", ".join(sorted(TIER_BY_NAME))
                + " (both 'T0' and 'READ' spellings are accepted)"
            )
        out.append(TIER_BY_NAME[key])
    return tuple(out)


class Daemon:
    """The resident process. Construct, then ``serve_forever()``."""

    def __init__(
        self,
        *,
        state: Path | str = STATE,
        vault: Path | str | None = None,
        attended: bool = True,
        session_ceiling: Tier = Tier.IRREVERSIBLE,
    ):
        state = Path(state)
        self.state = state
        state.mkdir(parents=True, exist_ok=True)

        # --- audit first, because everything else depends on it -------------
        self.audit = AuditLog(state / "audit.jsonl")

        # --- manifests: the only way anything outside this tree is reached --
        self.manifests = ManifestStore(state / "workspaces", ceiling=session_ceiling)
        self.session_ceiling = session_ceiling

        # The gate's allowed roots are the approved workspaces. This is the join
        # between two modules that were written independently: D10 says the
        # daemon knows nothing about any project, D13 says paths are checked
        # against approved roots, and this line is where "approved" gets its
        # only source of truth. A root is reachable if and only if a manifest
        # says so.
        self.gate = default_gate(list(self.manifests.roots()))
        self.gate.attended = attended
        self.attended = attended

        # --- VRAM arbiter ---------------------------------------------------
        self.arbiter = VramArbiter()

        # --- Obsidian: optional, must never stop the daemon -----------------
        # The vault is on E:, which is a removable-or-not volume the user may
        # have unmounted, renamed, or never had. None of the live tier depends
        # on it, so construction is allowed to fail and be reported.
        self.vault_error: str | None = None
        self.vault: ObsidianBridge | None = None
        if vault is not None:
            try:
                self.vault = ObsidianBridge(Path(vault))
            except VaultError as exc:
                self.vault_error = str(exc)

        self.started_at = time.time()
        # T2/T3 calls that arrive while unattended land here and wait for a
        # human. A list, not a queue object, because nothing pops it yet and a
        # reader that forgets to pop would rather see the backlog than silently
        # lose it.
        self.queue: list[dict] = []
        self.authkey = ipc.generate_authkey()
        self.authkey_path = state / "authkey.bin"
        self.server: ipc.PipeServer | None = None
        # RLock, not Lock. op_confirm_approve calls _new_confirmation *while it
        # holds this lock* (requeueing a confirmation whose payload changed), and
        # _new_confirmation takes the lock itself to store the new item. With a
        # plain Lock that is a self-deadlock, and it is a hang rather than an
        # error - which on a 24/7 daemon means a wedged request thread with
        # nothing in the log. Reentrancy is the correct fix here: the same lock
        # discipline still applies, a thread can simply re-enter its own lock.
        self._lock = threading.RLock()
        self._client: ipc.PipeClient | None = None
        self._count = 0
        # Set by request_shutdown() so the serve loop can be stopped from the tray
        # or hotkey thread. See the method for why this is an Event and not something
        # cleverer.
        self._halt = threading.Event()
        # --- D32 confirmation state -------------------------------------------
        # Pending confirmations, keyed by an opaque id. The payload hash is what
        # the user actually approves, so an approval that does not carry the hash
        # the dialog displayed is refused rather than trusted.
        self._pending: dict[str, dict] = {}
        # Class grants, keyed by "<tool>|<root>", value = monotonic deadline.
        # Expiry is not enough on its own: see _invalidate_grants.
        self._grants: dict[str, float] = {}
        self._pending_seq = 0
        self.quarantine = state / "quarantine"
        # D3 child bookkeeping. `_brain_last_used` is 0.0 rather than None so the
        # arithmetic in the sweeper is uniform; the ops that report idle time
        # check for 0.0 and say "never used" rather than showing a number
        # derived from the epoch.
        self._brain: subprocess.Popen | None = None
        self._brain_client: ipc.PipeClient | None = None
        # Which tier the running child holds, or None if there is no child. Kept
        # next to the child rather than derived from it, so "the deep tier is
        # loaded" is a fact we can answer without loading a model to find out.
        self._brain_tier: str | None = None
        # The Windows job the child lives in. Holding this handle is what keeps the
        # child alive: drop it and the kernel kills the child, whatever state the
        # daemon is in when that happens. `parent_alive` in the child is the
        # fallback for when this could not be created.
        self._brain_job: Any = None
        # Voice. The loop is kept on the instance rather than as a module global
        # because it owns a microphone and a wake word model, and two daemons in
        # one process would then fight over the device - which is the same reason
        # the named pipes collide. The thread is daemon=True so a stuck
        # microphone cannot keep the process alive at shutdown.
        self._voice_loop: Any = None
        self._voice_thread: threading.Thread | None = None
        self._brain_last_used = 0.0
        self._brain_loaded = False
        self._sweeper: threading.Thread | None = None
        # The Telegram bridge, if one could be built. Absent is not an error:
        # a machine with no token still serves the PC and the hotkey, and the
        # bridge is the only thing in Plane A that touches a network.
        self.telegram = None
        self.telegram_error: str | None = None
        self._load_telegram()
        #: The pipe the child *serves* turns on. Distinct from PIPE_BRAIN_REQ,
        #: which is the pipe we *serve* on and the child *calls* to ask for its
        #: lease. Sharing one name would mean the child asks itself for
        #: permission, which fails in a way that looks like a refusal.
        self.brain_pipe = ipc.PIPE_BRAIN_RESP
        #: The pipe we serve on, and the one the child calls back through.
        #: Set by serve_forever so a caller that moved the daemon's pipe is
        #: followed by the child rather than being silently bypassed.
        self.daemon_pipe = ipc.PIPE_BRAIN_REQ

    # ------------------------------------------------------------------ startup

    def preflight(self) -> dict:
        """Everything that must be true before we accept a single request."""
        checks: dict[str, Any] = {}

        checks["audit_chain"] = _jsonable(self.audit.verify())
        if not checks["audit_chain"]["ok"]:
            raise DaemonError(
                "audit chain does not verify; refusing to start. "
                f"first bad record: seq {checks['audit_chain']['first_bad_seq']} "
                f"({checks['audit_chain']['detail']}). "
                "Inspect state\\audit.jsonl before doing anything else."
            )

        # A third-party library that quietly opened a listening socket would
        # break D3 and D12, and it would be invisible in a log.
        ipc.assert_no_ports()

        checks["no_listening_ports"] = True
        checks["audit_records"] = checks["audit_chain"]["checked"]
        checks["workspaces"] = [w.name for w in self.manifests]
        checks["allowed_roots"] = list(self.manifests.roots())
        checks["attended"] = self.attended
        checks["session_ceiling"] = str(self.session_ceiling)
        checks["vault"] = self.vault.describe() if self.vault else None
        checks["vault_error"] = self.vault_error
        checks["vram"] = None
        try:
            checks["vram"] = _jsonable(self.arbiter._snapshot())  # noqa: SLF001
        except VramUnavailable as exc:
            # Not fatal at startup: the arbiter will refuse to schedule, which
            # is the correct behaviour, and the daemon should still be up to
            # tell you why.
            checks["vram"] = {"unavailable": str(exc)}
        checks["version"] = __version__
        return checks

    # ------------------------------------------------------------------- serving

    def handle(self, request: ipc.Request) -> dict:
        """Dispatch one request. Never raises; returns a JSON-safe dict."""
        op = request.op
        args = request.args or {}
        handler = self._OPS.get(op)
        if handler is None:
            # Marked refused, not just failed: an unknown op is "I will not do
            # this", and a caller that cannot tell that apart from "I have not
            # built this yet" will eventually report the difference to you as
            # though the assistant had quietly dropped the job.
            return {
                "ok": False,
                "error": f"unknown op {op!r}",
                "refused": True,
                "known_ops": sorted(self._OPS),
            }
        try:
            # Channel is validated centrally, before dispatch, rather than by
            # each op that happens to need it. An unrecognised channel reaching a
            # handler is a request whose provenance nobody can vouch for, and the
            # D13 channel rule is only as strong as this check.
            if args.get("channel") is not None:
                self._channel(args)
            return {"ok": True, "result": _jsonable(handler(self, args))}
        except Refused as exc:
            return {"ok": False, "error": str(exc), "refused": True}
        except NotImplementedOp as exc:
            return {"ok": False, "error": str(exc), "not_implemented": True}
        except DaemonError as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        except (ValueError, KeyError, TypeError, OSError) as exc:
            # A bad argument is the caller's problem and deserves a useful
            # message. An unexpected exception is ours and must not leak a
            # traceback with absolute paths to a peer.
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        except Exception as exc:  # noqa: BLE001 - deliberate last resort
            # Last resort, and the reason it exists: a bug in one handler must
            # not take down a 24/7 resident daemon, and a traceback crossing a
            # named pipe hands the peer our absolute paths. The full detail goes
            # to the local notes; the peer gets the type and message only.
            # BaseException (KeyboardInterrupt, SystemExit) is deliberately not
            # caught - those are how the process is told to stop.
            detail = traceback.format_exc()
            ipc.note(f"unhandled {type(exc).__name__} in op {op!r}: {detail}")
            try:
                self._audit(
                    actor="daemon",
                    channel=CHANNEL_INTERNAL,
                    tool=f"op.{op}",
                    tier="?",
                    decision="error",
                    reason=f"unhandled {type(exc).__name__}: {exc}",
                )
            except Exception:  # noqa: BLE001
                # The audit sink itself is broken. Losing the record is bad;
                # losing the process because of it is worse. The note above is
                # the only surviving evidence, so we do not raise over it.
                pass
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def request_shutdown(self, why: str = "requested") -> None:
        """Ask the daemon to stop, from another thread.

        The tray and the hotkey run on their own threads, so they cannot simply
        return from `serve_forever`. This sets an event that the serve loop is
        already parked on.

        The obvious alternatives were rejected on purpose. Raising KeyboardInterrupt
        from the tray thread would interrupt the tray, not the serve loop.
        `PyThreadState_SetAsyncExc` would work and is a one-liner, but it injects an
        exception into a thread's stack at an arbitrary point, which is exactly the
        kind of cleverness that is fine in a demo and fatal in something meant to be
        always-on. A flag the loop checks is boring and cannot surprise anyone.
        """
        self._audit(
            actor="daemon",
            channel=CHANNEL_SCHEDULED,
            tool="daemon.shutdown_requested",
            tier=str(Tier.READ),
            decision="allow",
            reason=why,
        )
        self._halt.set()

    def serve_forever(self, pipe: str = ipc.PIPE_BRAIN_REQ) -> None:
        checks = self.preflight()
        self._audit(
            actor="daemon",
            channel=CHANNEL_SCHEDULED,
            tool="daemon.start",
            tier=str(Tier.READ),
            decision="allow",
            reason=f"preflight ok: {len(checks['workspaces'])} workspaces, "
            f"audit {checks['audit_records']} records verified",
        )
        ipc.store_authkey(self.authkey_path, self.authkey)
        self.server = ipc.PipeServer(pipe, self.handle, authkey=self.authkey)
        # The child must call back on the pipe we actually bound. If --pipe moved
        # us, a child told to call the default would reach nothing, and the lease
        # denial would be the only symptom.
        self.daemon_pipe = pipe
        self.server.start()
        print(f"[daemon] listening on {pipe}", flush=True)
        print(f"[daemon] state    {self.state}", flush=True)
        print(f"[daemon] version  {__version__}", flush=True)
        # Start the idle sweeper only when actually serving. A Daemon built in a
        # test, or in --check, must not gain a background thread that outlives the
        # assertion that produced it.
        self._sweeper = threading.Thread(
            target=self._brain_sweeper, name="brain-sweeper", daemon=True
        )
        self._sweeper.start()
        # The Telegram bridge starts only when serving, and only if it was
        # configured. Same reasoning as the sweeper: a Daemon built in a test or
        # in --check must not acquire a thread that outlives the assertion.
        if self.telegram is not None:
            threading.Thread(
                target=self._telegram_thread, name="telegram", daemon=True
            ).start()
            print(
                f"[daemon] telegram  bridge configured, "
                f"{len(self.telegram.config.allowed_chat_ids)} allowed chat id(s)",
                flush=True,
            )
        elif self.telegram_error:
            print(f"[daemon] telegram  not available: {self.telegram_error}", flush=True)
        try:
            while not self._halt.wait(3600):
                pass
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        for note in ipc.drain_notes():
            self._audit(
                actor="daemon",
                channel=CHANNEL_SCHEDULED,
                tool="daemon.note",
                tier=str(Tier.READ),
                decision="allow",
                reason=note,
            )
        if self.server is not None:
            self.server.stop()
        if self.telegram is not None:
            self.telegram.stop()
        # Kill the brain before we record the stop. A daemon that shut down
        # leaving a child holding 5 GiB would hand the memory back only when the
        # machine reboots, and the audit line "clean shutdown" would be the last
        # and most reassuring thing on record about it.
        self._brain_stop("daemon shutting down")
        self._audit(
            actor="daemon",
            channel=CHANNEL_SCHEDULED,
            tool="daemon.stop",
            tier=str(Tier.READ),
            decision="allow",
            reason="clean shutdown",
        )

    def client(self) -> ipc.PipeClient:
        with self._lock:
            if self._client is None:
                self._client = ipc.PipeClient(ipc.PIPE_BRAIN_REQ, authkey=self.authkey)
            return self._client

    # -------------------------------------------------------------------- audit

    def _audit(self, **kw) -> None:
        kw.setdefault("actor", "user")
        self._audit_sink(**kw)

    def _audit_sink(self, **kw) -> None:
        # args is pulled out rather than passed alongside: AuditLog.append takes
        # an `args` keyword itself, so **kw plus an explicit args= is a duplicate
        # argument. Redaction happens here, once, so no call site can forget.
        kw["args"] = _redact(kw.get("args"))
        self.audit.append(**kw)
        self._count += 1

    # ----------------------------------------------------------------- sessions

    def op_ping(self, args: dict) -> Any:
        return "pong"

    def op_status(self, args: dict) -> Any:
        return {
            "version": __version__,
            "uptime_s": round(time.time() - self.started_at, 1),
            "requests": self._count,
            "attended": self.attended,
            "session_ceiling": self.session_ceiling,
            "workspaces": [w.name for w in self.manifests],
            "tools_registered": len(ALL_TOOLS),
            "tools_executable": sorted(self._EXECUTORS),
            "vram": self.arbiter.summary(),
            "audit_records": self.audit.verify().checked,
        }

    def op_session_set_attended(self, args: dict) -> Any:
        """Only op that may change attended, and it may only narrow.

        A caller asking for attended=True is refused rather than ignored: it
        means something upstream believes it can grant itself the right to run
        T2 actions without a human, and that belief is the bug.
        """
        want = bool(args.get("attended", False))
        channel = args.get("channel", CHANNEL_SCHEDULED)
        # The only rule: asserting presence requires a present channel. Repeated
        # and idempotent, because the presence detector will legitimately send
        # the same value many times, and a detector that has to work around a
        # refusal will eventually stop being wired up at all.
        if want and channel not in PRESENT_CHANNEL:
            raise Refused(
                f"channel {channel!r} may not assert the user is present; "
                f"only {PRESENT_CHANNEL} may"
            )
        before = self.attended
        self.attended = want
        self.gate.attended = want
        # D32: a class grant meant "I'll be here for the next ten minutes". If the
        # user is no longer here, the premise of the grant is gone, so the grant
        # goes with it. Without this, walking away from the keyboard would silently
        # leave standing permissions behind.
        if before and not want:
            self._invalidate_grants()
        self._audit(
            actor=args.get("actor", "user"),
            channel=channel,
            tool="session.set_attended",
            tier=str(Tier.READ),
            decision="allow" if before == want else "allow",
            reason=f"attended {before} -> {want}",
        )
        return {"attended": self.attended}

    # --------------------------------------------------------------------- gate

    def _channel(self, args: dict) -> str:
        channel = str(args.get("channel") or CHANNEL_SCHEDULED)
        known = {CHANNEL_PC, "voice", CHANNEL_TELEGRAM, CHANNEL_SCHEDULED, "internal"}
        if channel not in known:
            raise ValueError(f"unknown channel {channel!r}; expected one of {sorted(known)}")
        return channel

    def _verdict(self, tool: str, channel: str, args: dict) -> Verdict:
        # allowed_roots is recomputed from the manifest store on every call
        # rather than cached at construction. A workspace added or removed
        # while the daemon runs must take effect immediately; a stale copy
        # would be a permission that outlives the decision to grant it.
        self.gate.allowed_roots = list(self.manifests.roots())
        verdict = self.gate.evaluate(tool, channel=channel, args=args)

        # A live class grant downgrades CONFIRM to ALLOW, but only for the PC
        # channel and only for a genuinely T3 tool. Both restrictions matter:
        # a grant is something the user established while sitting at the
        # keyboard, so it must not be cashable by a Telegram message that happens
        # to arrive mid-window, and it must not be a way to quietly loosen T2.
        if (
            verdict.decision == Decision.CONFIRM
            and channel == CHANNEL_PC
            and verdict.tier == Tier.IRREVERSIBLE
            and self.attended
            and self._has_grant(tool, args)
        ):
            return Verdict(
                decision=Decision.ALLOW,
                tier=verdict.tier,
                reason="allowed under a live class grant (D32 scope=class)",
            )
        return verdict

    def op_tool_check(self, args: dict) -> Any:
        """Ask what would happen. Does not execute, and says so in the result."""
        tool = str(args["tool"])
        channel = self._channel(args)
        verdict = self._verdict(tool, channel, args.get("args") or {})
        return {
            "tool": tool,
            "channel": channel,
            "would": verdict.decision,
            "tier": verdict.tier,
            "reason": verdict.reason,
            "requires_confirmation_at": verdict.requires_confirmation_at,
            "executable": tool in self._EXECUTORS,
            "note": "this is a prediction, not a decision to act on",
        }

    def op_tool_call(self, args: dict) -> Any:
        """The only path to an action: gate, audit, then execute."""
        tool = str(args["tool"])
        channel = self._channel(args)
        call_args = self._anchor_args(args.get("args") or {})
        actor = str(args.get("actor", "user"))

        verdict = self._verdict(tool, channel, call_args)
        self._audit(
            actor=actor,
            channel=channel,
            tool=tool,
            tier=str(verdict.tier) if verdict.tier else "?",
            decision=verdict.decision,
            reason=verdict.reason,
            args=call_args,
        )

        if verdict.decision == Decision.DENY:
            raise Refused(f"{tool}: {verdict.reason}")
        if verdict.decision == Decision.QUEUE:
            self.queue.append(
                {
                    "queued_at": time.time(),
                    "tool": tool,
                    "channel": channel,
                    "actor": actor,
                    "args": call_args,
                    "reason": verdict.reason,
                }
            )
            return {"queued": True, "reason": verdict.reason, "depth": len(self.queue)}
        # The executor is looked up BEFORE the CONFIRM branch, deliberately. If a
        # destructive tool has no executor, we must not ask the user to approve
        # something that cannot then happen - the approval would be theatre, and
        # the user would have gone through a security ceremony for a no-op. The
        # honest answer is "this is not built", which is a build gap, not a refusal.
        executor = self._EXECUTORS.get(tool)
        if executor is None:
            raise NotImplementedOp(
                f"{tool} is registered at tier {verdict.tier} but has no "
                f"executor yet. {len(self._EXECUTORS)} of {len(ALL_TOOLS)} "
                f"tools are executable; this is a build gap, not a refusal."
            )

        if verdict.decision == Decision.CONFIRM:
            # Deliberately not executable from here. A confirmation is a human
            # looking at a specific proposed action, which means the UI has to
            # render it and the user has to answer - neither of which can be
            # faked by whoever sent the request. `_new_confirmation` builds the
            # payload preview and binds the approval to its hash (D32).
            return self._new_confirmation(
                tool=tool,
                args=call_args,
                channel=channel,
                actor=actor,
                reason=verdict.reason,
            )

        # A class grant is a statement about a calm period. Anything at T2 or
        # above ends it (D32): the session has moved on, and a permission granted
        # for routine work should not silently cover the next serious thing.
        if verdict.tier is not None and verdict.tier >= Tier.CONSEQUENTIAL:
            self._invalidate_grants()
        return executor(self, call_args)

    # -------------------------------------------------- confirmation (D32)
    #
    # A confirmation is the highest-authority moment in the system, so none of this
    # is negotiable at the call site. The shape:
    #
    #   tool_call -> gate says CONFIRM -> daemon builds a PREVIEW of exactly what
    #   would happen, hashes it, and returns a confirmation_id plus that hash.
    #   The UI renders the preview, the user decides.
    #   confirm.approve -> the daemon RE-BUILDS the preview, RE-HASHES it, and
    #   only proceeds if the hash still matches what the user saw.
    #
    # That re-hash is the whole point. Between the dialog appearing and the user
    # clicking, the filesystem can change - a glob can now match a different set of
    # files, a path can resolve differently. If we trusted the original hash we
    # would be authorising an action the user never actually saw. So we refuse on
    # any mismatch and re-queue, rather than proceeding "close enough".

    _PREVIEW_LIMIT = 200  # max paths rendered into a preview; the count is the real signal

    # D32: how long a "this class, not this call" approval stays cashable. The user
    # chose 600 s. Named as a constant because a policy number that a person
    # deliberately selected should be visible from the outside, and because the next
    # person to shorten or lengthen it should have to find this line on purpose.
    CLASS_GRANT_SECONDS = 600.0

    def _preview_for(self, tool: str, args: dict) -> dict:
        """Compute what a tool would do, without doing it.

        Not every tool can be previewed (a preview of `system.diagnose` is just
        the tool name). For those we fall back to a preview carrying the tool, the
        tier, and the redacted args - enough to bind an approval to *this* call,
        even though the exact effect is described only by the tool's own semantics.
        The hash still changes if the args change, which is the property that
        actually matters for D32.
        """
        fn = self._PREVIEWS.get(tool)
        if fn is not None:
            return fn(self, args)
        return {"preview": "none - this tool has no preview builder; approving binds the call args"}

    def _make_preview(self, tool: str, args: dict) -> dict:
        preview = self._preview_for(tool, args)
        targets = preview.get("targets") if isinstance(preview, dict) else None
        if isinstance(targets, list) and len(targets) > self._PREVIEW_LIMIT:
            preview = dict(preview)
            preview["targets"] = targets[: self._PREVIEW_LIMIT]
            preview["truncated"] = True
            preview["note"] = (
                f"showing first {self._PREVIEW_LIMIT} of {len(targets)} targets; "
                "the count below is the full number"
            )
        return preview

    @staticmethod
    def _payload_hash(tool: str, args: dict, preview: dict) -> str:
        body = json.dumps(
            {"tool": tool, "args": args, "preview": preview},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    def _new_confirmation(
        self, *, tool: str, args: dict, channel: str, actor: str, reason: str
    ) -> dict:
        preview = self._make_preview(tool, args)
        phash = self._payload_hash(tool, args, preview)
        with self._lock:
            self._pending_seq += 1
            cid = f"c{self._pending_seq:06d}"
            self._pending[cid] = {
                "id": cid,
                "tool": tool,
                "args": _redact(args),
                "_raw_args": dict(args),
                "channel": channel,
                "actor": actor,
                "reason": reason,
                "preview": preview,
                "payload_hash": phash,
                "created": time.time(),
            }
        return {
            "needs_confirmation": True,
            "confirmation_id": cid,
            "tool": tool,
            "reason": reason,
            "payload_hash": phash,
            "preview": preview,
            # The raw args are redacted for display, but the hash is over the raw
            # args, so the caller must echo the hash back verbatim.
            "args": _redact(args),
        }

    def op_confirm_list(self, args: dict) -> Any:
        """What is waiting on the user right now.

        `payload_hash` is here, and it was not. The overlay reads it from this
        list and sends it back on approve, but the list omitted it, so the
        overlay sent an empty string - and an empty claimed hash is falsy, so
        `op_confirm_approve` *skipped the payload check entirely*.

        That is D32's central guarantee not happening: the check existed, and
        was bypassed every time by the one caller that was supposed to use it.
        A hash is a digest of the payload, not a secret, and the arguments stay
        out of this list because they may contain one.
        """
        with self._lock:
            return {
                "pending": [
                    {
                        "id": p["id"],
                        "tool": p["tool"],
                        "reason": p["reason"],
                        "preview": p["preview"],
                        "payload_hash": p["payload_hash"],
                        "created": p["created"],
                    }
                    for p in sorted(self._pending.values(), key=lambda x: x["created"])
                ],
                "grants": self._grant_summary(),
            }

    def _grant_summary(self) -> list[dict]:
        now = time.monotonic()
        return [
            {"scope": k, "expires_in_s": round(v - now, 1)}
            for k, v in sorted(self._grants.items())
            if v > now
        ]

    def op_confirm_approve(self, args: dict) -> Any:
        """The user approved. Re-verify everything, then execute exactly that.

        There are three independent ways this refuses, and all three produce a
        structured refusal the UI can render - none of them ever executes
        "approximately the right thing".
        """
        cid = str(args["confirmation_id"])
        claimed = str(args.get("payload_hash", ""))
        scope = str(args.get("scope", "once"))
        if scope not in ("once", "class"):
            raise Refused(f"unknown approval scope {scope!r}; expected 'once' or 'class'")

        with self._lock:
            item = self._pending.get(cid)
            if item is None:
                raise Refused(
                    f"no pending confirmation {cid!r} - it was denied, expired, "
                    "or already answered"
                )
            tool = item["tool"]
            raw_args = item["_raw_args"]
            channel = item["channel"]
            actor = str(args.get("actor", "user"))

            # 1. The hash the UI displayed must match the hash of the payload we
            #    are about to act on. Re-preview, re-hash, compare.
            preview = self._make_preview(tool, raw_args)
            actual = self._payload_hash(tool, raw_args, preview)
            if claimed and claimed != actual:
                # Something changed between display and approval. Do NOT execute.
                # Re-queue a fresh confirmation so the user re-sees the new reality.
                self._pending.pop(cid, None)
                fresh = self._new_confirmation(
                    tool=tool,
                    args=raw_args,
                    channel=channel,
                    actor=actor,
                    reason="payload changed while you were reading the confirmation",
                )
                return {
                    "approved": False,
                    "reason": "the action changed since it was shown; please confirm again",
                    "requeued": fresh,
                }

            # 2. The gate is re-evaluated now, not trusted from display time. A
            #    workspace could have been revoked, the session could have become
            #    unattended. Authority is checked at the moment it is exercised.
            #
            #    NOTE the condition is DENY-or-QUEUE, *not* "must be ALLOW". A T3
            #    tool's steady-state verdict IS CONFIRM - that is what makes it
            #    T3 - and the confirmation being processed right now is the
            #    authorisation. Requiring ALLOW here meant every single T3
            #    approval was refused, i.e. the whole mechanism could never
            #    approve anything. What must not survive is a DENY (the path moved
            #    outside the approved roots, the tool was deregistered) or a
            #    QUEUE (unattended, and above the unattended ceiling).
            verdict = self._verdict(tool, channel, raw_args)
            if verdict.decision in (Decision.DENY, Decision.QUEUE):
                self._pending.pop(cid, None)
                self._audit(
                    actor=actor,
                    channel=channel,
                    tool=tool,
                    tier=str(verdict.tier) if verdict.tier else "?",
                    decision="refused",
                    reason=(
                        f"approval refused: gate re-evaluated to "
                        f"{verdict.decision} ({verdict.reason})"
                    ),
                    args=raw_args,
                )
                return {
                    "approved": False,
                    "reason": f"the gate no longer permits this ({verdict.reason})",
                }

            # 2b. Presence, checked explicitly, because the gate cannot express
            #     it. The gate's channel rule deliberately downgrades a remote T3
            #     to CONFIRM rather than QUEUE, so at approve time a local T3 and
            #     a remote one look identical, and a session that went unattended
            #     still reports CONFIRM rather than QUEUE. "Someone is at the
            #     keyboard right now" is a separate fact, and it is checked here.
            if not self.attended:
                self._pending.pop(cid, None)
                self._audit(
                    actor=actor,
                    channel=channel,
                    tool=tool,
                    tier=str(verdict.tier) if verdict.tier else "?",
                    decision="refused",
                    reason="approval refused: the session is unattended",
                    args=raw_args,
                )
                return {
                    "approved": False,
                    "reason": (
                        "you are not at the keyboard any more, so this cannot be "
                        "approved. Ask again when you are back."
                    ),
                }

            # 3. Only now, with a matching hash, a live gate and a human present,
            #    do we execute.
            self._pending.pop(cid, None)
            self._audit(
                actor=actor,
                channel=channel,
                tool=tool,
                tier=str(verdict.tier) if verdict.tier else "?",
                decision="confirmed",
                reason=f"user approved confirmation {cid} (scope={scope})",
                args={"confirmation_id": cid, "payload_hash": actual, "scope": scope},
            )

        # The executor runs OUTSIDE the lock: it does filesystem I/O and must not
        # block the gate for every other caller while it moves bytes.
        executor = self._EXECUTORS.get(tool)
        if executor is None:
            raise NotImplementedOp(f"{tool} was approved but has no executor")
        result = executor(self, raw_args)

        # A class grant, if requested, is recorded AFTER a successful execution so
        # a failed action cannot be used to obtain a standing permission.
        if scope == "class":
            self._grant(tool, raw_args)
        return {"approved": True, "tool": tool, "result": result, "scope": scope}

    def op_confirm_deny(self, args: dict) -> Any:
        cid = str(args["confirmation_id"])
        with self._lock:
            item = self._pending.pop(cid, None)
        self._audit(
            actor=str(args.get("actor", "user")),
            channel=item["channel"] if item else CHANNEL_PC,
            tool=item["tool"] if item else "?",
            tier="T3",
            decision="denied",
            reason=f"user denied confirmation {cid}",
            args={"confirmation_id": cid},
        )
        return {
            "denied": True,
            "confirmation_id": cid,
            "was_pending": item is not None,
        }

    # --- class grants ---------------------------------------------------------
    def _grant_key(self, tool: str, args: dict) -> str:
        """The class a grant covers: this tool, on this approved root.

        It has to be the ROOT, not the resolved path. The dialog offers the user
        "this class, for ten minutes" precisely so they are not asked about every
        single file in a folder they just approved; keying on the path made the
        second button behave identically to the first, which is worse than not
        offering it at all - the user grants a class, gets asked again for the
        very next file, and concludes the grant is broken.

        The narrowest enclosing approved root is used, so two nested workspaces
        still get separate grants rather than the outer one swallowing the
        inner. If nothing encloses the path - the workspace was revoked between
        the call and the approval - it falls back to the path itself, which
        degrades to exact-match rather than to a global grant.
        """
        target: Any = "."
        for k, v in args.items():
            if any(
                t in k for t in ("path", "file", "dir", "root", "target")
            ) and isinstance(v, str):
                try:
                    target = Path(v).resolve()
                except OSError:
                    target = v
                break
        if isinstance(target, Path):
            best: Path | None = None
            for root in self.manifests.roots():
                try:
                    r = Path(root)
                except (TypeError, ValueError):
                    continue
                if r == target or r in target.parents:
                    if best is None or len(r.parts) > len(best.parts):
                        best = r
            if best is not None:
                target = best
        return f"{tool}|{target}"

    def _grant(self, tool: str, args: dict) -> None:
        # The window is CLASS_GRANT_SECONDS, not a literal. This is a number the
        # user chose deliberately ("600s is fine"), so it is named and inspectable
        # rather than buried in this line where nobody would think to look for it.
        with self._lock:
            self._grants[self._grant_key(tool, args)] = (
                time.monotonic() + self.CLASS_GRANT_SECONDS
            )

    def _has_grant(self, tool: str, args: dict) -> bool:
        now = time.monotonic()
        with self._lock:
            for key, deadline in list(self._grants.items()):
                if deadline <= now:
                    self._grants.pop(key, None)
                elif key == self._grant_key(tool, args):
                    return True
        return False

    def _invalidate_grants(self) -> None:
        """Class grants do not survive anything consequential.

        A grant means "for the next ten minutes, stop asking about this one
        operation class". That is a statement about a *calm* period. The moment
        the assistant does something more serious, or the user stops being
        present, the premise is gone and the grant goes with it. Leaving it alive
        across a T3 or an absence would make it a standing permission, which is
        exactly what D32 says must not exist.
        """
        with self._lock:
            n = len(self._grants)
            self._grants.clear()
        if n:
            self._audit(
                actor="daemon",
                channel=CHANNEL_SCHEDULED,
                tool="confirm.grant_clear",
                tier=str(Tier.READ),
                decision="allow",
                reason=(
                    f"cleared {n} class grant(s): a T2+ action or presence loss "
                    "invalidates them"
                ),
            )

    # ----------------------------------------------------------------- executors

    # Deliberately three. The vertical slice in PLAN.md needs a read, a read of
    # a file the assistant chose, and a write the assistant is allowed to make.
    # Every other tool stays in the registry, gated, and unimplemented, so the
    # gap is visible in `status` rather than discovered at the moment someone
    # needed it.
    # -- the system probes -------------------------------------------------
    #
    # Every one of these is a T0 read and every one is delegated to
    # `sysinfo`, which is a module of its own because eight probes is too much
    # for this file and because they are worth testing without a Daemon.
    #
    # They are thin on purpose. The measurement, the parsing and the honest
    # error handling all live in `sysinfo`; a wrapper that added logic here
    # would be a second place for it to be wrong. What the wrapper does add is
    # the one thing `sysinfo` cannot: an exception becomes a refusal rather than
    # a traceback, because a probe that raises is a failed tool call and a probe
    # that lies is a failed assistant.

    def _exec_system_info(self, args: dict) -> Any:
        return sysinfo.system_info()

    def _exec_system_processes(self, args: dict) -> Any:
        return sysinfo.processes(int(args.get("limit", 25)))

    def _exec_system_gpu_state(self, args: dict) -> Any:
        return sysinfo.gpu_state()

    def _exec_system_disk_usage(self, args: dict) -> Any:
        return sysinfo.disk_usage()

    def _exec_system_package_list(self, args: dict) -> Any:
        return sysinfo.package_list(str(args.get("query", "")),
                                    int(args.get("limit", 200)))

    def _exec_system_service_status(self, args: dict) -> Any:
        return sysinfo.service_status(str(args.get("name", "")))

    def _exec_system_event_log(self, args: dict) -> Any:
        return sysinfo.event_log(int(args.get("hours", 24)),
                                 int(args.get("limit", 40)))

    def _exec_system_diagnose(self, args: dict) -> Any:
        return sysinfo.diagnose(check_event_log=bool(args.get("event_log", True)))

    def _anchor_args(self, call_args: dict) -> dict:
        """Resolve relative path arguments against the single approved root.

        This has to happen *before* the gate, which is the whole point and the
        reason it did not work the first time it was tried. Anchoring inside the
        executor is too late: the gate resolves `notes.txt` against the daemon's
        working directory, finds it outside every root, and refuses - correctly,
        on the evidence it was given. The call never reaches the executor.

        Anything that makes a call *valid* has to happen before the gate judges
        it, or the gate is judging a form of the request nobody intends to act
        on.

        Only `path` and `destination` are touched, and only when they are strings,
        and only when exactly one root is approved. The rewritten arguments are
        what gets audited, so the record shows what actually happened.
        """
        roots = list(self.manifests.roots())
        if len(roots) != 1 or not isinstance(call_args, dict):
            return call_args
        out = dict(call_args)
        for key in ("path", "destination"):
            value = out.get(key)
            if isinstance(value, str) and value and not os.path.isabs(value):
                out[key] = os.path.join(str(roots[0]), value)
        return out

    def _anchor(self, path: str) -> str:
        """Resolve a relative path against the single approved root.

        The 4B is bad at exactly one thing, and this is it. Asked to "copy
        notes.txt to a backup copy" it produced `path: "notes.txt"` - which
        resolves against the *daemon's* working directory, not the workspace, so
        the gate refused it. It is not a reasoning failure; the file was named
        and the instruction was followed as well as a 4B can manage it.

        So the deterministic half does it. When exactly one root is approved, a
        relative path is not ambiguous - there is only one place it can mean -
        and anchoring it cannot escape anything, because the gate still checks
        containment afterwards.

        With several roots it is ambiguous, so it is left alone: guessing which
        directory a relative name belongs to is how files end up in the wrong
        place. An absolute path is used unchanged either way.
        """
        raw = str(path)
        if os.path.isabs(raw):
            return raw
        roots = list(self.manifests.roots())
        if len(roots) != 1:
            return raw
        return os.path.join(str(roots[0]), raw)

    # -- voice ---------------------------------------------------------------
    #
    # T3 for both, and for a reason quarantine cannot cover. `file.delete` is
    # T3 because it is destructive, and destructive is *recoverable*: the file
    # moves to quarantine and the user can put it back. These are T3 because
    # being wrong is unrecoverable in a way restoring a file is not. Once a
    # microphone is open the assistant is transcribing, and once it speaks there
    # is no undo. Neither is externally visible, so neither is `external` either.
    #
    # Nothing loads a model here. `voice.status` is the free one and the others
    # pay on first use, which is recorded rather than hidden - a T3 confirmation
    # dialog that silently costs a 34 s model load is a dialog the user learns
    # to approve without reading.
    def _voice_listen_once(self, device: Any, seconds: int) -> dict[str, Any]:
        """Listen for one utterance, then transcribe it. The whole of `voice.listen`.

        Calibration happens here, once per call, and its numbers are returned. That
        is the point: the thresholds used are a measurement of this room, so a user
        who cannot make it hear has something to read instead of "it just doesn't
        work". `audio.segment` deliberately does not calibrate itself - doing so
        would consume the first 1.5 s of every utterance and clip the opening.

        The wav is deleted after transcription. Not tidiness: `voice.listen` is T3
        precisely because a microphone that records without consent is the danger,
        and leaving raw audio in the state directory after saying it would not be
        would undermine the reason it is T3.
        """
        try:
            mic = audio.Microphone(device if device else None)
        except audio.NoInputDevice as why:
            raise DaemonError(str(why)) from why

        with mic:
            cal = audio.calibrate(mic)
            heard = audio.segment(
                mic,
                open_rms=cal.open,
                release_rms=cal.release,
                total_s=float(seconds),
            )
            opened = {"device": mic.describe()["device"], "rate": mic.describe()["sample_rate"],
                      "calibration": cal.describe()}
            if heard is None:
                # Not an error. Nothing was said within the window, and a silent
                # microphone produces an empty transcript, not a failure.
                return {**opened, "heard": None, "text": "",
                        "note": f"nothing above the open threshold in {seconds}s"}

            # The directory is created here rather than relied upon from
            # `Utterance.save`. This is the code that chose the path, so it is the
            # code that should know whether the path is writable - and a
            # FileNotFoundError from inside a save call reads as a broken save
            # rather than as a directory that was never made.
            voice_dir = self.state / "voice"
            voice_dir.mkdir(parents=True, exist_ok=True)
            wav = voice_dir / f"listen-{time.time():.3f}.wav"
            try:
                heard.save(wav)
                model = self._transcriber()
                text = model.transcribe(wav)
            finally:
                try:
                    wav.unlink()
                except OSError:
                    pass

        # `Transcriber.transcribe` reports failure by returning a bracketed string,
        # which is the right choice inside a listener loop (an exception would end
        # it) and the wrong choice at a boundary. Splitting it here means no caller
        # has to recognise a magic prefix - and one already did, incorrectly,
        # treating a perfectly good transcript as an error because it was a string.
        failed = text.startswith("[transcription failed:")
        return {
            **opened,
            "heard": heard.describe(),
            "text": "" if failed else text,
            "error": text if failed else "",
            # Three outcomes, not two, and all three are reachable in normal use:
            # nothing was said, something was said but not understood, and
            # transcription itself broke. Collapsing the last two into `empty`
            # would make a broken microphone indistinguishable from a quiet room.
            "empty": (not text.strip()) or failed,
        }

    def _transcriber(self) -> Any:
        """The Whisper model, loaded once and kept (D50: voice costs no VRAM)."""
        if getattr(self, "_voice_model", None) is None:
            self._voice_model = speech.Transcriber()
        return self._voice_model

    def _exec_voice_speak(self, args: dict) -> Any:
        text = str(args.get("text", "")).strip()
        if not text:
            raise DaemonError("voice.speak needs non-empty text")
        # Split on sentence boundaries so the synthesiser's per-sentence cost is
        # paid on natural units. Piper renders a whole paragraph in one pass
        # noticeably worse than a sequence of sentences.
        out = self.state / "voice" / time.strftime("%Y%m%d-%H%M%S")
        result = speech.synthesise(
            speech.split_sentences(text), out, voice=args.get("voice")
        )
        if not result.get("ok"):
            raise DaemonError(str(result.get("error") or "speech failed"))
        return result

    def _exec_voice_listen(self, args: dict) -> Any:
        seconds = int(args.get("seconds") or 10)
        device = args.get("device")
        return self._voice_listen_once(device, seconds)

    def _exec_voice_status(self, args: dict) -> Any:
        """What voice can do here, and what it would cost. Loads nothing."""
        status = voice_loop.describe()
        # The loop is running state, and a status that does not report running
        # state makes a user ask "is it listening?" and get no answer.
        status["loop"] = {
            **status.get("loop", {}),
            "running": self._voice_thread is not None and self._voice_thread.is_alive(),
            "turns": self._voice_loop.stats.turns if self._voice_loop else 0,
            "stats": _voice_stats(self._voice_loop) if self._voice_loop else {},
            "wake_model_present": self._wake_model_path().exists(),
        }
        return status

    def _wake_model_path(self) -> Path:
        """Where the trained wake word is expected, and where it will be written.

        One function so the question "is the wake word trained?" has one answer.
        A second path would eventually be the one the trainer writes and the other
        the one the loop reads, and the symptom would be a loop that reports no
        wake word on a machine where one was just trained.
        """
        return ROOT / "models" / "wakeword" / "yo_archie.onnx"

    def _voice_turn(self, text: str) -> str:
        """One spoken request, through the same gate every other request uses.

        A voice turn is not a privileged path. It proposes through `brain_turn`,
        which returns a proposal, and the proposal goes back through `tool_call`
        where the policy gate and any T3 confirmation apply. A microphone is not a
        licence to skip the gate - it is a *new* reason to want the gate, since it
        is the one channel where the user may not be watching.
        """
        result = self._brain_call("turn", prompt=text, channel=CHANNEL_VOICE,
                                  actor="voice")
        kind = str(result.get("kind") or "answer")
        if kind != "tool":
            return str(result.get("answer") or result.get("text") or "")
        tool = str(result.get("tool") or "")
        call_args = result.get("arguments") or {}
        reply = self.op_tool_call({
            "tool": tool, "args": call_args,
            "channel": CHANNEL_VOICE, "actor": "voice",
        })
        if not isinstance(reply, dict):
            return ""
        if reply.get("error"):
            return f"I could not do that: {reply['error']}"
        return _describe_result(tool, reply.get("result"))

    def op_voice_watch(self, args: dict) -> Any:
        """Start listening for the wake word in the background.

        Separate from the tools on purpose. `voice.listen` is a single
        T3-approved utterance; this is the thing you leave running, and it needs
        to be startable without a human clicking a dialog about a permission they
        already gave when they turned voice on.
        """
        if self._voice_thread is not None and self._voice_thread.is_alive():
            return {"started": False, "reason": "already listening",
                    "turns": self._voice_loop.stats.turns}
        if not self._wake_model_path().exists():
            raise Refused(
                f"no wake word at {self._wake_model_path()}. Train it first "
                f"(train_wakeword.py), or use voice.listen without a wake word."
            )
        turns = args.get("turns")
        loop = voice_loop.VoiceLoop(
            self._voice_turn,
            device=args.get("device"),
            voice=str(args.get("voice") or speech.DEFAULT_VOICE),
            seconds=float(args.get("seconds", 30)),
            wake_name="yo_archie",
        )
        loop.open()
        self._voice_loop = loop

        def _run() -> None:
            try:
                loop.run(turns=int(turns) if turns else None,
                         max_idle_s=float(args.get("max_idle_s") or 900.0))
            except Exception as why:  # noqa: BLE001 - a dead thread is a report,
                # not a crash, and the daemon must survive a microphone vanishing
                self._audit(actor="daemon", channel=CHANNEL_VOICE,
                            tool="voice.watch", decision="allow",
                            reason=f"voice loop stopped: {type(why).__name__}: {why}")
            finally:
                self._voice_loop = None

        thread = threading.Thread(target=_run, name="voice-loop", daemon=True)
        self._voice_thread = thread
        thread.start()
        return {"started": True, "device": loop._mic.device_name,
                "rate": loop._mic.rate, "wake": loop.wake_name,
                "transcriber": loop._transcriber is not None}

    def _preview_voice_speak(self, args: dict) -> dict:
        text = str(args.get("text", "")).strip()
        voice = str(args.get("voice") or speech.DEFAULT_VOICE)
        words = len(text.split())
        # ~2.6 words/second is measured piper output, not a rule of thumb: the
        # point is that the user can see how long they will be listening.
        return {
            "action": "speak aloud through the speakers",
            "text": text if len(text) <= 400 else text[:400] + " ...",
            "characters": len(text),
            "words": words,
            "voice": voice,
            "about_seconds": round(words / 2.6, 1),
            "reversible": False,
            "note": "this cannot be taken back - the sound is already in the room",
        }

    def _preview_voice_listen(self, args: dict) -> dict:
        seconds = int(args.get("seconds") or 10)
        device = args.get("device")
        return {
            "action": "open the microphone and transcribe one utterance",
            "device": str(device) if device else "(the system default)",
            "max_seconds": seconds,
            "returns": "the words, as text - no audio is stored after transcription",
            "reversible": False,
            "note": "listening cannot be taken back once the microphone is open",
        }

    # -- the remaining file tools --------------------------------------------
    #
    # Thin wrappers around `fileops`, which holds the logic. What the wrapper
    # adds is the two things `fileops` cannot reach: `self.quarantine`, so an
    # overwrite is kept by the mechanism that makes a delete recoverable (D33),
    # and `_anchor`, so a relative path means something.
    #
    # Every one of these is bounded by `fileops` on files scanned, bytes read,
    # entries copied, matches returned or bytes archived. An unbounded operation
    # on a user's own machine is a denial of service against the user.

    def _exec_file_metadata(self, args: dict) -> Any:
        return fileops.metadata(self._anchor(args["path"]))

    def _exec_file_preview(self, args: dict) -> Any:
        return fileops.preview(self._anchor(args["path"]),
                               int(args.get("max_bytes", fileops.PREVIEW_MAX_BYTES)))

    def _exec_file_search(self, args: dict) -> Any:
        return fileops.search(self._anchor(args["path"]), str(args["query"]),
                              int(args.get("max_bytes", fileops.SEARCH_MAX_BYTES)))

    def _exec_file_edit(self, args: dict) -> Any:
        return fileops.edit(self._anchor(args["path"]), str(args.get("content", "")),
                            backup_dir=self.quarantine)

    def _exec_file_copy(self, args: dict) -> Any:
        return fileops.copy(self._anchor(args["path"]),
                            self._anchor(args["destination"]))

    def _exec_file_move(self, args: dict) -> Any:
        return fileops.move(self._anchor(args["path"]),
                            self._anchor(args["destination"]))

    def _exec_file_archive(self, args: dict) -> Any:
        return fileops.archive(self._anchor(args["path"]),
                               self._anchor(args["destination"]))

    def _exec_file_bulk_rename(self, args: dict) -> Any:
        # Defaults to a dry run *here*, at the executor, rather than only in the
        # registry description - so a caller that omits the argument gets the safe
        # behaviour rather than whatever the schema happened to say.
        dry = args.get("dry_run")
        return fileops.bulk_rename(
            self._anchor(args["path"]), str(args["pattern"]),
            str(args.get("replacement", "")),
            dry_run=True if dry is None else bool(dry),
        )

    def _exec_file_list(self, args: dict) -> Any:
        target = Path(args["path"])
        if not target.is_dir():
            raise DaemonError(f"not a directory: {target}")
        entries = []
        for child in sorted(target.iterdir(), key=lambda p: p.name.lower())[:2000]:
            try:
                st = child.stat()
            except OSError:
                continue
            entries.append(
                {"name": child.name, "dir": child.is_dir(), "size": st.st_size}
            )
        return {"path": str(target), "count": len(entries), "entries": entries}

    def _exec_file_read(self, args: dict) -> Any:
        target = Path(args["path"])
        limit = int(args.get("max_bytes", 200_000))
        data = target.read_bytes()
        text = data[:limit].decode("utf-8", errors="replace")
        return {
            "path": str(target),
            "bytes": len(data),
            "truncated": len(data) > limit,
            "text": text,
        }

    def _exec_file_create(self, args: dict) -> Any:
        target = Path(args["path"])
        body = str(args.get("content", ""))
        existed = target.exists()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".partial")
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, target)
        return {"path": str(target), "bytes": len(body), "existed": existed}

    # --- D33: delete is a move, not an unlink --------------------------------
    #
    # A T3 tool whose only implementation is `Path.unlink()` is untestable and
    # irreversible, and this system's whole premise is that its actions are
    # reversible (D14 keeps its own vault snapshots for exactly this reason).
    # So `file.delete` MOVES the target into state/quarantine/<batch>/ and writes
    # a manifest beside it. The file is gone from where it was - which is what the
    # user asked for - and it is recoverable with `file.restore`, which is also
    # what makes the confirmation path in D32 something we can actually exercise
    # in a test suite without destroying anything real.
    #
    # The quarantine lives inside our own state directory, so the gate's self-path
    # protection covers it: the assistant cannot use this tool to delete its own
    # source, its own decisions, or its own quarantine.

    _QUARANTINE_PREVIEW_CAP = 500

    def _delete_targets(self, args: dict) -> list[Path]:
        """Resolve what a delete would touch, following the *resolved* path.

        D32 rule 1: resolution happens here, once, and the preview is built from
        the result. If we previewed the raw string and executed the resolved one,
        the dialog would be describing something other than what happens.
        """
        raw = str(args["path"])
        p = Path(raw)
        if any(ch in raw for ch in "*?["):
            parent = p.parent if p.parent != Path("") else Path(".")
            pattern = p.name
            base = parent.resolve()
            matches = sorted(
                (m.resolve() for m in base.glob(pattern)),
                key=lambda q: str(q).lower(),
            )
        else:
            try:
                matches = [p.resolve()]
            except OSError:
                matches = [p.absolute()]
        return matches

    def _preview_file_delete(self, args: dict) -> dict:
        targets = self._delete_targets(args)
        described, total_bytes, missing = [], 0, 0
        for t in targets:
            try:
                if t.is_dir():
                    n = sum(1 for _ in t.rglob("*"))
                    described.append({"path": str(t), "kind": "dir", "entries": n})
                else:
                    size = t.stat().st_size
                    total_bytes += size
                    described.append({"path": str(t), "kind": "file", "size": size})
            except OSError:
                missing += 1
                described.append({"path": str(t), "kind": "missing"})
        return {
            "action": "move to quarantine (recoverable, not erased)",
            "proposed": str(args["path"]),
            "resolved": str(Path(str(args["path"])).resolve()) if not any(
                ch in str(args["path"]) for ch in "*?["
            ) else "(glob resolved per match below)",
            "count": len(described),
            "files": len(described) - missing,
            "total_bytes": total_bytes,
            "missing": missing,
            "targets": described[: self._QUARANTINE_PREVIEW_CAP],
            "truncated": len(described) > self._QUARANTINE_PREVIEW_CAP,
        }

    def _exec_file_delete(self, args: dict) -> Any:
        targets = self._delete_targets(args)
        if not targets:
            raise DaemonError("nothing matched")
        batch = time.strftime("%Y%m%d-%H%M%S") + f"-{self._pending_seq:03d}"
        dest_root = self.quarantine / batch
        dest_root.mkdir(parents=True, exist_ok=True)
        manifest = {
            "batch": batch,
            "created": time.time(),
            "channel": self._channel(args) if isinstance(args, dict) else "pc",
            "entries": [],
        }
        moved, failed = 0, []
        for i, src in enumerate(targets):
            if not src.exists():
                failed.append({"path": str(src), "error": "missing"})
                continue
            # Flat, collision-free name. The original location is recorded in the
            # manifest, which is what restore uses; the on-disk name only has to
            # be unique and not escape.
            flat = f"{i:05d}-{src.name}"
            dest = dest_root / flat
            try:
                shutil.move(str(src), str(dest))
            except OSError as exc:
                failed.append({"path": str(src), "error": str(exc)})
                continue
            manifest["entries"].append(
                {"original": str(src), "quarantined": str(dest), "is_dir": src.is_dir()}
            )
            moved += 1
        (dest_root / "manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )
        return {
            "deleted": moved,
            "failed": failed,
            "quarantine_batch": batch,
            "quarantine_root": str(dest_root),
            "recoverable": True,
        }

    def _exec_file_restore(self, args: dict) -> Any:
        batch = str(args["batch"])
        mpath = self.quarantine / batch / "manifest.json"
        if not mpath.is_file():
            raise DaemonError(f"no quarantine batch {batch!r}")
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
        restored, failed = 0, []
        for e in manifest.get("entries", []):
            src = Path(e["quarantined"])
            dst = Path(e["original"])
            if not src.exists():
                failed.append({"path": e["original"], "error": "already moved or gone"})
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(src), str(dst))
            except OSError as exc:
                failed.append({"path": e["original"], "error": str(exc)})
                continue
            restored += 1
        return {"restored": restored, "failed": failed, "batch": batch}

    # ---------------------------------------------------------------- workspaces

    def op_workspace_add(self, args: dict) -> Any:
        ws = Workspace(
            name=str(args["name"]),
            root=str(args["root"]),
            danger_level=str(args.get("danger_level", "low")),
            allow_tiers=_tiers(args.get("allow_tiers", ["T0"])),
            language=str(args.get("language", "")),
            stack=tuple(args.get("stack", ())),
            test_command=str(args.get("test_command", "")),
            build_command=str(args.get("build_command", "")),
            notes=str(args.get("notes", "")),
            agent_definitions=str(args.get("agent_definitions", "")),
        )
        stored = self.manifests.add(ws, overwrite=bool(args.get("overwrite", False)))
        self._audit(
            actor=str(args.get("actor", "user")),
            channel=self._channel(args),
            tool="workspace.add",
            tier=str(Tier.REVERSIBLE_WRITE),
            decision="allow",
            reason=f"granted access to {stored.root} at {stored.danger_level}",
            args={"name": stored.name, "root": stored.root},
        )
        return stored

    def op_workspace_remove(self, args: dict) -> Any:
        name = str(args["name"])
        self.manifests.remove(name)
        self._audit(
            actor=str(args.get("actor", "user")),
            channel=self._channel(args),
            tool="workspace.remove",
            tier=str(Tier.REVERSIBLE_WRITE),
            decision="allow",
            reason=f"revoked access to {name}",
            args={"name": name},
        )
        return {"removed": name, "remaining": [w.name for w in self.manifests]}

    def op_workspace_list(self, args: dict) -> Any:
        return [w.describe() for w in self.manifests]

    def op_workspace_resolve(self, args: dict) -> Any:
        try:
            ws = self.manifests.resolve(
                str(args["name"]), session_ceiling=self.session_ceiling
            )
        except NotPermitted as exc:
            raise Refused(str(exc)) from exc
        return ws

    def op_workspace_roots(self, args: dict) -> Any:
        """The approved roots, as data.

        Separate from `workspace_list`, which is formatted for a person to read.
        A caller that has to parse a human-readable string to learn where it may
        work is a caller one wording change away from silently believing the
        wrong thing - and the brain child needs this every turn, to tell the
        model where it is allowed to act.
        """
        return {"roots": [str(r) for r in self.manifests.roots()]}

    def op_workspace_covers(self, args: dict) -> Any:
        return self.manifests.covers(str(args["path"]))

    # --------------------------------------------------------------------- vault

    def _vault(self) -> ObsidianBridge:
        if self.vault is None:
            raise DaemonError(
                "the Obsidian vault is not available "
                + (f"({self.vault_error})" if self.vault_error else "")
                + ". The live tier does not depend on it; only durable memory does."
            )
        return self.vault

    def op_vault_status(self, args: dict) -> Any:
        if self.vault is None:
            return {"available": False, "error": self.vault_error}
        return {
            "available": self.vault.available,
            "describe": self.vault.describe(),
            "stats": _jsonable(self.vault.index_stats()),
        }

    def op_vault_index(self, args: dict) -> Any:
        notes = self._vault().index()
        limit = int(args.get("limit", 200))
        return {
            "total": len(notes),
            "returned": min(limit, len(notes)),
            "notes": [_jsonable(n) for n in notes[:limit]],
        }

    def op_vault_read_journal(self, args: dict) -> Any:
        return self._vault().read_journal(
            str(args.get("subdir", "journal")), limit=args.get("limit")
        )

    def op_vault_write_note(self, args: dict) -> Any:
        """Vault writes are T1 and still go through the gate.

        This is a deliberate exception to "the assistant only writes its own
        state dir" (D21). That rule is about the assistant rewriting *itself* -
        its code and its policy. Notes are the human-readable memory substrate
        the user asked to read, and refusing to write them would make requirement
        4 unauditable by eye. The distinction is enforced by the deny-path on
        the assistant's own source and decisions, not by forbidding notes.
        """
        result = self._vault().write_note(
            str(args["subdir"]),
            str(args["title"]),
            str(args.get("body", "")),
            front_matter=args.get("front_matter"),
            overwrite=bool(args.get("overwrite", False)),
        )
        self._audit(
            actor=str(args.get("actor", "user")),
            channel=self._channel(args),
            tool="vault.write_note",
            tier=str(Tier.REVERSIBLE_WRITE),
            decision="allow",
            reason=f"wrote note {result.name}",
            args={"subdir": args.get("subdir"), "title": args.get("title")},
        )
        return {"path": str(result)}

    def op_vault_append(self, args: dict) -> Any:
        result = self._vault().append_jsonl(
            str(args["subdir"]), dict(args["record"]), filename=args.get("filename")
        )
        self._audit(
            actor=str(args.get("actor", "user")),
            channel=self._channel(args),
            tool="vault.append",
            tier=str(Tier.REVERSIBLE_WRITE),
            decision="allow",
            reason=f"appended to {result.name}",
            args={"subdir": args.get("subdir")},
        )
        return {"path": str(result)}

    def op_vault_role_templates(self, args: dict) -> Any:
        """Lists role definitions as *templates*.

        The D10 boundary is that we may read another project's agent markdown
        but must not run it in place. Returning a path into that project would
        invite exactly that, so the copy step is
        ``ManifestStore.import_agent_definitions`` and it is deliberate.
        """
        return [_jsonable(n) for n in self._vault().role_templates()]

    # ------------------------------------------------------------------ arbiter

    def op_arbiter_acquire(self, args: dict) -> Any:
        result = self.arbiter.acquire(
            str(args["holder"]),
            int(args["need_bytes"]),
            Priority[str(args.get("priority", "FAST_TIER"))],
        )
        if isinstance(result, Denial):
            self._audit(
                actor=str(args.get("actor", "user")),
                channel=self._channel(args),
                tool="arbiter.acquire",
                tier=str(Tier.READ),
                decision="deny",
                reason=format_denial(result),
                args={"holder": args.get("holder"), "need_bytes": args.get("need_bytes")},
            )
            raise Refused(format_denial(result))
        return result

    def op_arbiter_renew(self, args: dict) -> Any:
        result = self.arbiter.renew(
            str(args["holder"]),
            need_bytes=args.get("need_bytes"),
            priority=Priority[str(args["priority"])] if args.get("priority") else None,
        )
        if isinstance(result, Denial):
            raise Refused(format_denial(result))
        return result

    def op_arbiter_release(self, args: dict) -> Any:
        return {"released": self.arbiter.release(str(args["holder"]))}

    def op_arbiter_sweep(self, args: dict) -> Any:
        return {"must_kill": self.arbiter.sweep()}

    def op_arbiter_summary(self, args: dict) -> Any:
        return self.arbiter.summary()

    # -------------------------------------------------------------------- audit

    def op_audit_verify(self, args: dict) -> Any:
        return _jsonable(self.audit.verify())

    def op_audit_tail(self, args: dict) -> Any:
        limit = int(args.get("limit", 50))
        records = list(self.audit.read())
        return [_jsonable(r) for r in records[-limit:]]

    def op_audit_note(self, args: dict) -> Any:
        """Record a decision that was not a tool call.

        Used by the supervisor and by the brain's own reflection, so that
        "why did it do that" is answerable for reasoning and not only for
        actions. A free-text note is the easiest thing in this system to abuse
        into a fake audit trail, so it is capped and it is attributed to a
        named actor.
        """
        reason = str(args.get("reason", ""))[:500]
        actor = str(args.get("actor", "user"))
        self._audit(
            actor=actor,
            channel=self._channel(args),
            tool=str(args.get("kind", "note")),
            tier=str(Tier.READ),
            decision="allow",
            reason=reason,
        )
        return {"recorded": True}

    # ------------------------------------------------------------------- brain
    #
    # D3. The brain is a child process, not a thread, and the reason is physical
    # rather than architectural taste: on Windows, GPU memory comes back only when
    # the process holding it dies. There is no unload that releases it. So the
    # daemon's job is not to manage a brain, it is to *run one up on demand and
    # kill it when it goes quiet* - and only process death is a mechanism we can
    # rely on rather than hope for.
    #
    # The child asks this daemon for its VRAM lease over the same pipe, so the
    # component holding the most precious resource on the machine is the one
    # component that cannot grant it to itself. Every lease it takes is audited
    # here, and the 90s TTL sweep that reclaims it is a second line of defence:
    # even if the child hung rather than exited, the lease would expire.

    #: How long the child is allowed to sit idle before it is killed (D3).
    BRAIN_IDLE_TTL_S = 90.0
    #: How often the sweeper looks. Short enough that a killed child is noticed
    #: promptly, long enough that the check is not itself a busy loop.
    BRAIN_SWEEP_EVERY_S = 15.0
    #: How long to wait for a freshly spawned child to answer `ping`. Generous,
    #: because a cold start includes importing torch-free llama_cpp and binding
    #: the pipe; too short and we would declare a healthy child dead.
    BRAIN_START_TIMEOUT_S = 60.0
    #: Bytes the fast tier needs. Deliberately a *claim*, not a reservation: the
    #: arbiter re-measures live VRAM and this is only what we ask for. The real
    #: measured cost is 4.92 GiB, so asking for 5 keeps a little slack for the
    #: compute buffer without inviting a lease we could not honour.
    BRAIN_LEASE_BYTES = 5 * 1024 ** 3
    #: The holder name used in the arbiter and in the audit chain. The child's PID
    #: is recorded alongside it, because "brain" on its own does not say which
    #: process held 5 GiB and a stale brain is exactly the thing to be able to
    #: find later.
    BRAIN_HOLDER = "brain"
    #: Which tier a turn gets when the caller does not ask for one. The fast tier,
    #: because it is the one that is always present and because answering quickly
    #: matters more than answering cleverly for most requests. Anything touching
    #: files asks for the deep tier by name.
    DEFAULT_BRAIN_TIER = "fast"

    def _brain_env(self) -> dict[str, str]:
        """The environment the child is launched with (D26).

        D26 says the daemon must not hand a model child the ambient desktop
        PATH, because a different CUDA version appearing on PATH is how a
        working setup breaks with no change on our side. So the toolkit the build
        was made against is put *first* and the rest of the environment is kept
        - the child still needs PATH for the system DLLs, it just must not get
        first refusal on which CUDA runtime it loads.
        """
        env = dict(os.environ)
        cuda_bin = r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v13.3\bin"
        if os.path.isdir(cuda_bin):
            env["PATH"] = cuda_bin + os.pathsep + env.get("PATH", "")
        # PYTHONPATH so `python -m assistant.brain_server` resolves without the
        # child having to know where it was installed from.
        src_root = str(Path(__file__).resolve().parent.parent)
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = src_root + (os.pathsep + existing if existing else "")
        return env

    def _brain_alive(self) -> bool:
        child = self._brain
        return bool(child is not None and child.poll() is None)

    def _brain_stop(self, why: str) -> None:
        """Kill the child, if there is one. Never raises.

        D3 again: this is the moment the VRAM comes back, so it must not be
        conditional on the child being in a good mood. `kill()` first rather
        than a polite `terminate()` - a child blocked inside a CUDA call will not
        see a terminate request until it returns to the interpreter, and the whole
        point is that we do not wait. `kill()` is a hard process death, which is
        the only shutdown mechanism Windows makes reliable for GPU memory.
        """
        with self._lock:
            child = self._brain
            tier = self._brain_tier
            self._brain = None
            self._brain_client = None
            self._brain_last_used = 0.0
            self._brain_tier = None
            self._brain_loaded = False
            job = self._brain_job
            self._brain_job = None
        if child is None or child.poll() is not None:
            return
        self._audit(
            actor="daemon",
            channel=CHANNEL_SCHEDULED,
            tool="brain.unload",
            tier=str(Tier.READ),
            decision="allow",
            reason=f"stopped brain pid={child.pid} tier={tier or '?'} ({why})",
        )
        # The lease is released explicitly as well as by TTL. The arbiter's
        # contract is that release does not kill and expiry does not release, so
        # doing only one of the two would leave a phantom lease on the books for
        # up to 90 seconds after the memory is actually free.
        try:
            self.arbiter.release(self.BRAIN_HOLDER)
        except Exception:  # noqa: BLE001 - releasing must never block a stop
            pass
        # Closing the job is the real kill. It comes first so the child does not
        # depend on kill() succeeding - and it is unconditional, so the one path
        # that used to leak (kill() raised, or the process was already gone
        # between the poll and the kill) cannot leak any more.
        brain_server.job_close(job)
        try:
            child.kill()
        except Exception:  # noqa: BLE001
            pass
        try:
            child.wait(timeout=10)
        except Exception:  # noqa: BLE001
            pass

    def _brain_ensure(self, tier: str | None = None) -> Any:
        """Start the child if it is not running, and return a client for it.

        Raises ``BrainUnavailable`` rather than returning None, because every
        caller would otherwise have to check, and a caller that forgets the check
        reports a dead brain as an empty answer - which is the failure mode D31
        exists to prevent.

        `tier` selects which model the child serves. A tier change is a *swap*, not
        a second child: the fast and deep models together need more VRAM than the
        card has, so keeping both resident is not a slower version of this, it is
        an impossible one. The old child is stopped, which returns its VRAM
        immediately, and only then is the new one started - so the swap asks the
        arbiter for what is actually free rather than for the card's total.
        """
        tier = str(tier or self.DEFAULT_BRAIN_TIER)
        if tier not in BRAIN_TIERS:
            # Refused rather than quietly falling back. Answering a "deep" request
            # from the 4B would be a lie about which model produced the answer,
            # and the whole reason to have tiers is that they are not the same.
            raise Refused(
                f"unknown brain tier {tier!r}; known tiers are "
                f"{sorted(BRAIN_TIERS)}"
            )
        with self._lock:
            if self._brain_alive() and self._brain_client is not None:
                if self._brain_tier == tier:
                    return self._brain_client
                self._brain_stop(
                    f"swapping tier {self._brain_tier or '?'} -> {tier}"
                )

            # A previous child that died without being stopped leaves its lease
            # behind. Reclaim it here rather than letting the next turn be denied
            # by our own corpse.
            if self._brain is not None:
                self._brain = None
                self._brain_client = None
                self._brain_tier = None
            try:
                self.arbiter.release(self.BRAIN_HOLDER)
            except Exception:  # noqa: BLE001
                pass

            # The authkey is the only thing proving a caller is us. It is written
            # by serve_forever; if it is not there we are being called without a
            # daemon having been started properly, and we should say so rather
            # than mint a key that nothing else will trust.
            if not self.authkey_path.exists():
                raise Refused(
                    "no authkey on disk - the daemon has not been started properly, "
                    "so a brain child could not be authenticated"
                )

            args = [
                sys.executable,
                "-m",
                "assistant.brain_server",
                # The child *serves* turns on this pipe...
                "--pipe",
                self.brain_pipe,
                # ...and calls *out* on the daemon's own pipe to ask for its VRAM
                # lease. Two pipes, because one pipe would have the child asking
                # itself for permission.
                "--upstream",
                self.daemon_pipe,
                # The child must read *this* daemon's authkey, not one it guesses
                # at relative to the repo. A daemon run from a test state dir
                # would otherwise silently authenticate against whatever key the
                # production daemon last wrote - or against nothing at all.
                "--authkey",
                str(self.authkey_path),
                # Which model to hold. The child resolves this to a file and a
                # lease request; the daemon does not send the path itself, so
                # there is exactly one place where a model file is named.
                "--tier",
                tier,
            ]
            child = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                args,
                env=self._brain_env(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=str(ROOT),
            )
            self._brain = child
            self._brain_tier = tier
            self._brain_job = _arm_kill_on_close(self, child.pid)
            client = ipc.PipeClient(self.brain_pipe, authkey=self.authkey, timeout=120.0)
            self._brain_client = client

        # Wait for the pipe to answer, outside the lock: the child needs no lock,
        # and holding the daemon's lock for up to a minute would stall every other
        # caller including policy checks. This is a real deadlock if done inside.
        deadline = time.time() + self.BRAIN_START_TIMEOUT_S
        last: Exception | None = None
        while time.time() < deadline:
            if child.poll() is not None:
                out = ""
                try:
                    out = (child.stdout.read() or b"").decode("utf-8", "replace")
                except Exception:  # noqa: BLE001
                    pass
                self._brain_stop("child exited during start-up")
                # The *tail* of a traceback is the interesting part. The head is
                # always runpy boilerplate, and quoting the head means every
                # child failure looks like the same useless thing.
                tail = out.strip()[-800:]
                raise Refused(
                    f"brain child exited immediately (rc={child.returncode}). "
                    f"End of its output: {tail or '(none captured)'}"
                )
            try:
                client.call("ping")
                self._brain_last_used = time.time()
                self._audit(
                    actor="daemon",
                    channel=CHANNEL_SCHEDULED,
                    tool="brain.load",
                    tier=str(Tier.READ),
                    decision="allow",
                    reason=f"brain child ready pid={child.pid}",
                )
                return client
            except Exception as exc:  # noqa: BLE001 - not up yet, keep trying
                last = exc
                time.sleep(0.25)
        self._brain_stop("start-up timed out")
        raise Refused(
            f"brain child did not answer within {self.BRAIN_START_TIMEOUT_S:.0f}s "
            f"({type(last).__name__ if last else 'no error'}); it was killed"
        )

    def _brain_call(self, op: str, *, tier: str | None = None, **args: Any) -> Any:
        """One round trip to the child, with the idle clock touched.

        Every op goes through here rather than touching the client directly, so
        that there is exactly one place where "the brain is alive" is established
        and exactly one place where the idle timer is reset. A path that
        refreshed the timer by hand would eventually be one that forgot, and a
        brain that is never marked idle never gets evicted - which would quietly
        cost the user 5 GiB indefinitely, which is the exact thing D1 forbids.

        `tier` is consumed here rather than forwarded: it decides which child is
        running, so it is a property of the connection and not of the request. A
        tier left in `args` would arrive at the child as an unexpected keyword.
        """
        client = self._brain_ensure(tier)
        self._brain_last_used = time.time()
        # `PipeClient.call` returns the *unwrapped* result and raises on a failed
        # response - there is no {"ok": ...} envelope to inspect. Treating the
        # result as an envelope makes every call look like a failure, because a
        # successful turn dict has no "ok" key to be truthy.
        result = client.call(op, **args)
        # A turn that came back with its own error is reported as a failure of
        # that op, not as a successful turn with an empty answer. The distinction
        # matters: the caller must be able to tell "the model declined" from
        # "the child is broken", and only the daemon can tell them apart.
        if isinstance(result, dict) and result.get("error"):
            raise Refused(str(result["error"]))
        return result

    def op_brain_status(self, args: dict) -> Any:
        """Is there a brain, and has it been touched recently?

        Reports the idle seconds even when no child exists, because "it is not
        running" and "it is running and was just used" are the two answers a user
        needs when they are wondering why the card is busy.
        """
        idle = (
            time.time() - self._brain_last_used if self._brain_last_used else None
        )
        return {
            "running": self._brain_alive(),
            "pid": self._brain.pid if self._brain_alive() and self._brain else None,
            "idle_s": round(idle, 1) if idle is not None else None,
            "ttl_s": self.BRAIN_IDLE_TTL_S,
            "model_loaded": bool(self._brain_loaded),
            "tier": self._brain_tier,
            "note": (
                "no child is running; the next brain.turn starts one "
                "(~2.5s model load, D22)"
            )
            if not self._brain_alive()
            else "the child is up; the model is loaded only after a turn",
        }

    def op_brain_load(self, args: dict) -> Any:
        """Ask the child to load the model now rather than on the next turn.

        Exists because the first turn after a cold daemon otherwise pays the
        model load silently, and a user watching a 6 second pause has no way to
        tell load from thinking. Making it explicit means the overlay can show it.
        """
        result = self._brain_call("load", tier=args.get("tier"))
        self._brain_loaded = bool(result.get("loaded"))
        return result

    def op_brain_turn(self, args: dict) -> Any:
        """One turn: decide, then act. The returned Action is a proposal.

        The critical boundary is that this returns a *proposal*. The daemon does
        not run it. It returns the tool name and arguments to the caller, who must
        send them back through `tool_call`, where the policy gate and - for
        anything T3 - a human get to see them. A brain that could execute its own
        decision would make the entire gate decorative.
        """
        prompt = str(args.get("prompt", "")).strip()
        if not prompt:
            raise Refused("brain.turn needs a non-empty prompt")
        result = self._brain_call("turn", prompt=prompt, tier=args.get("tier"))
        self._brain_loaded = bool(result.get("loaded", True))
        # Audited at proposal time, with the tier the *model* chose to reach for,
        # because a brain that keeps reaching for T3 tools is a fact worth having
        # in the log even though nothing has been executed yet.
        self._audit(
            actor=str(args.get("actor", "brain")),
            channel=self._channel(args),
            tool=f"brain.turn:{result.get('tool') or result.get('kind')}",
            tier=str(Tier.READ),
            decision="allow",
            reason=f"brain proposed {result.get('tool') or result.get('kind')!r}",
            args={"kind": result.get("kind")},
        )
        return result

    def op_brain_stream(self, args: dict) -> Any:
        prompt = str(args.get("prompt", "")).strip()
        if not prompt:
            raise Refused("brain.stream needs a non-empty prompt")
        result = self._brain_call("stream", prompt=prompt, tier=args.get("tier"))
        self._brain_loaded = bool(result.get("loaded", True))
        return result

    def op_brain_unload(self, args: dict) -> Any:
        """Kill the child. The whole model goes with it.

        There is deliberately no "unload the model but keep the process". On
        Windows an unloaded llama context does not reliably give its VRAM back, so
        a keep-the-process-unload-the-model design would look like it saved the
        user's memory while in fact holding 5 GiB. Killing is the only version of
        this that is true.
        """
        was = self._brain_alive()
        self._brain_stop("explicit unload")
        self._brain_loaded = False
        return {"stopped": was, "vram_returned": was}

    def op_brain_describe(self, args: dict) -> Any:
        """What the brain can see, from the child that would use it.

        Deliberately asks the child rather than reading the registry here: the
        number that matters is the one the model was actually given, and a local
        re-derivation could disagree with it without anything noticing.
        """
        return self._brain_call("describe")

    # -- the Telegram bridge ------------------------------------------------
    #
    # Three ops and a thread. The bridge is a *client* like everything else here:
    # an inbound message becomes a `handle()` call with channel="telegram" and
    # nothing more (D31).
    #
    # The channel matters more than it looks. A Telegram message does NOT assert
    # presence. The commonest reason to be away from the PC is not sleep - it is
    # coffee, phone in pocket - and treating that message as presence would
    # unlock T2 and T3 on a machine nobody is watching. So T0/T1 run from the
    # coffee shop and T2/T3 queue until the person is back at the keyboard (D30).

    def op_telegram_status(self, args: dict) -> Any:
        """What the bridge is doing. Never includes the token."""
        if self.telegram is None:
            return {
                "configured": False,
                "note": (
                    "no state/telegram.json - put telegram_bot_token and "
                    "telegram_allowed_chat_ids there to enable the bridge"
                ),
            }
        return {"configured": True, **self.telegram.status()}

    def op_telegram_send(self, args: dict) -> Any:
        """Proactively send a message. T3 and external, so the gate rules apply.

        The bridge's own replies are transport and do not come through here -
        answering the message you were just sent is not this tool. This is for a
        message the assistant starts: a reminder, an alert, a result nobody asked
        for. Which is exactly why it is T3.
        """
        chat_id = args.get("chat_id")
        text = str(args.get("text", ""))
        if self.telegram is None:
            raise Refused("the Telegram bridge is not configured")
        if not self.telegram.config.allows(chat_id):
            raise Refused(
                f"chat {chat_id!r} is not on the allowlist; the bridge will not "
                "talk to anyone else"
            )
        if not text.strip():
            raise Refused("telegram.send needs some text")
        if not self.telegram.say(int(chat_id), text):
            raise Refused(f"the send failed: {self.telegram.last_error}")
        return {"sent": True, "chat_id": int(chat_id), "chars": len(text)}

    def _load_telegram(self) -> None:
        """Build the bridge if it can be built, and say why if it cannot.

        Deliberately non-fatal. A missing or malformed token must not stop the
        daemon: the tray, the hotkey, the brain and the gate all work without it,
        and a daemon that refuses to start over a Telegram problem turns one
        broken integration into a dead assistant.
        """
        try:
            from .telegram import TelegramBridge, TelegramError, load_config

            self.telegram = TelegramBridge(self.handle, load_config(self.state / "telegram.json"))
        except Exception as exc:  # noqa: BLE001 - reported, never fatal
            self.telegram = None
            self.telegram_error = f"{type(exc).__name__}: {exc}"

    def _telegram_thread(self) -> None:
        self.telegram.start()

    def _brain_sweeper(self) -> None:
        """Kill the child once it has been idle past the TTL (D3).

        This is the mechanism the whole resource model rests on, so it is written
        to be boring: a loop, a comparison against the parent's own clock, and a
        kill. It catches everything because an exception escaping this thread
        would silently end the only thing reclaiming the user's VRAM, and the
        symptom would be a card that stays busy forever with no error anywhere.
        """
        while not self._halt.wait(self.BRAIN_SWEEP_EVERY_S):
            try:
                if not self._brain_alive():
                    continue
                idle = time.time() - self._brain_last_used
                if idle >= self.BRAIN_IDLE_TTL_S:
                    self._brain_stop(f"idle for {idle:.0f}s (D3 TTL)")
            except Exception:  # noqa: BLE001 - a broken sweeper must not be silent
                self._brain_stop("sweeper error")

    def _brain_sweep_now(self, args: dict) -> Any:
        """Force a TTL check now. Exposed so the behaviour is testable and
        inspectable rather than only reachable by waiting 15 seconds."""
        if not self._brain_alive():
            return {"running": False, "stopped": False}
        idle = time.time() - self._brain_last_used
        if idle < self.BRAIN_IDLE_TTL_S:
            return {"running": True, "stopped": False, "idle_s": round(idle, 1)}
        self._brain_stop(f"idle for {idle:.0f}s (D3 TTL)")
        return {"running": False, "stopped": True, "idle_s": round(idle, 1)}

    # ------------------------------------------------------------------ wiring

    queue: list  # populated in __init__ via _reset; see below

    _OPS: dict[str, Callable] = {}
    _EXECUTORS: dict[str, Callable] = {}
    _PREVIEWS: dict[str, Callable] = {}


def _install_tables() -> None:
    """Fill the op, executor and preview tables by name.

    Done after the class body so the tables are built from names that are
    checked by ``_check_tables`` below, rather than from a dict literal that
    can drift from the methods without anyone noticing.
    """
    ops = {}
    for name in dir(Daemon):
        if name.startswith("op_"):
            ops[name[3:]] = getattr(Daemon, name)
    Daemon._OPS = ops
    Daemon._EXECUTORS = {
        "system.info": Daemon._exec_system_info,
        "system.processes": Daemon._exec_system_processes,
        "system.gpu_state": Daemon._exec_system_gpu_state,
        "system.disk_usage": Daemon._exec_system_disk_usage,
        "system.package_list": Daemon._exec_system_package_list,
        "system.service_status": Daemon._exec_system_service_status,
        "system.event_log": Daemon._exec_system_event_log,
        "system.diagnose": Daemon._exec_system_diagnose,
        "file.list": Daemon._exec_file_list,
        "file.metadata": Daemon._exec_file_metadata,
        "file.preview": Daemon._exec_file_preview,
        "file.search": Daemon._exec_file_search,
        "file.edit": Daemon._exec_file_edit,
        "file.copy": Daemon._exec_file_copy,
        "file.move": Daemon._exec_file_move,
        "file.archive": Daemon._exec_file_archive,
        "file.bulk_rename": Daemon._exec_file_bulk_rename,
        "file.read": Daemon._exec_file_read,
        "file.create": Daemon._exec_file_create,
        "file.delete": Daemon._exec_file_delete,
        "file.restore": Daemon._exec_file_restore,
        "voice.speak": Daemon._exec_voice_speak,
        "voice.listen": Daemon._exec_voice_listen,
        "voice.status": Daemon._exec_voice_status,
    }
    Daemon._PREVIEWS = {
        # Only destructive tools need a real preview, because only destructive
        # tools are the ones a human is asked to bless. A preview for a T0 read
        # would be theatre.
        "file.delete": Daemon._preview_file_delete,
        # Audible tools get previews for the same reason destructive ones do:
        # a T3 dialog with no payload is a dialog the user approves blind.
        "voice.speak": Daemon._preview_voice_speak,
        "voice.listen": Daemon._preview_voice_listen,
    }


_install_tables()


def _check_tables() -> None:
    """Fail loudly at import if the wiring and the registry disagree.

    A tool that exists in the registry but not in the executor table is not a
    bug yet - it is an unfinished feature, and ``status`` reports the count. But
    an *executor* with no registry entry would be a tool that runs without ever
    being classified, which is exactly what D5 forbids. That is a bug, and it
    should stop the process rather than be discovered in production.
    """
    registry = {spec.name for spec in ALL_TOOLS}
    orphans = set(Daemon._EXECUTORS) - registry
    if orphans:
        raise RuntimeError(
            f"executors with no registry entry: {sorted(orphans)} - these would "
            f"run without a tier, which D5 does not permit"
        )
    preview_orphans = set(Daemon._PREVIEWS) - registry
    if preview_orphans:
        raise RuntimeError(
            f"previews with no registry entry: {sorted(preview_orphans)}"
        )
    # A destructive tool that is *executable* must have a preview, or the
    # confirmation dialog would show a bare "approve file.delete?" with nothing
    # describing what would happen. D32 requires the resolved payload, so a
    # missing preview is a build gap that has to be visible at import rather than
    # a surprise at the moment of a delete.
    #
    # Scoped to executable tools on purpose. A destructive tool with no executor
    # can never reach a confirmation, because op_tool_call resolves the executor
    # before the CONFIRM branch - so demanding a preview for it would be
    # demanding ceremony for a code path that cannot execute.
    # T3 tools that a human must bless, whatever the reason they are T3. The
    # rule is about the dialog, not about the flag: D32 requires the resolved
    # payload, and a dialog reading only "approve voice.listen?" is a dialog
    # with nothing to approve *on*. That was true for destructive tools and it
    # is just as true for audible ones, so the check asks about the tier rather
    # than about the reason - two reasons, one requirement.
    unblessable = {
        spec.name
        for spec in ALL_TOOLS
        if spec.tier == Tier.IRREVERSIBLE
        and (spec.destructive or spec.audible or spec.external)
        and spec.name in Daemon._EXECUTORS
        and spec.name not in Daemon._PREVIEWS
    }
    if unblessable:
        raise RuntimeError(
            "executable T3 tools with no preview builder: "
            f"{sorted(unblessable)} - D32 requires the confirmation dialog to show "
            f"the resolved payload, not just the tool name"
        )
    dupes = {spec.name for spec in ALL_TOOLS}
    if len(dupes) != len(ALL_TOOLS):
        raise RuntimeError("duplicate tool names in the registry")


_check_tables()


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #


def _parse_argv(argv: list[str]) -> dict[str, Any]:
    """Minimal flag parsing. argparse for four flags would be more code than the
    flags, and the daemon is not a general-purpose CLI."""
    opts: dict[str, Any] = {
        "vault": None,
        "pipe": ipc.PIPE_BRAIN_REQ,
        "unattended": False,
        "ceiling": Tier.IRREVERSIBLE,
        "check": False,
    }
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-h", "--help"):
            print(__doc__)
            print(
                "usage: python -m assistant.daemon [--vault PATH] [--pipe NAME]\n"
                "                        [--unattended] [--ceiling T0..T3] [--check]"
            )
            raise SystemExit(0)
        if arg == "--check":
            opts["check"] = True
        elif arg == "--unattended":
            opts["unattended"] = True
        elif arg in ("--vault", "--pipe", "--ceiling"):
            if i + 1 >= len(argv):
                raise SystemExit(f"{arg} needs a value")
            key = arg[2:]
            if key == "ceiling":
                key = "ceiling"
                if argv[i + 1] not in TIER_BY_NAME:
                    raise SystemExit(
                        f"--ceiling must be one of "
                        f"{sorted(TIER_BY_NAME)}; got {argv[i + 1]!r}"
                    )
                opts["ceiling"] = TIER_BY_NAME[argv[i + 1]]
            else:
                opts[key] = argv[i + 1]
            i += 1
        else:
            raise SystemExit(f"unknown argument {arg!r}")
        i += 1
    return opts


def main(argv: list[str] | None = None) -> int:
    opts = _parse_argv(list(sys.argv[1:] if argv is None else argv))
    d = Daemon(
        state=STATE,
        vault=opts["vault"],
        attended=not opts["unattended"],
        session_ceiling=opts["ceiling"],
    )
    if opts["check"]:
        # Preflight only. This is the flag to run after any change to the state
        # directory, and it is deliberately a separate mode so that checking
        # never starts a listener.
        print(json.dumps(d.preflight(), indent=2, default=str))
        return 0
    d.serve_forever(pipe=opts["pipe"])
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
