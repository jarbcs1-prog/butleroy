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
import re
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
from . import delegate, fileops, obsidian, sysinfo, teach
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
#: The message limit, imported rather than restated. `telegram.send` refuses an
#: over-long message instead of letting `say` cut it mid-sentence, and a second
#: copy of 4096 is a second copy that can be wrong. `telegram.py` imports only
#: the standard library, so this does not make the bridge mandatory at import -
#: `_load_telegram` keeps its own deliberate laziness about the *config*.
from .telegram import TELEGRAM_TEXT_MAX

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


_EXTENSION_OK = re.compile(r"\.[A-Za-z][A-Za-z0-9]{0,9}")
"""What counts as a file extension in `_admin_is_pathish`.

Declared here rather than inside the method because it is a grammar, and a
grammar that lives in a `return` statement is a grammar nobody can read. See
`_admin_is_pathish` for why the naive version of this test refused
`python -c "print(3.14)"`.
"""


def _delegate_int(raw: Any, fallback: int) -> int:
    """An integer argument that a caller may have sent as anything at all.

    Coerces, and falls back on anything it cannot read rather than refusing: a
    read tool that raises because `limit` arrived as a string would make the
    read unavailable exactly when the caller most wants it, and the fallback is
    the documented default rather than an invented number.
    """
    if raw is None or isinstance(raw, bool):
        return fallback
    try:
        return int(raw)
    except (TypeError, ValueError):
        return fallback


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

        # --- delegated tasks ------------------------------------------------
        # Created here rather than lazily on first use because the bound is a
        # property of the daemon, not of the call: `MAX_TASKS` tasks exist from
        # the moment the process does, and a lazily-created registry would let
        # the count mean something different after a restart than before.
        self.tasks = delegate.Registry()

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
            "wake_model_present": self._wake_model_path() is not None,
        }
        return status

    def _wake_model_path(self) -> Path | None:
        """The wake-word file this daemon will actually load, or None.

        One function so the question "is there a wake word?" has one answer. A
        second path would eventually be the one the loader reads and the other the
        one the check looks at, and the symptom would be a loop that reports no
        wake word on a machine where one is installed.

        It resolves through the audio module, because audio.WakeWord is what loads
        the model, and a check that reasoned about a different file than the loader
        does would be a check of a hope. Returns None rather than a Path that does
        not exist, so a missing wake word cannot be told from one nobody looked
        for - the two look identical and mean opposite things.
        """
        return audio.wake_model_path(audio.WAKE_WORD)

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
        if self._wake_model_path() is None:
            raise Refused(
                f"no wake word named {audio.WAKE_WORD!r} is installed. Run "
                f"openwakeword.utils.download_models(), or use voice.listen "
                f"without one - available: {audio.wake_models_available()}"
            )
        turns = args.get("turns")
        loop = voice_loop.VoiceLoop(
            self._voice_turn,
            device=args.get("device"),
            voice=str(args.get("voice") or speech.DEFAULT_VOICE),
            seconds=float(args.get("seconds", 30)),
            wake_name=audio.WAKE_WORD,
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

    # ------------------------------------------------- durable notes (D63)
    #
    # `file.write_notes` is T1 and takes no path. The vault chooses the
    # directory, for the same reason `telegram.send_draft` and the teaching
    # ledger do: a T1 write tool that takes a path from the caller is an
    # arbitrary-write primitive wearing a better name. Which is also why the
    # parameter is called `section` and not `subdir` - `subdir` contains
    # "dir", so `_check_paths` screened the documented call
    # `{"subdir": "journal"}` as a relative path and refused every valid
    # invocation. The rename is the fix; carving an exception out of the
    # screen would have been the alternative, and a hole in a rule is still a
    # hole (D49 runs both ways).

    def _note_section(self, args: dict) -> str:
        section = str(args.get("section") or "").strip()
        if not section:
            raise DaemonError(
                "which section? choose one of: "
                + ", ".join(sorted(obsidian.SUBDIRS)))
        if section not in obsidian.SUBDIRS:
            raise DaemonError(
                f"unknown Assistant section {section!r}; the vault has: "
                + ", ".join(sorted(obsidian.SUBDIRS)))
        return section

    def _exec_file_write_notes(self, args: dict) -> Any:
        section = self._note_section(args)
        title = str(args.get("title") or "").strip()
        if not title:
            raise DaemonError("a note needs a title; it is what the file is named after")
        body = args.get("body")
        if not isinstance(body, str) or not body.strip():
            raise DaemonError("a note needs a body; there is nothing to write otherwise")
        if len(body.encode("utf-8")) > obsidian.MAX_WRITE_BYTES:
            raise DaemonError(
                f"the body is {len(body.encode('utf-8'))} bytes and the vault "
                f"refuses more than {obsidian.MAX_WRITE_BYTES}; split it, or "
                "write the long half as a file and link to it")

        target = self._vault().write_note(section, title, body)

        # Whether an existing note was replaced is *measured*, not reported
        # from the flag we passed in. `write_note` never overwrites unless
        # asked, and when it declines it writes a timestamped sibling - so the
        # filename is the evidence. Claiming `overwrote: false` from the
        # default would be a thing the bridge could contradict, and reading the
        # consumer is cheaper than being contradicted by it (D53).
        plain = f"{obsidian.slugify(title)}.md"
        overwrote = target.name == plain
        out = {
            "path": str(target),
            "section": section,
            "title": title,
            "bytes": len(body.encode("utf-8")),
            "overwrote": overwrote,
            "reversible": True,
        }
        if overwrote:
            out["note"] = (
                "the existing note was replaced in place. It is still only a "
                "file in your vault, and Obsidian keeps its own version "
                "history if it is switched on")
        else:
            out["previous"] = str(target.parent / plain)
            out["note"] = (
                "your vault already had a note called "
                f"{plain!r}, so this was written beside it as "
                f"{target.name!r} rather than over the top of it. Nothing you "
                "wrote was replaced. If you meant to update the older note, "
                "say so and it will be done explicitly")
        return out

    # -------------------------------------------- emptying the recycle bin
    #
    # The one T3 with no inverse, which makes it the honest test of what the
    # D60 undo journal is for. It still gets a row: a journal that only
    # records the things it can undo is a journal that cannot tell you which
    # of its entries were the ones you cannot take back.
    #
    # `C:\$Recycle.Bin` is on our own deny list, so nothing here reads it.
    # `Get-ChildItem 'C:\$Recycle.Bin'` would work perfectly well - that is
    # exactly why it is the wrong thing to do. The path would be buried in a
    # script string and would never reach `_check_paths`, which is the gate
    # bypassed by construction rather than by argument. So we ask the shell's
    # own namespace instead, and ask PowerShell what it *would* do.

    def _preview_file_empty_recycle_bin(self, args: dict) -> dict:
        listing = sysinfo.recycle_listing()
        whatif = sysinfo.recycle_whatif()
        out: dict[str, Any] = {
            "action": "empty the recycle bin (there is no undo)",
        }
        if listing["available"]:
            out["count"] = listing["count"]
            out["items"] = listing["items"][:50]
            out["items_shown"] = min(len(listing["items"]), 50)
        else:
            out["count"] = None
            out["items"] = []
            out["count_unknown"] = (
                "the shell would not tell us what is in the bin, so the "
                "count below is unknown rather than zero - an empty list and "
                "an unreadable list look identical and mean opposite things")
        # Two things here, each said once. The first version interpolated
        # `listing['note']` and then added a second sentence about the same
        # unverifiable size column, so the field opened by explaining why there
        # is no byte total and closed by explaining it again - one sentence
        # carrying the same fact twice reads as two separate problems to
        # whoever is being asked to approve this (the shape D62 caught in the
        # teaching refusals).
        #
        # The second thing is the one the first version left out entirely, and
        # it is the one a reader needs: we did not simply walk the bin
        # directory, because our own policy denies it. Saying "how we counted"
        # without saying "and here is the route we were not allowed to take"
        # invites the reasonable question of why the obvious method was
        # skipped, with no answer in the dialog.
        out["how_we_counted"] = (
            f"{listing['source']}, and not by reading "
            f"{listing['denied_directory']} - that path is on this system's "
            "own deny list, and a shell command naming it would slip past the "
            "check that refuses it rather than pass it. The size column could "
            "not be verified, so no byte total is given: a number nobody "
            "checked is worse than no number")
        out["windows_says"] = (whatif["stdout"] or "").strip() or whatif["stderr"].strip()
        out["windows_confirmed_the_target"] = whatif["names_target"]
        if not whatif["names_target"]:
            out["windows_said_nothing_useful"] = (
                "PowerShell exited "
                f"{whatif['exit_code']} without naming the target, so we do "
                "not actually know this would act on the recycle bin. "
                "Approving on the strength of the command alone would be "
                "claiming a confirmation we do not have")
        out["undo"] = None
        out["undo_note"] = (
            "nothing here can be brought back. The bin is emptied by asking "
            "Windows to empty it, and Windows does not keep a copy")
        out["reversible"] = False
        out["not_the_quarantine"] = (
            "this does not touch this system's quarantine, which is where "
            "`file.delete` puts things; use `file.restore` for those. "
            "Emptying the bin leaves them recoverable")
        return out

    def _exec_file_empty_recycle_bin(self, args: dict) -> Any:
        # Asked again here, so the *result* carries the evidence rather than
        # the preview carrying it alone. A confirmation shown ten minutes ago
        # is evidence about ten minutes ago.
        whatif = sysinfo.recycle_whatif()
        if not whatif["names_target"]:
            raise DaemonError(
                "PowerShell did not name the recycle bin as its target "
                f"(exit {whatif['exit_code']}: "
                f"{whatif['stderr'].strip() or whatif['stdout'].strip()}), so "
                "we do not know what this would empty. Nothing was done")
        row = self._admin_journal(
            "file.empty_recycle_bin", {},
            undo=None,
            note="the first action with no inverse. The row exists because a "
                 "journal that only records what it can undo cannot tell you "
                 "which entries were the ones you cannot take back")
        listing = sysinfo.recycle_listing()
        result = sysinfo.recycle_clear()
        payload = {
            "emptied": bool(result["ok"]),
            "exit_code": result["exit_code"],
            "stdout": result["stdout"].strip(),
            "stderr": result["stderr"].strip(),
            "was_holding": listing["count"] if listing["available"] else None,
            "windows_says": (whatif["stdout"] or "").strip(),
            "reversible": False,
            "undo": None,
        }
        if not result["ok"]:
            payload["note"] = (
                "Windows refused. The bin is as it was - the exit code and "
                "its message are above, and nothing was removed")
        self._admin_close_journal(row, ok=bool(result["ok"]), result=payload)
        if not result["ok"]:
            raise DaemonError(
                f"emptying the recycle bin failed with exit "
                f"{result['exit_code']}: "
                f"{result['stderr'].strip() or result['stdout'].strip()}")
        return payload

    # ------------------------------------------------------------ administration
    #
    # Three Tier 2 tools: launch an application, close a process, run one
    # command. Each gets a real preview, not because Tier 2 is destructive -
    # `file.read` is T0 and touches far more - but because of what a preview is
    # for here. "Launch an application" and "close a process" are the two
    # vaguest phrases in the registry: `notepad` is a name the shell may
    # resolve to five different binaries, and `chrome.exe` is a request that
    # means "all eight of them". A confirmation dialog that says only that
    # passes D32's letter - the args hash - and fails its purpose, because the
    # user is being asked to approve a word rather than an action.
    #
    # So each preview resolves the payload *first* and shows the resolution.
    # That is also what binds the confirmation to something real: approving
    # `app.launch` binds to the resolved path, so a different `app.launch`
    # resolving elsewhere needs a second confirmation rather than inheriting
    # the first.

    _ADMIN_MAX_ARGS = 4096          # a command line a human cannot read is not a preview
    _ADMIN_OUTPUT_CAP = 4000
    # Directory-walk budget for resolving an application name. A search that can
    # take an unbounded time is a search a confirmation dialog waits on.
    _APP_DIR_BUDGET = 3000
    # Directories under Program Files that hold libraries and SDKs rather than
    # applications. Skipping them is the difference between a resolution that
    # answers in one iteration and one that never answers: `Common Files` and
    # `Reference Assemblies` alone are thousands of directories, and nothing a
    # person says out loud is installed in them.
    _APP_PRUNE = frozenset({
        "common files", "windowsapps", "reference assemblies", "dotnet",
        "microsoft.net", "windows kits", "windows powershell", "nodejs",
        "package cache", "microsoft sdks", "microsoft shared", "gadget meta",
        "windows photo viewer", "windows portable devices", "diagnostics",
        "hostednetwork", "internet explorer", "windows sidebar", "xbox games",
        "microsoft games", "windows nt", "winsxs", "servicing",
    })

    # Windows' own protected processes. Killing any of these ends the session,
    # the desktop, or the machine - and the failure mode is a blue screen or an
    # instant logout, neither of which a confirmation dialog describes well.
    _ADMIN_UNKILLABLE = frozenset({
        "system", "system idle process", "registry", "smss.exe", "csrss.exe",
        "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe",
        "svchost.exe", "lsm.exe", "audiodg.exe", "dwm.exe", "spoolsv.exe",
        "fontdrvhost.exe", "sihost.exe", "ctfmon.exe", "wmiprvse.exe",
        "memory compression", "secure system", "explorer.exe",
    })

    def _admin_argv(self, args: dict, key: str = "command") -> list[str]:
        """A command as an argv list, or refuse.

        A string is refused rather than split. `subprocess` with `shell=False`
        takes a list and treats every element as one argument, which is the
        whole reason there is no injection surface here; accepting a string
        would mean splitting it ourselves with quoting rules that are wrong in
        some corner case we have not thought of yet. `shlex` is not used either
        - it implements POSIX rules, and a Windows command line is not POSIX.
        """
        raw = args.get(key)
        if raw is None:
            raise DaemonError(f"{key!r} is required")
        if isinstance(raw, str):
            raise DaemonError(
                f"{key!r} must be a list of arguments, not a string. A command "
                f"string has to be split on spaces, and every rule for that split "
                f"is wrong for some argument; a list is unambiguous, and it is "
                f"what stops a path with a space in it from becoming two "
                f"arguments. Example: {json.dumps([key, raw])}"
            )
        if not isinstance(raw, (list, tuple)) or not raw:
            raise DaemonError(f"{key!r} must be a non-empty list of arguments")
        argv = []
        for i, part in enumerate(raw):
            if not isinstance(part, str):
                raise DaemonError(
                    f"{key}[{i}] is {type(part).__name__}, not a string. Every "
                    f"argument must already be a string - this tool does no "
                    f"quoting, conversion or interpolation of its own"
                )
            argv.append(part)
        total = sum(len(a) for a in argv)
        if total > self._ADMIN_MAX_ARGS:
            raise DaemonError(
                f"command line is {total} characters, over the "
                f"{self._ADMIN_MAX_ARGS} this tool will accept. A confirmation "
                f"dialog showing the first 4 KB of a command is a dialog the "
                f"user approves without reading"
            )
        return argv

    def _admin_cwd(self, args: dict) -> str:
        r"""Where the command runs, anchored and confined like any other path.

        Defaults to the single approved root rather than to the daemon's own
        working directory, which is wherever the daemon happened to be started
        from. A command with no cwd that inherits `G:\` is a command whose
        relative paths reach the whole drive.
        """
        raw = str(args.get("cwd") or "").strip()
        if not raw:
            roots = list(getattr(self, "manifests", None).roots()) if hasattr(
                self, "manifests") else []
            if len(roots) == 1:
                return str(Path(str(roots[0])).resolve())
            return str(Path.cwd())
        target = Path(raw)
        if not target.is_absolute() and len(list(getattr(self, "manifests", None).roots())) == 1:
            target = Path(str(list(self.manifests.roots())[0])) / target
        why = self.gate.path_refusal(str(target))
        if why:
            raise Refused(f"shell.run working directory refused - {why}")
        return str(target.resolve())

    @staticmethod
    def _admin_is_pathish(part: str) -> bool:
        r"""Does this argument name a file, rather than being a switch?

        The first version of this test asked `os.path.isabs`, which is wrong in
        a way only running it finds: `os.path.isabs("/c")` is True, because a
        rooted path is "absolute" relative to the drive it resolves against. So
        `["cmd", "/c", "exit", "3"]` had `/c` screened as a path, and the call
        was refused for reaching `C:\c` - a directory that does not exist, was
        never named, and was invented by the screen. On Windows a leading slash
        is a switch; that is the convention `cmd.exe` and `tasklist` both use.

        Absolute therefore means a drive letter and a separator, or a UNC
        prefix - not merely a leading slash.

        The last line is the extension test, and it was `bool(Path(part).suffix)`
        until delegating a task to a python one-liner refused it. `PurePath`
        takes everything after the last dot as the suffix, so
        `"import time; time.sleep(120)"` has the suffix `".sleep(120)"`, is
        therefore a filename, and the call was refused for reaching a path the
        screen had invented - the same defect as `/c` above, found by the same
        route of asking what a real command line looks like. It also made
        `python -c "print(3.14)"` unrunnable, which is the ordinary case rather
        than the exotic one.

        So the suffix has to look like a file extension: letters and digits only,
        short, not starting with a digit. `notes.txt` and `x.dll` pass;
        `.sleep(120)`, `.14)` and `.tar.gz`-as-a-whole-directory-name do not.
        Over-matching a real filename still costs the user one restatement, which
        is why this stays a heuristic rather than becoming a real parser.
        """
        if not part:
            return False
        head, sep, tail = part.partition(":")
        if sep and len(head) == 1 and head.isalpha() and tail[:1] in ("\\", "/"):
            return True                        # C:\... or C:/...
        if part.startswith("\\\\"):
            return True                        # \\server\share
        if part.startswith(("/", "\\")):
            return False                       # /c, /FI, \n - switches
        if "\\" in part or "/" in part:
            return True                        # ..\..\secrets.txt, build/out
        return bool(_EXTENSION_OK.fullmatch(Path(part).suffix))

    def _admin_arg_paths(self, argv: list[str]) -> list[tuple[str, str]]:
        """The arguments that name a path, paired with the path.

        A heuristic, and it is applied *in addition to* the gate's own check
        rather than instead of it: `policy._check_paths` already screens every
        argument whose *name* looks like a path (`path`, `dir`, `target`, ...),
        and this catches the ones hidden behind a program name, where
        `["del", "C:/Windows/System32/config/SAM"]` passes that screen entirely
        because no argument is named `path`.

        Over-matching a real path costs the user one restatement; missing one is
        a hole with no symptom. The gap that remains is a bare relative name
        with no extension - `del secrets` - which resolves inside the working
        directory, and `_admin_cwd` confines that to an approved root. So what
        this fails to catch still cannot escape.
        """
        found: list[tuple[str, str]] = []
        for part in argv[1:]:
            if part.startswith("-"):
                # A flag, or `--flag=value`. The value is the path, so it is
                # still screened; the flag itself is not.
                if "=" not in part:
                    continue
                part = part.split("=", 1)[1]
            if self._admin_is_pathish(part):
                found.append((part, part))
        return found

    def _preview_shell_run(self, args: dict) -> dict:
        argv = self._admin_argv(args)
        cwd = self._admin_cwd(args)
        paths = [p for p, _ in self._admin_arg_paths(argv)]
        return {
            "action": "run one command (no shell; output is returned, not streamed)",
            "command": argv,
            "command_line": subprocess.list2cmdline(argv),
            "cwd": cwd,
            "paths_in_arguments": paths[:50],
            "paths_truncated": len(paths) > 50,
            "timeout_s": float(args.get("timeout_s", 120.0)),
            "note": (
                "no shell is involved, so & | ; > and redirection are literal "
                "characters rather than syntax. That is deliberate: it removes "
                "the injection surface, at the cost of not being able to run "
                "pipelines. Use two calls, or a build script in the workspace."
            ),
        }

    def _exec_shell_run(self, args: dict) -> Any:
        argv = self._admin_argv(args)
        cwd = self._admin_cwd(args)
        timeout = min(max(float(args.get("timeout_s", 120.0)), 1.0), 900.0)
        # Re-check the paths in the executor as well as the preview. The
        # preview is what the user approved; this is what runs. If they differ,
        # that is a bug worth refusing rather than executing (D32).
        for _, path in self._admin_arg_paths(argv):
            why = self.gate.path_refusal(str(Path(cwd) / path)
                                         if not Path(path).is_absolute() else path)
            if why:
                raise Refused(f"shell.run refused - {why}")
        result = sysinfo.run_capture(argv, timeout=timeout)
        result["cwd"] = cwd
        result["ok"] = result.get("exit_code") == 0 and not result.get("timed_out")
        return result

    def _preview_app_launch(self, args: dict) -> dict:
        raw = str(args.get("app") or args.get("name") or "").strip()
        resolved, how = self._resolve_app(raw)
        extra = args.get("args") or []
        return {
            "action": "start an application",
            "requested": raw,
            # Resolved here, not in the executor, so the dialog names the file
            # that will actually run. An unresolved name is refused rather than
            # run: handed to CreateProcess, a bare name could resolve against the
            # working directory as well as PATH, and the user would be approving
            # a word instead of an executable.
            "resolved": str(resolved) if resolved else None,
            "found_by": how,
            "arguments": extra if isinstance(extra, list) else str(extra),
            "note": (
                "resolved to an executable before this dialog is shown; an "
                "unresolved name is refused rather than run"
            ),
        }

    def _exec_app_launch(self, args: dict) -> Any:
        raw = str(args.get("app") or args.get("name") or "").strip()
        if not raw:
            raise DaemonError("app.launch needs 'app' - an executable name or a full path")
        exe, how = self._resolve_app(raw)
        if exe is None:
            # Say why the search came up empty and what would work instead,
            # rather than handing the string to CreateProcess and letting it
            # fail with "not a valid Win32 application".
            raise DaemonError(f"cannot launch {raw!r}: {how}")
        why = self.gate.protected_refusal(str(exe))
        if why:
            raise Refused(f"app.launch refused - {why}")
        # Arguments are screened exactly as `shell.run` screens them: opening an
        # editor is harmless, opening it on somebody's private key is not, and
        # the argument is the only place the difference can be seen.
        extra: list[str] = []
        if args.get("args"):
            extra = self._admin_argv(args, "args")
            for _, path in self._admin_arg_paths([str(exe), *extra]):
                if self.gate.path_refusal(path):
                    raise Refused(f"app.launch refused - {path} is off-limits")
        proc = subprocess.Popen(
            [str(exe), *extra],
            cwd=str(self._admin_cwd(args)) if args.get("cwd") else None,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            # Its own process group, so a Ctrl-C aimed at the daemon does not
            # reach a program the user just opened, and so a launched GUI does
            # not inherit the daemon's console.
            creationflags=(getattr(subprocess, "DETACHED_PROCESS", 0)
                           | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)),
        )
        return {"launched": str(exe), "found_by": how, "pid": proc.pid,
                "requested": raw, "detached": True}

    def _preview_app_close(self, args: dict) -> dict:
        rows = sysinfo.find_processes(str(args.get("process") or args.get("name") or ""))
        blocked = self._admin_unkillable_reason(args)
        own = self._admin_own_chain()
        described = [{**p, "protected": p["name"].lower() in self._ADMIN_UNKILLABLE
                      or int(p["pid"]) in own}
                     for p in rows.get("processes", [])]
        return {
            "action": "ask a running process to close",
            "requested": str(args.get("process") or args.get("name") or ""),
            "found": rows.get("count", 0),
            "processes": described[:20],
            "truncated": rows.get("count", 0) > 20,
            "force": bool(args.get("force")),
            "note": blocked or (
                "processes are asked to close first; 'force' terminates without "
                "the chance to save. Unsaved work is not recoverable. Rows marked "
                "protected - Windows' own, or the assistant and whatever started "
                "it - are shown so the request can be seen to include them, and "
                "are left alone"
            ),
            "refused": blocked is not None,
        }

    @staticmethod
    def _app_name_ok(raw: str) -> bool:
        """Is this a plain application name, rather than something else?

        Checked because the name is used to build a glob pattern. A name
        carrying `*`, `?` or `[]` would match whatever the caller liked rather
        than the program they named - and "launch whatever matches notepad*"
        is not a thing to hand to a human's approval dialog.
        """
        if not raw or len(raw) > 64:
            return False
        if not (raw[0].isalnum()):
            return False
        # `+` because of notepad++.exe, space because it is legal in a filename.
        return all(c.isalnum() or c in "._+- " for c in raw)

    def _app_search_roots(self) -> list[Path]:
        """Where Windows puts programs that are not on PATH.

        Measured, not assumed: notepad++ is installed at
        `C:/Program Files/Notepad++/notepad++.exe` and `Get-Command notepad++`
        finds nothing, because no installer adds it to PATH. A resolver that
        only asks `shutil.which` therefore refuses most of what is actually
        installed, which is the opposite of useful.
        """
        out: list[Path] = []
        for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
            value = os.environ.get(var)
            if value:
                out.append(Path(value))
        local = os.environ.get("LOCALAPPDATA")
        if local:
            out.append(Path(local) / "Programs")
            out.append(Path(local) / "Microsoft" / "WindowsApps")
        system = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
        if system:
            out.append(Path(system) / "System32")
        seen: set[str] = set()
        unique: list[Path] = []
        for candidate in out:
            # ProgramW6432 duplicates ProgramFiles on a 64-bit build; walking
            # it twice doubles the cost of every resolution for no new match.
            key = str(candidate).lower()
            if key not in seen:
                seen.add(key)
                unique.append(candidate)
        return unique

    def _resolve_app(self, raw: str) -> tuple[Path | None, str]:
        """Find the executable `raw` names. Returns (path, how).

        Three sources, in order, because they differ in how much they can be
        trusted: PATH is what the shell would use, an absolute path is the
        caller's own claim and is checked for existence, and the install
        folders are a guess bounded by a directory budget.

        The budget matters more than it looks. `Program Files` holds tens of
        thousands of directories; an unbounded walk makes `app.launch` slow
        enough that the user learns to approve it without waiting for it, which
        is worse than the tool not existing. So the walk is capped and says so
        when the cap is what stopped it.
        """
        name = str(raw).strip().strip('"')
        if not name:
            return None, "no application name given"
        # An absolute path is a different kind of claim from a bare name, and it
        # is checked by a different rule. The bare-name validator rejects any
        # `\` or `:`, so running it first made the absolute-path branch
        # unreachable and reported `G:\localaized\src\assistant\daemon.py` as
        # "not a plain application name" - a complaint about spelling, for a
        # path the real question is whether it is protected. Order is the fix.
        candidate = Path(name)
        if candidate.is_absolute() or name.startswith("\\\\"):
            if candidate.is_file():
                return candidate.resolve(), "absolute path given"
            return None, f"{name!r} does not exist"
        if not self._app_name_ok(name):
            return None, (
                f"{name!r} is not a plain application name. Give the "
                f"executable's own name (letters, digits, dot, dash, plus) or "
                f"a full path - wildcards are refused, because 'launch whatever "
                f"matches this' is not something to put in front of an approval"
            )
        found = shutil.which(name)
        if found:
            return Path(found).resolve(), "on PATH"
        target = name if name.lower().endswith(".exe") else f"{name}.exe"
        budget = self._APP_DIR_BUDGET
        for root in self._app_search_roots():
            if not root.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                budget -= 1
                if budget < 0:
                    return None, (
                        f"searched {self._APP_DIR_BUDGET} directories under the "
                        f"program folders without finding {name!r} - give the "
                        f"full path to skip the search"
                    )
                # A folder named after the application, checked here rather than
                # by descending into it. `os.walk` hands back a directory's
                # entire listing on entry, so `Notepad++` is visible in the
                # *first* iteration under Program Files. Measured: the previous
                # version only compared `filenames`, so it walked past the
                # match, burned all 3000 of its budget in `Common Files`, and
                # reported that notepad++ was not installed.
                for folder_name in dirnames:
                    if folder_name.lower() != name.lower():
                        continue
                    folder = Path(dirpath) / folder_name
                    for inner in (target, name):
                        if (folder / inner).is_file():
                            return (folder / inner).resolve(), f"under {folder}"
                for found_name in filenames:
                    if found_name.lower() == target.lower():
                        return (Path(dirpath) / found_name).resolve(), \
                            f"under {dirpath}"
                # Prune before descending, not after listing: these trees hold
                # tens of thousands of directories, none of which contain
                # anything a person launches by name.
                dirnames[:] = [
                    d for d in dirnames
                    if not d.startswith(".") and d.lower() not in self._APP_PRUNE
                ]
        return None, (
            f"no executable named {name!r} on PATH or in the program folders. "
            f"A full path works, and 'system.package_list' shows what is "
            f"installed"
        )

    def _admin_own_chain(self) -> set[int]:
        """This process and every ancestor of it, by PID.

        `app.close` matches by image name, and on this machine the assistant is
        a `python.exe` that a request for "close the python processes" matches.
        Measured: asking to close `python.exe` produced eight matches, and the
        list included the daemon itself and the shell that started it. Force
        closing those is the same class of outcome as force-closing
        `explorer.exe` - the session ends mid-call, with no audit entry, no
        error, and no way to ask why.

        So the chain is protected by PID rather than by name. Name-protection
        would have to blacklist `python.exe` and `pwsh.exe` wholesale, which
        would make the tool unable to clean up the stray interpreters it is
        most likely to be asked about. PID-protection keeps the useful cases
        working: the strays still close, and the ones that are us are reported
        as skipped rather than silently killed.

        The walk itself lives in `delegate.own_chain`, because delegation needs
        the same walk for a second and sharper reason: a task's process is a
        *child* of this daemon, so `delegate.intervene --action stop` can name
        the daemon. Two copies of this loop would drift, and the drifted one
        would be the one guarding the process that stops everything (D58).
        """
        return delegate.own_chain()

    def _admin_unkillable_reason(self, args: dict) -> str | None:
        """Refuse to kill a process the machine needs, and say which one.

        Checked by *name*, because that is what the caller supplied and what the
        confirmation shows. Checking the PID instead would pass the moment the
        OS restarted that process, which is exactly the moment it matters.
        """
        name = str(args.get("process") or args.get("name") or "").strip().lower()
        bare = name if name.endswith(".exe") else f"{name}.exe"
        for protected in self._ADMIN_UNKILLABLE:
            if name == protected or bare == protected:
                return (
                    f"{name!r} is a process Windows does not survive being closed. "
                    f"Closing it ends the session or the machine rather than the "
                    f"application, so this tool will not do it. If you meant a "
                    f"different program, give that name instead."
                )
        return None

    def _exec_app_close(self, args: dict) -> Any:
        blocked = self._admin_unkillable_reason(args)
        if blocked:
            raise Refused(blocked)
        name = str(args.get("process") or args.get("name") or "").strip()
        if not name:
            raise DaemonError("app.close needs 'process' - an image name such as notepad.exe")
        rows = sysinfo.find_processes(name)
        targets = rows.get("processes", [])
        if not targets:
            # `find_processes` distinguishes "no such process" from "that is
            # not a name I can look up", and reporting both as the first is how
            # a rejected query came to read as a program that was not running
            # - which is a claim about the machine, and it was wrong.
            detail = rows.get("note") or f"no process named {name!r} is running"
            raise DaemonError(
                f"{detail}. 'system.processes' lists what is, and "
                f"'app.launch' starts something."
            )
        force = bool(args.get("force"))
        own = self._admin_own_chain()
        closed, failed = [], []
        for row in targets:
            pid, proc_name = int(row["pid"]), str(row["name"])
            if pid in own:
                # Skipped, not fatal: "close the stray interpreters" is a
                # reasonable request on a machine where the assistant is itself
                # a python.exe, and refusing the whole call would make the tool
                # unable to answer it at all. What is reported is what was left.
                failed.append({"pid": pid, "name": proc_name,
                               "error": "this is the assistant, or something it "
                                        "was started from - not closing it"})
                continue
            if proc_name.lower() in self._ADMIN_UNKILLABLE:
                failed.append({"pid": pid, "name": proc_name,
                               "error": "protected process"})
                continue
            argv = ["taskkill", "/PID", str(pid)]
            if force:
                argv.append("/F")
            result = sysinfo.run_capture(argv, timeout=20.0)
            if result.get("exit_code") == 0:
                closed.append({"pid": pid, "name": proc_name})
            else:
                failed.append({
                    "pid": pid, "name": proc_name,
                    "error": (result.get("stderr") or result.get("stdout")
                              or "").strip()[:200] or "taskkill failed",
                })
        return {"requested": name, "force": force, "closed": closed,
                "failed": failed, "closed_count": len(closed)}

    # ------------------------------------------ administration, tier 3

    # These five are the highest-consequence tools in the registry, and the
    # thing they have in common is that the tier name says `IRREVERSIBLE`. It
    # does not have to be true.
    #
    # Every one of them writes a row to `state/admin_undo.jsonl` *before* it
    # changes anything, recording the exact inverse - the backup file to import,
    # the service action to run, the version to reinstall. The preview shows the
    # undo, the result carries the undo, and the row is written whether the
    # action succeeds or fails, so the journal cannot lose a failure.
    #
    # The journal is a plain append-only text file rather than a table because
    # its whole job is to be readable after something has gone wrong, by a
    # human, with a text editor. A database that will not open is a poor place
    # to keep the record of the thing that broke it.
    _ADMIN_UNDO_FILE = "admin_undo.jsonl"

    def _admin_undo_path(self) -> Path:
        return Path(self.state) / self._ADMIN_UNDO_FILE

    def _admin_journal(self, tool: str, args: dict, *, undo: Any,
                       note: str = "") -> dict[str, Any]:
        """Record how to reverse this, before it happens. Returns the row.

        Named for the section it was born in, and now serving every Tier 3
        including `file.empty_recycle_bin`, which has no inverse at all and so
        passes `undo=None` with a note saying so. Not renamed, because renaming
        it would touch D60's committed tests for the sake of a name; the smell
        is recorded in D63 instead.

        Why the no-inverse case still gets a row: a journal that only records
        what it can undo cannot tell you which of its entries were the ones you
        cannot take back. "Nothing here is reversible" is information, and it
        has to be findable.

        The `ok` field is written later, by the caller, as a second row with
        the same `id`. Appending twice beats rewriting: a half-written journal
        is still a journal, and a rewritten one that fails mid-rewrite is not.

        A journal that cannot be written is not fatal. An action whose undo we
        failed to record is still the user's call - refusing would turn "we
        could not save a note" into "we cannot help you", which is a worse
        outcome than the missing note. The caller is told either way.
        """
        row = {
            "id": time.strftime("%Y%m%d-%H%M%S") + f"-{self._pending_seq:03d}",
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "tool": tool,
            "channel": self._channel(args) if isinstance(args, dict) else "pc",
            "args": _redact(args) if isinstance(args, dict) else {},
            "undo": undo,
            "note": note,
            "ok": None,
        }
        path = self._admin_undo_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, sort_keys=True, default=str) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            row["journal_error"] = str(exc)
        return row

    def _admin_close_journal(self, row: dict[str, Any], *,
                            ok: bool, result: Any = "") -> None:
        """Append the outcome against the row that said what would happen."""
        if row.get("journal_error"):
            return
        done = dict(row)
        done["ok"] = bool(ok)
        done["result"] = result
        done["closed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            with self._admin_undo_path().open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(done, sort_keys=True, default=str) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except OSError:
            pass

    def _admin_undo_tip(self, row: dict[str, Any]) -> str:
        """One line for the result, saying how to put it back."""
        undo = row.get("undo")
        if isinstance(undo, dict) and undo.get("command"):
            return (f"recorded in {self._ADMIN_UNDO_FILE}; to put it back run: "
                    f"{undo['command']}")
        if row.get("journal_error"):
            return (f"could NOT be recorded to the undo journal "
                    f"({row['journal_error']})")
        return f"recorded in {self._ADMIN_UNDO_FILE}"

    # -- the preview answers first, for every one of these ------------------

    def _preview_registry_write(self, args: dict) -> dict:
        key = str(args.get("key", ""))
        name = str(args.get("name", ""))
        kind = str(args.get("type", "REG_SZ")).upper()
        delete = bool(args.get("delete"))
        refusal = (sysinfo.registry_key_refusal(key)
                   or sysinfo.registry_value_refusal(name)
                   or (None if kind in sysinfo.REG_VALUE_TYPES else
                       f"type {kind!r} is not one of "
                       f"{', '.join(sysinfo.REG_VALUE_TYPES)}")
                   or (None if delete else
                       sysinfo.registry_data_refusal(kind, args.get("data"))))
        before = sysinfo.registry_read(key) if not refusal else {}
        current = (before.get("values") or {}).get(name)
        backup = str(self._registry_backup_path(key))
        return {
            "action": ("remove one registry value"
                       if delete else "set one registry value"),
            "key": key,
            "name": name,
            "type": kind,
            "data": None if delete else args.get("data"),
            "deleting": delete,
            "key_exists": before.get("exists", False),
            "current_value": current,
            "changes_anything": current is not None,
            "backup_of_the_key": None if not before.get("exists") else backup,
            "undo": (f'reg import "{backup}"' if before.get("exists")
                     else "remove the value again (the key did not exist)"),
            "reversible": True,
            "would_be_refused": refusal,
            "note": "the key is exported to a .reg file before the write, so "
                    "this is reversible even though the tier says otherwise",
        }

    def _registry_backup_path(self, key: str) -> Path:
        """A stable, readable path for this key's backup."""
        slug = re.sub(r"[^A-Za-z0-9]+", "_", key).strip("_") or "key"
        return (Path(self.state) / "registry_backups"
                / f"{slug}-{time.strftime('%Y%m%d-%H%M%S')}.reg")

    def _preview_service_start_stop(self, args: dict) -> dict:
        name = str(args.get("service", ""))
        action = str(args.get("action", "")).lower()
        refusal = self._service_refusal(name, action)
        state = sysinfo.service_state(name) if not refusal else {}
        prior = state.get("state_name")
        opposite = {"start": "stop", "stop": "start"}.get(action, "restart")
        return {
            "action": f"{action} a Windows service",
            "service": name,
            "requested": action,
            "current_state": prior,
            "currently_running": state.get("running"),
            "command": f'sc {action} {name}' if name else "",
            "undo": (f'sc {opposite} {name}' if name and action in
                     ("start", "stop") else
                     "restarting twice is not an undo"),
            "reversible": action in ("start", "stop"),
            "would_be_refused": refusal,
            "note": ("stopping a boot-critical or security service leaves "
                     "nothing to undo, which is why those are refused"),
        }

    def _service_refusal(self, name: str, action: str) -> str | None:
        """Why this service action will not run, or None."""
        if action not in ("start", "stop", "restart"):
            return (f"action {action!r} is not one of start, stop, restart")
        if not name:
            return "service is required - the name it is registered under"
        if not sysinfo.SERVICE_NAME_OK.match(name):
            return (f"{name!r} is not a service name. Windows allows letters, "
                    f"digits, dot, dash, underscore and backslash - nothing "
                    f"else. It is refused rather than trimmed, because a name "
                    f"with a space or a quote in it is a mistake, not a "
                    f"service")
        state = sysinfo.service_state(name)
        if not state.get("exists"):
            return (f"there is no service named {name!r}. Run "
                    f"system.services to see the ones that exist")
        if action in ("stop", "restart"):
            blocked = sysinfo.SERVICE_NO_STOP.get(name) or \
                sysinfo.SERVICE_NO_STOP.get(name.lower())
            if blocked:
                return (f"{name!r} will not be {action}ped: {blocked}. This is "
                        f"not a judgement about whether it is safe - it is "
                        f"that stopping it cannot be undone from inside the "
                        f"machine")
        return None

    def _preview_package_install(self, args: dict) -> dict:
        package = str(args.get("package", ""))
        managers = sysinfo.package_managers()
        available = [m["name"] for m in managers.get("managers", [])
                     if m.get("usable")]
        asked = str(args.get("manager", "")).lower()
        chosen = asked or (available[0] if available else "")
        refusal = self._package_refusal(package, asked, available)
        argv: list[str] = []
        if chosen and not refusal:
            try:
                argv = sysinfo.manager_install_argv(
                    chosen, package, str(args.get("version", "")))
            except FileNotFoundError:
                refusal = f"{chosen} is not installed on this machine"
        return {
            "action": "install a package",
            "package": package,
            "manager": chosen or "(none found)",
            "managers_here": available,
            "version": str(args.get("version", "")) or "(latest)",
            "command": subprocess.list2cmdline(argv) if argv else "",
            "undo": (f'uninstall it again: {chosen} uninstall'
                     if chosen else ""),
            "reversible": True,
            "would_be_refused": refusal,
            "note": managers.get("note", ""),
        }

    def _package_refusal(self, package: str, asked: str,
                         available: list[str]) -> str | None:
        if not package:
            return ("package is required - the id as its manager knows it, "
                    "e.g. notepad++")
        if not sysinfo.PACKAGE_NAME_OK.match(package):
            return (f"{package!r} is not a package id. Windows package ids are "
                    f"letters, digits, dot, dash, underscore, plus and spaces")
        if asked and asked not in available:
            return (f"{asked!r} is not a package manager here. Available: "
                    f"{', '.join(available) or 'none'} - both are checked, "
                    f"because being on PATH is not the same as being "
                    f"installed")
        if not available:
            return ("no package manager was found on this machine, so nothing "
                    "can be installed")
        return None

    def _preview_package_uninstall(self, args: dict) -> dict:
        package = str(args.get("package", ""))
        owners = self._package_owners(package)
        managers = [name for name, _ in owners]
        refusal: str | None = None
        if not package:
            refusal = "package is required"
        elif not sysinfo.PACKAGE_NAME_OK.match(package):
            refusal = (f"{package!r} is not a package id. Letters, digits, "
                       f"dot, dash, underscore, plus and spaces")
        elif len(owners) > 1:
            refusal = (f"{package!r} is claimed by more than one package "
                       f"manager ({', '.join(managers)}). Which one to remove "
                       f"it from is the user's decision, not a default")
        elif not owners:
            installed = sysinfo.package_list(query=package, limit=200)
            match = [p for p in installed.get("packages", [])
                     if p["name"].lower() == package.lower()]
            refusal = (
                f"{package!r} is not a package any manager here claims to "
                f"have installed"
                + (f", although Windows lists {len(match)} program(s) with "
                   f"that name"
                   if match else
                   " - uninstalling something this machine did not install is "
                   "not a package operation, it is a guess"))
        argv: list[str] = []
        undo: Any = "no version recorded, so reinstalling the same version "\
                    "is not guaranteed"
        if owners and not refusal:
            name, version = owners[0]
            argv = sysinfo.manager_uninstall_argv(name, package)
            undo = (f"reinstall {version}: {subprocess.list2cmdline(sysinfo.manager_install_argv(name, package, version))}"
                    if version else
                    f"reinstall the latest: {subprocess.list2cmdline(sysinfo.manager_install_argv(name, package))}")
        return {
            "action": "uninstall a package",
            "package": package,
            "manager": managers[0] if managers else "(none claims it)",
            "installed_version": owners[0][1] if owners else "",
            "command": subprocess.list2cmdline(argv) if argv else "",
            "undo": undo,
            "reversible": bool(owners),
            "would_be_refused": refusal,
            "note": "an installed program and a manager-owned package are "
                    "different facts; only the second can be uninstalled here",
        }

    def _package_owners(self, package: str) -> list[tuple[str, str]]:
        """Every manager that claims this package, with the version it reports.

        Checked against the listing's *contents*, never its exit code: measured
        on this machine, `choco list <missing> --exact` exits 0 with empty
        output, so an exit code cannot tell "installed" from "not installed".
        """
        if not package:
            return []
        found: list[tuple[str, str]] = []
        for name in sysinfo.PACKAGE_MANAGERS:
            if sysinfo.package_owned_by(name, package):
                found.append((name, sysinfo.package_installed_version(name, package)))
        return found

    def _preview_power_shutdown(self, args: dict) -> dict:
        action = str(args.get("action", "")).lower()
        delay = args.get("delay_seconds")
        refusal, seconds = self._power_refusal(action, delay)
        argv: list[str] = []
        if action and not refusal:
            argv = sysinfo.power_argv(action, seconds)
        return {
            "action": f"{action} this machine" if action else "(no action)",
            "requested": action,
            "delay_seconds": seconds,
            "command": subprocess.list2cmdline(argv) if argv else "",
            "cancel_with": sysinfo.POWER_CANCEL_HINT if action in
            ("shutdown", "restart") else "",
            "undo": (sysinfo.POWER_CANCEL_HINT if action in
                     ("shutdown", "restart") else "nothing to undo"),
            "reversible": action == "cancel" or bool(action in
                                                     ("shutdown", "restart")),
            "would_be_refused": refusal,
            "note": ("the delay is why this is reversible: Windows will not "
                     "act on it until the countdown ends, and the countdown "
                     "can be stopped"),
        }

    def _power_refusal(self, action: str,
                       delay: Any) -> tuple[str | None, int | None]:
        """Why this power action will not run, and the delay it would use."""
        if action not in ("shutdown", "restart", "cancel", "logoff"):
            return (f"action {action!r} is not one of shutdown, restart, "
                    f"cancel, logoff", None)
        if action == "cancel":
            return None, None
        seconds = sysinfo.POWER_DEFAULT_DELAY_S
        if delay is not None:
            # A float is refused before `int()` is tried, because `int(1.5)`
            # is 1 - it succeeds, and then the reason the user is given is
            # "too short to cancel" rather than "that is not a whole number
            # of seconds". Two different mistakes, two different fixes, and
            # the first one reported would have sent them looking at the
            # delay's *size* rather than its *type*.
            if isinstance(delay, bool) or (
                    isinstance(delay, float) and not delay.is_integer()):
                return (f"delay_seconds {delay!r} is not a whole number of "
                        f"seconds", None)
            try:
                seconds = int(delay)
            except (TypeError, ValueError):
                return (f"delay_seconds {delay!r} is not a whole number of "
                        f"seconds", None)
        if seconds < sysinfo.POWER_MIN_DELAY_S:
            return (f"a {seconds}s delay is too short to cancel: this tool "
                    f"will not schedule a shutdown you cannot stop, so the "
                    f"minimum is {sysinfo.POWER_MIN_DELAY_S}s "
                    f"({subprocess.list2cmdline(sysinfo.power_argv(action, sysinfo.POWER_MIN_DELAY_S))} "
                    f"then {sysinfo.POWER_CANCEL_HINT})", seconds)
        if seconds > sysinfo.POWER_MAX_DELAY_S:
            return (f"a {seconds}s delay is longer than the "
                    f"{sysinfo.POWER_MAX_DELAY_S}s maximum", seconds)
        return None, seconds

    # -- and then the actions ------------------------------------------------

    def _exec_registry_write(self, args: dict) -> Any:
        key = str(args.get("key", ""))
        name = str(args.get("name", ""))
        kind = str(args.get("type", "REG_SZ")).upper()
        data = args.get("data")
        delete = bool(args.get("delete"))
        refusal = self._preview_registry_write(args).get("would_be_refused")
        if refusal:
            raise Refused(str(refusal))
        before = sysinfo.registry_read(key)
        backup = ""
        undo: Any
        if before.get("exists"):
            target = self._registry_backup_path(key)
            target.parent.mkdir(parents=True, exist_ok=True)
            exported = sysinfo.run_capture(
                ["reg.exe", "export", key, str(target), "/y"], timeout=60.0)
            if exported.get("exit_code") == 0 and target.exists():
                backup = str(target)
                undo = {"kind": "reg_import", "command":
                        f'reg import "{backup}"',
                        # The argv as well as the string, because a command
                        # that has to be re-parsed to be run is a command that
                        # can be run differently than it was recorded - and
                        # the one thing an undo must not be is approximate.
                        "argv": ["reg.exe", "import", backup],
                        "file": backup}
            else:
                undo = {"kind": "restore_value", "key": key, "name": name,
                        "previous": (before.get("values") or {}).get(name),
                        "note": "the key could not be exported; the previous "
                                "value is recorded here instead"}
        else:
            undo = {"kind": "remove_value", "key": key, "name": name}
        row = self._admin_journal(
            "registry.write", args, undo=undo,
            note=f"backup: {backup}" if backup else "no backup needed: the key "
                                               f"did not exist")
        written = sysinfo.registry_write(key, name, kind, data, delete=delete)
        if not written.get("ok"):
            self._admin_close_journal(row, ok=False,
                                      result=str(written.get("error")))
            raise DaemonError(str(written.get("error")))
        self._admin_close_journal(row, ok=True, result=f"{kind} {name}")
        after = sysinfo.registry_read(key)
        return {"ok": True, "key": key, "name": name, "removed": delete,
                "type": None if delete else kind,
                "value_now": (after.get("values") or {}).get(name),
                "backup": backup or None,
                "undo": row.get("undo"),
                "undo_tip": self._admin_undo_tip(row)}

    def _exec_service_start_stop(self, args: dict) -> Any:
        name = str(args.get("service", ""))
        action = str(args.get("action", "")).lower()
        refusal = self._service_refusal(name, action)
        if refusal:
            raise Refused(refusal)
        state = sysinfo.service_state(name)
        prior = state.get("state_name")
        opposite = {"start": "stop", "stop": "start"}.get(action)
        undo: Any = ({"kind": "sc", "command": f"sc {opposite} {name}",
                      "reason": f"it was {prior} before"}
                     if opposite else
                     {"kind": "none",
                      "note": "a restart leaves the service running either "
                              "way; the undo is to stop it"})
        row = self._admin_journal("service.start_stop", args, undo=undo,
                                  note=f"was {prior}")
        result = sysinfo.run_capture(
            ["sc.exe", action, name], timeout=120.0)
        ok = result.get("exit_code") == 0
        self._admin_close_journal(row, ok=ok,
                                  result=f"exit {result.get('exit_code')}")
        if not ok:
            raise DaemonError(
                f"sc {action} {name} failed with exit "
                f"{result.get('exit_code')}: "
                f"{(result.get('stderr') or result.get('stdout') or '').strip()[:200]}")
        after = sysinfo.service_state(name)
        return {"ok": True, "service": name, "action": action,
                "state_before": prior, "state_after": after.get("state_name"),
                "undo": undo, "undo_tip": self._admin_undo_tip(row)}

    def _exec_package_install(self, args: dict) -> Any:
        package = str(args.get("package", ""))
        version = str(args.get("version", ""))
        available = [m["name"] for m in
                     sysinfo.package_managers().get("managers", [])
                     if m.get("usable")]
        asked = str(args.get("manager", "")).lower()
        manager = asked or (available[0] if available else "")
        refusal = self._package_refusal(package, asked, available)
        if refusal:
            raise Refused(refusal)
        argv = sysinfo.manager_install_argv(manager, package, version)
        row = self._admin_journal(
            "package.install", args,
            undo={"kind": "package_uninstall", "manager": manager,
                  "command": subprocess.list2cmdline(
                      sysinfo.manager_uninstall_argv(manager, package)),
                  "reason": "it was not installed before"})
        result = sysinfo.run_capture(argv, timeout=1800.0)
        ok = result.get("exit_code") == 0
        self._admin_close_journal(row, ok=ok,
                                  result=f"exit {result.get('exit_code')}")
        return {"ok": ok, "package": package, "manager": manager,
                "version": version or "(latest)",
                "command": subprocess.list2cmdline(argv),
                "exit_code": result.get("exit_code"),
                "stdout": result.get("stdout", "")[-800:],
                "stderr": result.get("stderr", "")[-800:],
                "undo": row.get("undo"), "undo_tip": self._admin_undo_tip(row)}

    def _exec_package_uninstall(self, args: dict) -> Any:
        package = str(args.get("package", ""))
        preview = self._preview_package_uninstall(args)
        if preview.get("would_be_refused"):
            raise Refused(str(preview["would_be_refused"]))
        manager, version = self._package_owners(package)[0]
        argv = sysinfo.manager_uninstall_argv(manager, package)
        row = self._admin_journal(
            "package.uninstall", args,
            undo={"kind": "package_install", "manager": manager,
                  "version": version,
                  "command": subprocess.list2cmdline(
                      sysinfo.manager_install_argv(manager, package, version)),
                  "reason": f"it was {version}" if version else
                            "the version was not reported, so reinstalling "
                            "may give a newer one"})
        result = sysinfo.run_capture(argv, timeout=1800.0)
        ok = result.get("exit_code") == 0
        self._admin_close_journal(row, ok=ok,
                                  result=f"exit {result.get('exit_code')}")
        if not ok:
            raise DaemonError(
                f"{manager} uninstall {package} failed with exit "
                f"{result.get('exit_code')}: "
                f"{(result.get('stderr') or result.get('stdout') or '').strip()[:200]}")
        return {"ok": True, "package": package, "manager": manager,
                "removed_version": version,
                "command": subprocess.list2cmdline(argv),
                "undo": row.get("undo"), "undo_tip": self._admin_undo_tip(row)}

    def _exec_power_shutdown(self, args: dict) -> Any:
        action = str(args.get("action", "")).lower()
        refusal, seconds = self._power_refusal(action,
                                              args.get("delay_seconds"))
        if refusal:
            raise Refused(refusal)
        argv = sysinfo.power_argv(action, seconds)
        if action == "cancel":
            result = sysinfo.run_capture(argv, timeout=60.0)
            code = result.get("exit_code")
            # 1116 is "no shutdown was in progress". Reported plainly, because
            # the alternative - reporting success for a machine that was never
            # going to shut down - is a lie with a cheerful shape.
            if code == 1116:
                return {"ok": False, "action": action, "exit_code": code,
                        "cancelled": False,
                        "detail": "nothing was scheduled to cancel - no "
                                  "shutdown or restart was pending",
                        "command": subprocess.list2cmdline(argv)}
            if code != 0:
                raise DaemonError(
                    f"shutdown /a failed with exit {code}: "
                    f"{(result.get('stderr') or '').strip()[:200]}")
            return {"ok": True, "action": action, "cancelled": True,
                    "command": subprocess.list2cmdline(argv)}
        row = self._admin_journal(
            "power.shutdown", args,
            undo={"kind": "power_cancel",
                  # Built by the same function that built the action, rather
                  # than as a literal `["shutdown", "/a"]` - a second copy of
                  # a command is a second chance to have it wrong, and the
                  # literal had already been proved wrong by the time this
                  # method ran at all.
                  "command": subprocess.list2cmdline(
                      sysinfo.power_argv("cancel")),
                  "argv": sysinfo.power_argv("cancel"),
                  "reason": f"it was not scheduled before; cancel it within "
                            f"{seconds}s if that was not the intention"},
            note=f"{seconds}s to cancel")
        result = sysinfo.run_capture(argv, timeout=60.0)
        ok = result.get("exit_code") == 0
        self._admin_close_journal(row, ok=ok,
                                  result=f"exit {result.get('exit_code')}")
        if not ok:
            raise DaemonError(
                f"shutdown failed with exit {result.get('exit_code')}: "
                f"{(result.get('stderr') or '').strip()[:200]}")
        return {"ok": True, "action": action, "delay_seconds": seconds,
                "command": subprocess.list2cmdline(argv),
                "cancel_with": sysinfo.POWER_CANCEL_HINT,
                "undo": row.get("undo"), "undo_tip": self._admin_undo_tip(row)}

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

    # ----------------------------------------------------------------- teaching

    _TEACH_SOURCE_MAX = 20000

    def _teach_ledger(self) -> teach.Ledger:
        """The record of what has been taught.

        Under the state directory, chosen here rather than taken as an argument.
        A teaching tool that lets the caller name where it writes is an arbitrary
        write wearing a pedagogy name - the same argument that settled where
        Telegram drafts go.
        """
        return teach.Ledger(self.state / teach.LEDGER_NAME)

    def _teach_refusals(self, args: dict, *, kind: str) -> list[str]:
        """Everything wrong with this call, as sentences. Never raises.

        Both halves of the D32 rule live in one function because both halves
        have to be the *same* function: the preview asks this, the executor asks
        this, and a refusal list that agrees with itself is not evidence that it
        agrees with the code it guards (D61).
        """
        out: list[str] = []
        topic_bad = teach.topic_refusal(args.get("topic") or args.get("subject"))
        if topic_bad:
            out.append(topic_bad)
        style_bad = teach.style_refusal(args.get("style"))
        if style_bad:
            out.append(style_bad)
        if kind == teach.KIND_RECALL:
            q_bad = teach.question_refusal(args.get("question"))
            if q_bad:
                out.append(q_bad)
        if kind == teach.KIND_SUMMARY:
            # One question, asked once. The first version also had an explicit
            # "neither source nor text" branch below this one, and a call with
            # neither got the same sentence twice in the same list - which reads
            # as two separate faults to whoever is shown the dialog, and is the
            # two-copies shape that produced nine of the ten defects in D61.
            src_bad = teach.source_refusal(args.get("source") or args.get("text"))
            if src_bad:
                out.append(src_bad)
        return [r for r in out if r]

    def _teach_source(self, args: dict) -> tuple[str, str]:
        """The material to condense, and where it came from.

        The path is judged by the gate, not here - this module holds no opinion
        about what the user may read, and the gate holds the approved roots.
        `text` is checked for emptiness first so an empty path never becomes a
        request to read the whole drive.
        """
        pasted = str(args.get("text") or "")
        if pasted.strip():
            return pasted[:self._TEACH_SOURCE_MAX], "pasted"
        raw = str(args.get("source") or "").strip()
        if not raw:
            return "", ""
        path = Path(raw)
        bad = self.gate.path_refusal(raw)
        if bad:
            raise Refused(bad)
        if not path.exists():
            raise Refused(f"{path} is not there. The ledger records what was "
                          "taught, not what was meant to be.")
        try:
            body = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise Refused(f"{path} could not be read: {exc}") from exc
        return body[:self._TEACH_SOURCE_MAX], str(path)

    def _teach_turn(self, kind: str, args: dict) -> dict[str, Any]:
        """One teaching turn: prompt, stream, record.

        The recording is the point. Without a row, `teach.progress` has nothing
        to report, `teach.recall` has nothing to test and `teach.quiz` does not
        know what was taught - so the ledger is written after the answer comes
        back and never before, because a row for a turn that failed would be a
        record of something that did not happen.
        """
        topic = str(args.get("topic") or args.get("subject") or "").strip()
        style = str(args.get("style") or teach.DEFAULT_STYLE).strip().lower()
        ledger = self._teach_ledger()
        source_text = ""
        where = ""
        if kind == teach.KIND_SUMMARY:
            source_text, where = self._teach_source(args)
        prompt = teach.build_prompt(
            kind, topic=topic,
            question=str(args.get("question") or "").strip(),
            style=style, ledger=ledger, source_text=source_text,
        )
        result = self._brain_call("stream", prompt=prompt,
                                  tier=str(Tier.CONSEQUENTIAL))
        text = str(result.get("text") or "")
        if not text.strip():
            return {"taught": False, "topic": topic, "kind": kind,
                    "note": "the model returned nothing, so there is nothing to "
                            "record. An empty turn is not a teaching turn.",
                    "prompt_chars": len(prompt)}
        row = ledger.record(kind, topic, text,
                            style=style,
                            question=str(args.get("question") or "").strip(),
                            source=where)
        return {"taught": True, "kind": kind, "topic": topic, "style": style,
                "answer": row["text"], "chars": row["chars"],
                "truncated": row["truncated"], "at": row["at"],
                "recorded": sum(1 for _ in ledger.rows()),
                "ledger": str(ledger.path),
                "prompt_chars": len(prompt),
                "source": where}

    def _preview_teach_turn(self, kind: str, args: dict) -> dict[str, Any]:
        """What will be asked, and what will be written down."""
        topic = str(args.get("topic") or args.get("subject") or "")
        why = self._teach_refusals(args, kind=kind)
        return {
            "action": f"teach one {kind} turn and record it in the teaching "
                      "ledger",
            "kind": kind,
            "topic": topic,
            "style": str(args.get("style") or teach.DEFAULT_STYLE),
            "question": str(args.get("question") or ""),
            "source": str(args.get("source") or ""),
            "recorded_in": str(self._teach_ledger().path),
            "cost": "streams from the brain, so this is a T2 turn: the child "
                    "model has to be resident, and the arbiter decides whether "
                    "it may be (D1, D50).",
            "reversible": "the row can be deleted by hand; nothing outside the "
                          "ledger is touched.",
            "would_be_refused": why or None,
        }

    def _preview_teach_explain(self, args: dict) -> dict[str, Any]:
        return self._preview_teach_turn(teach.KIND_EXPLAIN, args)

    def _preview_teach_quiz(self, args: dict) -> dict[str, Any]:
        return self._preview_teach_turn(teach.KIND_QUIZ, args)

    def _preview_teach_recall(self, args: dict) -> dict[str, Any]:
        return self._preview_teach_turn(teach.KIND_RECALL, args)

    def _preview_teach_summarise(self, args: dict) -> dict[str, Any]:
        return self._preview_teach_turn(teach.KIND_SUMMARY, args)

    def _preview_teach_plan_curriculum(self, args: dict) -> dict[str, Any]:
        return self._preview_teach_turn(teach.KIND_CURRICULUM, args)

    def _exec_teach_explain(self, args: dict) -> Any:
        why = self._teach_refusals(args, kind=teach.KIND_EXPLAIN)
        if why:
            raise Refused("; ".join(why))
        return self._teach_turn(teach.KIND_EXPLAIN, args)

    def _exec_teach_quiz(self, args: dict) -> Any:
        why = self._teach_refusals(args, kind=teach.KIND_QUIZ)
        if why:
            raise Refused("; ".join(why))
        return self._teach_turn(teach.KIND_QUIZ, args)

    def _exec_teach_recall(self, args: dict) -> Any:
        why = self._teach_refusals(args, kind=teach.KIND_RECALL)
        if why:
            raise Refused("; ".join(why))
        return self._teach_turn(teach.KIND_RECALL, args)

    def _exec_teach_summarise(self, args: dict) -> Any:
        why = self._teach_refusals(args, kind=teach.KIND_SUMMARY)
        if why:
            raise Refused("; ".join(why))
        return self._teach_turn(teach.KIND_SUMMARY, args)

    def _exec_teach_plan_curriculum(self, args: dict) -> Any:
        why = self._teach_refusals(args, kind=teach.KIND_CURRICULUM)
        if why:
            raise Refused("; ".join(why))
        return self._teach_turn(teach.KIND_CURRICULUM, args)

    def _exec_teach_progress(self, args: dict) -> Any:
        """What has been covered. No model, no lease, no VRAM.

        The only teaching tool that stays T0, and the only one that would still
        work with the brain unloaded - which is the honest reason it can be a
        free read rather than an excuse for one.
        """
        ledger = self._teach_ledger()
        out = ledger.progress(limit=_delegate_int(args.get("limit"), 50))
        out["cost"] = "reads one file; no model, no VRAM"
        return out

    # ---------------------------------------------------------------- delegation
    #
    # A worker here is a bounded command in an approved workspace, tracked for
    # its whole life. The difference from `shell.run` is not that it can do more -
    # it runs the same argv through the same screen - it is that it can afterwards
    # be *asked about*. An id, a budget, a status readable after the fact and an
    # audit row are what give `delegate.status`, `delegate.review` and
    # `delegate.audit` something to read. `shell.run` returns a result and forgets
    # it, which is the right shape for a command and the wrong shape for a task.
    #
    # Two things this section deliberately does not have. There is no `vram_gib`
    # argument, because there is nothing behind it: D1 says models take turns and
    # the arbiter is the only grantor, so a worker that needs a model has to take
    # a lease like anything else, and the absence of the argument is the
    # guarantee rather than a promise. And there is no "ask the model whether this
    # is a good idea" - `review` and `audit` are evidence, not judgement, because
    # putting a model in a Tier 0 read would cost VRAM on a call the user expects
    # to be free (D50). Judging the evidence is the brain turn's job.

    def _delegate_roots(self) -> list[str]:
        """The approved workspaces, as the delegation tools see them."""
        return list(getattr(self, "manifests", None).roots()) \
            if hasattr(self, "manifests") else []

    def _delegate_workspace(self, args: dict) -> str:
        """Where a task runs, resolved the way `shell.run` resolves its cwd.

        Shared deliberately rather than reimplemented: a second rule for "inside
        a workspace" is how one of two copies ends up not having it, and the
        difference between them is a hole with no symptom.
        """
        raw = str(args.get("workspace") or args.get("path") or "").strip()
        roots = self._delegate_roots()
        if not raw:
            if len(roots) == 1:
                return str(Path(roots[0]).resolve())
            if not roots:
                raise Refused(
                    "no workspace has been approved, so there is nowhere for a "
                    "task to run. Approve one first; nothing can be delegated "
                    "until then"
                )
            raise Refused(
                "several workspaces are approved, so a task needs to say which "
                f"one. Approved: {', '.join(roots[:8])}"
            )
        target = Path(raw)
        if not target.is_absolute() and len(roots) == 1:
            target = Path(roots[0]) / target
        why = self.gate.path_refusal(str(target))
        if why:
            raise Refused(f"delegate workspace refused - {why}")
        return str(target.resolve())

    def _delegate_argv(self, args: dict) -> list[str]:
        why = delegate.argv_refusal(args.get("command"))
        if why:
            raise Refused(f"delegate command refused - {why}")
        return [str(a) for a in args["command"]]

    def _delegate_arg_paths(self, argv: list[str], cwd: str) -> list[str]:
        r"""The path-ish arguments, each resolved against the task's cwd.

        Copied in behaviour from `_exec_shell_run`'s re-check, including the
        relative-to-cwd resolution: a relative name is meaningless to the gate
        until it knows what it is relative *to*, and refusing `del notes.txt`
        because the string does not start with a drive letter would refuse the
        ordinary case to prevent the rare one.
        """
        out: list[str] = []
        for _, path in self._admin_arg_paths(argv):
            candidate = Path(cwd) / path if not Path(path).is_absolute() else path
            out.append(str(candidate))
        return out

    def _delegate_refusals(self, args: dict) -> list[str]:
        """Every reason this assignment would be refused, in one pass.

        Returns rather than raises, because the preview builder must never
        raise: `_preview_for` has no exception handling, so a preview that
        throws takes the whole confirmation down with it and the user sees an
        error instead of the thing they were asked to approve. The executor
        asks the same questions and raises, which is what makes the approved
        payload the executed payload (D32).
        """
        why: list[str] = []
        for raw, why_one in (
            ("task id", delegate.task_id_refusal(self._delegate_task_id(args))),
            ("goal", delegate.goal_refusal(args.get("goal"))),
            ("budget_s", delegate.budget_refusal(args.get("budget_s"))),
            ("command", delegate.argv_refusal(args.get("command"))),
        ):
            if why_one:
                why.append(f"{raw}: {why_one}")
        if why:
            return why
        try:
            cwd = self._delegate_workspace(args)
        except DaemonError as exc:
            return [f"workspace: {exc}"]
        try:
            argv = self._delegate_argv(args)
        except DaemonError as exc:
            return [str(exc)]
        for path in self._delegate_arg_paths(argv, cwd):
            reason = self.gate.path_refusal(path)
            if reason:
                why.append(f"argument {path}: {reason}")
        return why

    @staticmethod
    def _delegate_task_id(args: dict) -> Any:
        """The task id, however the caller spelled it.

        One reader for three spellings, because the first version of this read
        the aliases in six places by hand: `_delegate_task` accepted `task`,
        `task_id` and `id`, while `_delegate_refusals` accepted only `task`. A
        caller who said `task_id` therefore got a task lookup that worked and an
        assignment that was refused for having no id - the tool reporting two
        different answers about the same field of the same call. Six copies of
        a rule is five ways for the rule to be wrong (D58).
        """
        return args.get("task") or args.get("task_id") or args.get("id")

    def _delegate_task(self, args: dict) -> delegate.Task:
        """Look up a task by id, or refuse naming what is on offer.

        Used by every read that takes a `task`. An unknown id is a refusal that
        lists the ids in hand, because "no such task" on its own cannot be acted
        on and the caller has no other way to learn the names.
        """
        raw = self._delegate_task_id(args)
        why = delegate.task_id_refusal(raw)
        if why:
            raise Refused(f"delegate task refused - {why}")
        task = self.tasks.get(str(raw))
        if task is None:
            held = ", ".join(self.tasks.ids()) or "none"
            raise Refused(
                f"no task called {str(raw)!r}. Tasks in hand: {held}"
            )
        return task

    # -- the readings -----------------------------------------------------------

    def _exec_delegate_plan(self, args: dict) -> Any:
        """What is measurably true about the workspace a task would run in.

        Named `plan` because that is what a caller asks for, and it is a
        *briefing*, not a plan: it reports measurements and the constraints that
        will apply. It produces no sequence of steps, because the one thing that
        makes a generated plan dangerous is that it reads like an authority -
        and a wall of "unknown" beside facts the assistant could have read is an
        invitation to fill the gap from training data, which is how a plan comes
        to name a directory the user never approved.
        """
        root = self._delegate_workspace(args)
        brief = delegate.workspace_briefing(root)
        return {
            "action": "briefing: what is measurably true about this workspace",
            "kind": "briefing",
            "is_a_plan": False,
            "workspace": root,
            "what_is_there": brief,
            "constraints": delegate.constraints_for(
                root,
                self._delegate_roots(),
                deny_paths=self.gate.deny_paths,
                self_paths=self.gate.self_paths,
            ),
            "note": (
                "this is a set of measurements, not a plan. Nothing here has "
                "been decided for you; a plan that claims otherwise was not "
                "produced by this tool"
            ),
        }

    def _exec_delegate_brainstorm(self, args: dict) -> Any:
        """The constraints, without the measurements.

        Separate from `plan` because the two answer different questions and the
        model reaches for the wrong one. `plan` answers "what is here"; this
        answers "what will be refused", which is the part a generated plan
        usually gets wrong - it proposes work in a directory that was never
        approved, or an edit to the assistant's own source, and both are refused
        at the gate after the thinking has been done.

        The constraints are read from the same gate the refusal will come from,
        rather than copied into a prompt: a rule quoted in a prompt and a rule
        enforced in code are two rules, and they drift.
        """
        root = self._delegate_workspace(args)
        topic = str(args.get("topic") or args.get("question") or "").strip()
        return {
            "action": "briefing: the constraints any plan for this workspace "
                      "must already satisfy",
            "kind": "briefing",
            "is_a_plan": False,
            "workspace": root,
            "topic": topic[:500],
            "constraints": delegate.constraints_for(
                root,
                self._delegate_roots(),
                deny_paths=self.gate.deny_paths,
                self_paths=self.gate.self_paths,
            ),
            "note": (
                "these are read from the gate that will apply, so they are not "
                "guidance to weigh - they are what a call will be checked "
                "against. A plan that violates one is not wrong because the "
                "assistant disagreed with it"
            ),
        }

    def _exec_delegate_status(self, args: dict) -> Any:
        raw = self._delegate_task_id(args)
        if raw:
            task = self._delegate_task(args)
            limit = _delegate_int(args.get("output_chars"), 4000)
            return {"task": task.result(output_chars=limit)}
        # `Registry.rows` already returns the public dict form; calling `.row()`
        # on it again was the first version's bug, and it raised
        # AttributeError on every call - which is the same shape as the D57
        # wake-word failure, where a tool that cannot do its one job reports
        # itself broken rather than crashing loudly.
        rows = list(self.tasks.rows())
        return {
            "tasks": rows,
            "count": len(rows),
            "capacity": self.tasks.capacity(),
            "running": sum(1 for r in rows if r["status"] == "running"),
            "note": (
                "output is not included here; ask for one task by id to read "
                "what it printed. A listing carrying 4000 characters of stdout "
                "per task is a wall, and a wall is not a status"
            ),
        }

    def _exec_delegate_review(self, args: dict) -> Any:
        """The evidence a judgement would be made from, and no judgement.

        Deliberately free. A review that called the model would be the most
        expensive read in the system - VRAM on a call the user expects to cost
        nothing - and its output would be an opinion about a transcript with no
        more grounding than the exit code the caller could already read. What it
        does add is the *comparison*: the goal as it was written, next to what
        the task actually did, so the reader can see the gap themselves.
        """
        task = self._delegate_task(args)
        out = {
            "action": "evidence for reviewing a task, not a verdict",
            "kind": "evidence",
            "task": task.row(),
            "goal_as_written": task.goal,
            "command_line": subprocess.list2cmdline(task.argv),
            "workspace": task.root,
            "still_approved": self.gate.path_refusal(task.root) is None,
            "note": (
                "no model was consulted and none was needed to produce this. "
                "Judging whether the output does what the goal asked is the "
                "caller's job, or the brain's on a turn where a model is "
                "already loaded"
            ),
        }
        if task.status == "running":
            out["incomplete"] = True
            out["incomplete_note"] = (
                "this task has not finished; anything read now is partial"
            )
        else:
            out["exit_code"] = task.exit_code
            out["stderr"] = task.stderr[-2000:]
            out["stderr_chars"] = len(task.stderr)
            out["stdout_chars"] = len(task.stdout)
        return out

    def _exec_delegate_audit(self, args: dict) -> Any:
        """Check the record against the machine, mechanically.

        Three questions, none of which needs judgement: is every finished task
        in the audit log, did each run in a workspace that is still approved,
        and does each task's claimed exit code match its status. A gap in any
        of those is a real finding about this daemon, and it is reported as
        such rather than as a warning - which is the difference between a
        broken audit trail and an honest report about one.
        """
        # `Registry.rows` already returns the public dict form; calling `.row()`
        # on it again was the first version's bug, and it raised
        # AttributeError on every call - which is the same shape as the D57
        # wake-word failure, where a tool that cannot do its one job reports
        # itself broken rather than crashing loudly.
        rows = list(self.tasks.rows())
        if args.get("task") or args.get("task_id") or args.get("id"):
            rows = [self._delegate_task(args).row()]
        # `AuditLog.read()` yields `AuditRecord` objects, not dicts. The first
        # version filtered on `isinstance(r, dict)` and so read an audit log as
        # though it were empty - which made every finished task report "no
        # audit row" on a daemon that had written one for each of them. A check
        # that reads nothing cannot report anything true, and it reported the
        # opposite of the truth with the confidence of a measurement.
        recorded = {
            str(getattr(r, "args", {}).get("task"))
            for r in self.audit.read()
            if getattr(r, "tool", "").startswith("delegate.")
            and getattr(r, "args", {}).get("task")
        }
        checks: list[dict] = []
        for row in rows:
            problems: list[str] = []
            root = str(row.get("workspace") or "")
            if root and self.gate.path_refusal(root):
                problems.append(
                    "the workspace it ran in is no longer approved"
                )
            if row.get("status") != "running" and row.get("id") not in recorded:
                problems.append("no audit row for a task that has finished")
            exit_code = row.get("exit_code")
            if row.get("status") == "done" and exit_code not in (0, None):
                problems.append(
                    f"status says done but the exit code was {exit_code}"
                )
            if row.get("status") == "failed" and exit_code == 0:
                problems.append(
                    "status says failed but the exit code was zero"
                )
            checks.append({
                "task": row.get("id"),
                "ok": not problems,
                "problems": problems,
            })
        return {
            "checks": checks,
            "checked": len(checks),
            # Counting the tasks with an *empty* problem list and calling the
            # total "problems" was the first version, and it reported a clean
            # audit as one problem per healthy task - the check list said
            # `ok: true, problems: []` in the same breath as `problems: 1`, so
            # the tool contradicted itself inside one response. It survived
            # every test that only asked whether the number was true, because
            # both readings of a count look like a count: the D55 shape one
            # level down. Summing the lengths answers the question that was
            # actually asked.
            "problems": sum(len(c["problems"]) for c in checks),
            "tasks_with_problems": sum(1 for c in checks if c["problems"]),
            "note": (
                "this compares the record with the machine; it does not judge "
                "whether the work was good. Every task this daemon has ever "
                "finished is expected to have an audit row - if one is missing, "
                "that is this daemon's bug and not a task's"
            ),
        }

    # -- the acting -------------------------------------------------------------

    def _preview_delegate_assign(self, args: dict) -> Any:
        """Never raises, and says plainly when the call would be refused.

        The reason this exists at all is that a confirmation dialog is the last
        place a bad call can still be caught. Showing the resolved workspace, the
        command as a real command line, and the budget gives the user three
        facts to check; the refusal list gives them the reason the call would
        not work, which is otherwise only discoverable by trying it.
        """
        refusals = self._delegate_refusals(args)
        try:
            cwd = self._delegate_workspace(args)
        except DaemonError as exc:
            cwd = f"unresolved - {exc}"
        try:
            argv = self._delegate_argv(args)
        except DaemonError:
            argv = [str(a) for a in (args.get("command") or [])]
        budget = args.get("budget_s")
        out = {
            "action": "assign a tracked task and return immediately",
            "task": args.get("task"),
            "goal": str(args.get("goal") or "")[:500],
            "workspace": cwd,
            "command": argv,
            "command_line": subprocess.list2cmdline(argv) if argv else "",
            "budget_s": float(budget) if isinstance(budget, (int, float)) \
                and not isinstance(budget, bool) else delegate.DEFAULT_BUDGET_S,
            "blocks_nothing": True,
            "note": (
                "the call returns as soon as the task is registered; the task "
                "runs on its own and is asked about later with "
                "delegate.status. A task killed on its budget reports as failed "
                "with the budget named - it is not a task that broke"
            ),
        }
        if refusals:
            out["would_be_refused"] = refusals
        return out

    def _exec_delegate_assign(self, args: dict) -> Any:
        refusals = self._delegate_refusals(args)
        if refusals:
            raise Refused(
                "delegate.assign refused - " + "; ".join(refusals)
            )
        root = self._delegate_workspace(args)
        argv = self._delegate_argv(args)
        budget = args.get("budget_s")
        budget_s = delegate.DEFAULT_BUDGET_S if budget is None else float(budget)
        task = delegate.Task(
            # The seventh copy of this rule, and the one that was still live:
            # `_delegate_refusals` had been taught the aliases, so `task_id`
            # cleared the gate and then raised KeyError one line later. The
            # gate passing is exactly what makes it worth checking what the
            # executor does with the same field - a refusal list that agrees
            # with itself is not evidence it agrees with the code it guards.
            id=self._delegate_task_id(args),
            goal=str(args["goal"]).strip(),
            root=root,
            argv=argv,
            budget_s=budget_s,
            created_at=time.time(),
        )
        evicted = self.tasks.start(task)
        self._audit(
            actor=str(args.get("actor", "user")),
            channel=self._channel(args),
            tool="delegate.assign",
            tier=str(Tier.CONSEQUENTIAL),
            decision="allow",
            reason=f"task {task.id} assigned",
            args={"task": task.id, "workspace": root, "command": argv,
                  "budget_s": budget_s},
        )
        return {
            "task": task.row(),
            "running": True,
            "evicted": evicted,
            "note": (
                "this task is now running and nothing is waiting on this call. "
                "Ask delegate.status with this id for the result"
            ),
        }

    def _preview_delegate_intervene(self, args: dict) -> Any:
        """Never raises. A task that is already gone is reported, not thrown.

        The refusal that matters here is the one about the assistant's own
        process: a delegated task is a *child* of this daemon, so `intervene`
        can name the daemon itself, and killing it would look exactly like a
        successful intervention until the daemon died. It is refused by PID
        rather than by name, for the same reason `app.close` is - the stray
        interpreters are the ones most often worth stopping, and a name
        blacklist would make them unreachable.
        """
        raw = self._delegate_task_id(args)
        action = str(args.get("action") or "stop").strip().lower()
        why = delegate.interact_refusal(action)
        task = self.tasks.get(str(raw)) if raw and not why else None
        out: dict[str, Any] = {
            "action": "stop or restart a running task",
            "task": raw,
            "requested_action": action,
            "note": (
                "a task that has already finished cannot be intervened with; "
                "use delegate.assign to run it again. Restarting re-runs the "
                "same command with the same budget - it does not resume"
            ),
        }
        if why:
            out["would_be_refused"] = [why]
            return out
        if task is None:
            held = ", ".join(self.tasks.ids()) or "none"
            out["would_be_refused"] = [
                f"no task called {str(raw)!r}. Tasks in hand: {held}"
            ]
            return out
        out.update({
            "status": task.status,
            "pid": task.pid,
            "workspace": task.root,
            "goal": task.goal,
            "command_line": subprocess.list2cmdline(task.argv),
            "budget_s": task.budget_s,
        })
        if task.pid in delegate.own_chain():
            out["would_be_refused"] = [
                "that task is this assistant, or something it was started "
                "from - not stopping it"
            ]
        if task.status != "running":
            out["would_be_refused"] = [
                f"the task has already finished (status {task.status!r}); "
                "there is nothing left to intervene with"
            ]
        return out

    def _exec_delegate_intervene(self, args: dict) -> Any:
        raw = self._delegate_task_id(args)
        action = str(args.get("action") or "stop").strip().lower()
        why = delegate.interact_refusal(action)
        if why:
            raise Refused(f"delegate.intervene refused - {why}")
        task = self._delegate_task(args)
        if action == "stop":
            stopped, reason = self.tasks.stop(task.id)
            if not stopped:
                raise Refused(f"delegate.intervene refused - {reason}")
            self._audit(
                actor=str(args.get("actor", "user")),
                channel=self._channel(args),
                tool="delegate.intervene",
                tier=str(Tier.CONSEQUENTIAL),
                decision="allow",
                reason=f"task {task.id} stop requested",
                args={"task": task.id, "action": "stop", "pid": task.pid},
            )
            return {
                "task": task.row(),
                "asked_to_stop": True,
                "note": (
                    "the process has been asked to stop, not confirmed dead. "
                    "delegate.status will report the real outcome once the "
                    "daemon has reaped it"
                ),
            }
        # restart: stop if alive, then reassign the identical command. The task
        # keeps its id so that `status` reads the same name throughout - a new
        # id would strand every reference the caller already holds.
        was_running = task.status == "running"
        if was_running:
            stopped, reason = self.tasks.stop(task.id)
            if not stopped:
                raise Refused(f"delegate.intervene refused - {reason}")
            self.tasks.wait(task.id, timeout=5.0)
        again = delegate.Task(
            id=task.id,
            goal=task.goal,
            root=task.root,
            argv=list(task.argv),
            budget_s=task.budget_s,
            created_at=time.time(),
        )
        evicted = self.tasks.start(again)
        self._audit(
            actor=str(args.get("actor", "user")),
            channel=self._channel(args),
            tool="delegate.intervene",
            tier=str(Tier.CONSEQUENTIAL),
            decision="allow",
            reason=f"task {task.id} restarted",
            args={"task": task.id, "action": "restart",
                  "was_running": was_running},
        )
        return {
            "task": again.row(),
            "restarted": True,
            "stopped_first": was_running,
            "evicted": evicted,
            "note": (
                "the same command, from the beginning, with the same budget. "
                "This does not resume the old run and it does not give it more "
                "time; a task that ran out of budget will do so again"
            ),
        }

    def _preview_delegate_accept(self, args: dict) -> Any:
        """Never raises. An unrecognised verdict is listed, not guessed.

        `accept` is the one delegation tool that records an opinion, so the
        preview is mostly about making sure the opinion has an owner and a
        subject: who is accepting, which task, and on what basis. A verdict with
        no stated reason is not reviewable later, and this is the one record in
        the system that a person will be asked to trust without being able to
        re-derive it.
        """
        raw = self._delegate_task_id(args)
        verdict = str(args.get("verdict") or args.get("decision")
                      or "accept").strip().lower()
        why = delegate.task_id_refusal(raw)
        out: dict[str, Any] = {
            "action": "record your judgement of a finished task",
            "task": raw,
            "verdict": verdict,
            "reason": str(args.get("reason") or "")[:500],
            "note": (
                "this records what you concluded, not what the task did. It "
                "cannot make a failed task pass; the exit code stays in "
                "delegate.review whatever is recorded here"
            ),
        }
        if why:
            out["would_be_refused"] = [why]
            return out
        if verdict not in ("accept", "reject"):
            out["would_be_refused"] = [
                f"{verdict!r} is not a verdict this tool understands; use "
                "'accept' or 'reject'"
            ]
            return out
        task = self.tasks.get(str(raw))
        if task is None:
            held = ", ".join(self.tasks.ids()) or "none"
            out["would_be_refused"] = [
                f"no task called {str(raw)!r}. Tasks in hand: {held}"
            ]
            return out
        out["status"] = task.status
        if task.status == "running":
            out["would_be_refused"] = [
                "the task is still running; there is nothing to accept yet"
            ]
        return out

    def _exec_delegate_accept(self, args: dict) -> Any:
        raw = self._delegate_task_id(args)
        verdict = str(args.get("verdict") or args.get("decision")
                      or "accept").strip().lower()
        why = delegate.task_id_refusal(raw)
        if why:
            raise Refused(f"delegate.accept refused - {why}")
        if verdict not in ("accept", "reject"):
            raise Refused(
                f"{verdict!r} is not a verdict this tool understands; use "
                "'accept' or 'reject'"
            )
        task = self._delegate_task(args)
        if task.status == "running":
            raise Refused(
                "delegate.accept refused - the task is still running; there is "
                "nothing to accept yet"
            )
        reason = str(args.get("reason") or "").strip()
        if not reason:
            raise Refused(
                "delegate.accept refused - a verdict with no stated reason "
                "cannot be reviewed later, and this is the one record in the "
                "system a person is asked to trust without re-deriving it"
            )
        task.accepted = verdict
        task.accept_reason = reason
        self._audit(
            actor=str(args.get("actor", "user")),
            channel=self._channel(args),
            tool="delegate.accept",
            tier=str(Tier.CONSEQUENTIAL),
            decision="allow",
            reason=f"task {task.id} {verdict}: {reason[:200]}",
            args={"task": task.id, "verdict": verdict, "reason": reason[:500]},
        )
        return {
            "task": task.row(),
            "verdict": verdict,
            "reason": reason[:500],
            "note": (
                "recorded against the task and in the audit log. This does not "
                "change the exit code or the status; it records that you looked "
                "and decided"
            ),
        }

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

    def _telegram_status(self, args: dict) -> Any:
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

    def _telegram_send(self, args: dict) -> Any:
        """Deliver one message, once the gate and D32 have had their say.

        Proactively sending a message is a T3 external action. The bridge's own
        replies are transport and do not come through here - answering the
        message you were just sent is not this tool. This is for a message the
        assistant starts: a reminder, an alert, a result nobody asked for. Which
        is exactly why it is T3.

        Private, and it was not for a while. While `telegram.send` was only an
        op this was `op_telegram_send`, and `op_*` names are ops - callable by
        name through the pipe with no tier check, no preview and no confirmation.
        Registering the tool therefore left a second door to a T3 action, and
        anything holding a pipe handle could walk through it while the registered
        tool sat behind a dialog. `tests/test_voice_ops.py` caught it, by asserting
        the general rule rather than the case: an executable tool must not also
        have an op. The helper keeps the allowlist check and the failure reporting
        in one place; it is no longer reachable by name.
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

    # -- the Telegram tools ---------------------------------------------------
    #
    # `telegram.send` had a helper before it had a tool. The tool is the
    # classified, previewed entry point a model reaches for; the helper is the raw
    # delivery underneath. They end in the same place on purpose: the allowlist
    # check and the "the send failed" reporting live in `_telegram_send` and
    # nowhere else, because two copies of "is this chat allowed" is how one of
    # them ends up not being. Five defects in one afternoon came from
    # reimplementing a rule the project already had (D58).
    #
    # The helper is deliberately *not* an `op_*`. Ops are tier-free and reachable
    # by name, so an op for a T3 tool is a way around the tool - see the note on
    # `_telegram_send`.

    _TG_PREVIEW_CHARS = 2000  # enough to read a message; truncation is always flagged

    def _telegram_allowed_ids(self) -> list[int]:
        if self.telegram is None:
            return []
        return list(self.telegram.config.allowed_chat_ids)

    def _telegram_recipient(self, args: dict) -> tuple[int | None, str]:
        """Who the message goes to, and why not if it cannot be decided.

        Never guesses. With exactly one chat on the allowlist the answer is
        unambiguous, so `chat_id` is optional and the ordinary case - one phone,
        one person - needs no ceremony. With two or more, choosing one of the
        user's own chats is not a decision a T3 tool should make on his behalf, so
        it asks instead and says which ids would work.

        The second return value is the refusal text. Returning it rather than
        raising is deliberate: the preview needs to *describe* the ambiguity to a
        dialog rather than blow up while building one, and `_preview_for` has no
        exception handling, so a preview that raised would take the whole
        confirmation down with it.
        """
        raw = args.get("chat_id", args.get("to"))
        ids = self._telegram_allowed_ids()
        if raw is not None and str(raw).strip() != "":
            try:
                chat_id = int(raw)
            except (TypeError, ValueError):
                return None, f"{raw!r} is not a chat id; it must be a whole number"
            if not self.telegram.config.allows(chat_id):
                return None, (
                    f"chat {chat_id} is not on the allowlist, so this will be "
                    f"refused. Allowed: {', '.join(str(i) for i in ids)}"
                )
            return chat_id, ""
        if not ids:
            return None, "the bridge has no allowlist, so there is nobody to send to"
        if len(ids) == 1:
            return ids[0], ""
        return None, (
            f"several chats are allowed ({', '.join(str(i) for i in ids)}), so "
            "this needs an explicit chat_id - picking one of them is the user's "
            "decision, not the assistant's"
        )

    def _preview_telegram_send(self, args: dict) -> dict:
        """D32 for words. The preview *is* the message.

        Everything else about this tool is knowable from its name: a send goes to
        Telegram. The one thing that cannot be known without reading is the text,
        and the text is the entire risk - a phone number, a password, a confident
        wrong answer the user did not check. So unlike `voice.speak`, which
        truncates because it is echoing the user's own words straight back, this
        shows the payload in full up to a length that no real message reaches, and
        when it does cut, it says so.

        It must not raise. `_preview_for` has no exception handling and this runs
        while a confirmation is being built, so a bridge that is not configured
        produces a preview that explains the send would fail rather than an
        exception that loses the dialog.
        """
        text = str(args.get("text", "")).strip()
        chat_id, why = self._telegram_recipient(args)
        ids = self._telegram_allowed_ids()
        shown = text[: self._TG_PREVIEW_CHARS]
        over = len(text) > TELEGRAM_TEXT_MAX
        out = {
            "action": "send this message to Telegram, unprompted, on your own initiative",
            "to": chat_id if chat_id is not None else "(nobody chosen)",
            "allowed_chat_ids": ids,
            "message": shown,
            "characters": len(text),
            "preview_truncated": len(text) > len(shown),
            "reversible": False,
            "note": (
                "once this is delivered it cannot be recalled or edited from here"
            ),
        }
        if len(text) > len(shown):
            out["note"] += (
                f"; the preview is cut at {self._TG_PREVIEW_CHARS} characters, so "
                "read the staged draft for the rest"
            )
        if over:
            out["would_be_refused"] = (
                f"{len(text)} characters is over Telegram's {TELEGRAM_TEXT_MAX}; "
                "this call will be refused rather than delivered cut off"
            )
        if why:
            out["would_be_refused"] = why
        if self.telegram is None:
            out["would_be_refused"] = (
                "the Telegram bridge is not configured, so the send would fail: "
                f"{self.telegram_error}"
            )
        return out

    def _exec_telegram_send(self, args: dict) -> Any:
        """Deliver one message. T3 and external, so the gate and D32 come first."""
        if self.telegram is None:
            raise DaemonError(
                "the Telegram bridge is not configured: "
                f"{self.telegram_error}"
            )
        text = str(args.get("text", "")).strip()
        if not text:
            raise DaemonError("telegram.send needs non-empty text")
        if len(text) > TELEGRAM_TEXT_MAX:
            # `say` would cut it, silently, and the user would receive half a
            # sentence and no sign that half was missing. A refused call is one
            # the model can see and act on; a silently truncated one it cannot.
            raise DaemonError(
                f"the message is {len(text)} characters and Telegram's limit is "
                f"{TELEGRAM_TEXT_MAX}; it will not be sent cut off. Shorten it, or "
                "stage it with telegram.send_draft and send the draft yourself."
            )
        chat_id, why = self._telegram_recipient(args)
        if chat_id is None:
            raise DaemonError(why)
        # The allowlist, the empty check and the transport-failure reporting all
        # live in the helper. Calling it is the point: one rule, one place.
        return self._telegram_send({**args, "chat_id": chat_id})

    def _exec_telegram_poll(self, args: dict) -> Any:
        """What Telegram has sent, as words rather than as a counter.

        A T0 read of what the bridge already has. It deliberately does not poll:
        `_poll_once` advances the cursor, and a read that consumed updates would
        delete messages the loop had not handled yet - see `TelegramBridge.inbox`
        for why that is data loss wearing a read tool's clothes.
        """
        status = self._telegram_status({})
        if not status.get("configured"):
            return {**status, "messages": [], "showing": 0}
        try:
            limit = int(args.get("limit") or 20)
        except (TypeError, ValueError):
            limit = 20
        rows = self.telegram.inbox(limit)
        note = ""
        if not status.get("running"):
            note = (
                "the polling loop is not running, so this will not change until it "
                "is started again"
            )
        elif not rows:
            note = "nothing has arrived on an allowed chat yet"
        return {**status, "messages": rows, "showing": len(rows), "note": note}

    def _exec_telegram_send_draft(self, args: dict) -> Any:
        """Stage a message for a human to read. Never sends anything.

        T1 because the only thing it does is append a line to a file inside the
        daemon's own state directory. No `path` argument, deliberately: a T1 write
        tool that takes a path from the caller is an arbitrary-write primitive
        wearing a messaging name, and the whole value of this tool is that it can
        only ever write to the one place.

        The draft lands where a human can open it without asking the assistant for
        anything - the gate would refuse to read `state/`, and that is the right
        answer. Staging is for a person, not for the assistant's own read tools.
        """
        text = str(args.get("text", "")).strip()
        if not text:
            raise DaemonError("telegram.send_draft needs non-empty text")
        chat_id, why = self._telegram_recipient(args)
        path = self.state / "telegram_drafts.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "at": time.time(),
            "chat_id": chat_id,
            "characters": len(text),
            "text": text,
            "sent": False,
        }
        if chat_id is None:
            # Recorded rather than refused: staging a message whose recipient is
            # still undecided is exactly the state this is for.
            row["undecided"] = why
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        staged = sum(1 for _ in path.open("r", encoding="utf-8"))
        return {
            "staged": True,
            "sent": False,
            "path": str(path),
            "chat_id": chat_id,
            "characters": len(text),
            "staged_total": staged,
            "next": (
                "open the file and read it; nothing has been sent. Deliver it "
                "yourself, or ask the assistant to telegram.send the same words."
            ),
        }

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
        "file.write_notes": Daemon._exec_file_write_notes,
        "file.empty_recycle_bin": Daemon._exec_file_empty_recycle_bin,
        "voice.speak": Daemon._exec_voice_speak,
        "voice.listen": Daemon._exec_voice_listen,
        "voice.status": Daemon._exec_voice_status,
        "app.launch": Daemon._exec_app_launch,
        "app.close": Daemon._exec_app_close,
        "shell.run": Daemon._exec_shell_run,
        "registry.write": Daemon._exec_registry_write,
        "service.start_stop": Daemon._exec_service_start_stop,
        "package.install": Daemon._exec_package_install,
        "package.uninstall": Daemon._exec_package_uninstall,
        "power.shutdown": Daemon._exec_power_shutdown,
        "telegram.poll": Daemon._exec_telegram_poll,
        "telegram.send_draft": Daemon._exec_telegram_send_draft,
        "telegram.send": Daemon._exec_telegram_send,
        "delegate.plan": Daemon._exec_delegate_plan,
        "delegate.brainstorm": Daemon._exec_delegate_brainstorm,
        "delegate.assign": Daemon._exec_delegate_assign,
        "delegate.status": Daemon._exec_delegate_status,
        "delegate.intervene": Daemon._exec_delegate_intervene,
        "delegate.review": Daemon._exec_delegate_review,
        "delegate.accept": Daemon._exec_delegate_accept,
    # Teaching: five turns that stream prose from the brain, and one read.
    # The previews are here for the same reason the T2 administration tools
    # have them - what a payload-bound confirmation is bound to is the thing
    # that will be asked and the file it will be recorded in, and a dialog
    # showing neither is a dialog the user approves blind (D32).
    "teach.explain": Daemon._exec_teach_explain,
    "teach.quiz": Daemon._exec_teach_quiz,
    "teach.recall": Daemon._exec_teach_recall,
    "teach.summarise": Daemon._exec_teach_summarise,
    "teach.plan_curriculum": Daemon._exec_teach_plan_curriculum,
    "teach.progress": Daemon._exec_teach_progress,
        "delegate.audit": Daemon._exec_delegate_audit,
    }
    Daemon._PREVIEWS = {
        # Only destructive tools need a real preview, because only destructive
        # tools are the ones a human is asked to bless. A preview for a T0 read
        # would be theatre.
        "file.delete": Daemon._preview_file_delete,
        # The one T3 with no inverse. Its preview cannot show an undo, so what
        # it shows instead is the evidence Windows itself produced - and the
        # difference between a bin we could not read and a bin that is empty.
        "file.empty_recycle_bin": Daemon._preview_file_empty_recycle_bin,
        # Audible tools get previews for the same reason destructive ones do:
        # a T3 dialog with no payload is a dialog the user approves blind.
        "voice.speak": Daemon._preview_voice_speak,
        "voice.listen": Daemon._preview_voice_listen,
        # The Tier 2 administration tools, which are the opposite case from the
        # comment above and are worth spelling out. `file.delete` is T3; these
        # are T2. The reason a T2 tool still needs a preview is not risk but
        # ambiguity: the confirmation's job is to let the user check that the
        # thing being blessed is the thing they meant, and "run this command" /
        # "close this" cannot be checked by reading the words. The preview shows
        # the resolved executable, the argv as a real command line, and the
        # exact PIDs - so approval is given against facts rather than intent.
        "app.launch": Daemon._preview_app_launch,
        "app.close": Daemon._preview_app_close,
        "shell.run": Daemon._preview_shell_run,
        # Tier 3 needs a preview whether or not it is convenient. Each of these
        # shows the exact argv it will run, and the undo, because the payload is
        # the only thing standing between the user and the consequence.
        "registry.write": Daemon._preview_registry_write,
        "service.start_stop": Daemon._preview_service_start_stop,
        "package.install": Daemon._preview_package_install,
        "package.uninstall": Daemon._preview_package_uninstall,
        "power.shutdown": Daemon._preview_power_shutdown,
        # External gets a preview for the same reason audible does, and here the
        # reason is sharper: for a send, the payload *is* the risk. "Approve
        # telegram.send?" is a dialog with nothing to approve on, and the one
        # thing the user must be able to check is the words.
        "telegram.send": Daemon._preview_telegram_send,
        # The delegation Tier 2 tools, for the ambiguity reason above and one
        # more that only delegation has. `delegate.assign` starts something that
        # keeps running after the call returns, so the dialog has to say what
        # will still be running in a minute - command, workspace, budget - and a
        # `pause` action is not offered because Windows cannot honour one. The
        # refusal list is the other half: an assign that would be refused shows
        # why, so the reason is learned from the dialog rather than by trying.
        "delegate.assign": Daemon._preview_delegate_assign,
        "delegate.intervene": Daemon._preview_delegate_intervene,
        # `delegate.accept` records an opinion rather than acting on the machine,
        # but it is still T2 and still deserves a payload to be checked against:
        # a verdict with no named subject and no stated reason is the one record
        # in this system that cannot be re-derived later.
        "delegate.accept": Daemon._preview_delegate_accept,
    "teach.explain": Daemon._preview_teach_explain,
    "teach.quiz": Daemon._preview_teach_quiz,
    "teach.recall": Daemon._preview_teach_recall,
    "teach.summarise": Daemon._preview_teach_summarise,
    "teach.plan_curriculum": Daemon._preview_teach_plan_curriculum,
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
