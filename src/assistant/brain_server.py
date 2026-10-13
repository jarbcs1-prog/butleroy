"""
Plane B as an actual OS process: a child that serves turns over a named pipe.

Why this is a separate process at all
--------------------------------------
D3. On Windows, GPU memory is reliably returned only when the process holding it
dies. There is no "unload" that gives the memory back, so a resident brain would
mean a permanently occupied card. A child process makes the idle TTL enforceable
by construction: the parent's sweeper kills the process, and the VRAM goes with
it. Nothing has to be trusted about graceful shutdown.

Why the child must ask the daemon for its lease
-----------------------------------------------
The VRAM arbiter lives in Plane A, where the audit log lives. If the child could
take a lease for itself, then the one component holding the most precious resource
would also be the one exempt from the record. So the child is handed a *callable*
that does a pipe round trip to `arbiter.acquire` in the daemon. The child cannot
widen its own authority: it can only ask, and the ask is audited.

This is also why `lease` is a callable parameter rather than an object. The
in-process tests pass a lambda; the real process passes a pipe client. Nothing else
about the brain changes between the two.
"""

from __future__ import annotations

import json
import ctypes
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterator

from .tools import ALL_TOOLS

from . import ipc
from .arbiter import as_denial, format_denial
from .policy import CHANNEL_SCHEDULED

ROOT = Path(__file__).resolve().parent.parent.parent

FAST_MODEL = (
    ROOT / "models" / "Qwen3.5-4B-Uncensored-HauhauCS-Aggressive-Q8_0.gguf"
)
DEEP_MODEL = (
    ROOT / "models"
    / "Qwen3.5-9B-The-Defiant-Fable-Uncnr-Heretic-NEO-MAX-IQ4_XS.gguf"
)
SPARE_MODEL = (
    ROOT / "models" / "Qwen3.5-0.8B-Q4_0.gguf"
)

DEFAULT_MODEL = FAST_MODEL
DEFAULT_CUDA_BIN = r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v13.3\bin"

#: The two tiers, and what each one asks the arbiter for.
#:
#: `need_bytes` is a request for a *lease*, not a measurement, so it is the whole
#: ceiling the tier is allowed to occupy (D1) rather than the file size. The gap
#: is real and was measured: on the 4B a 4.17 GiB file occupies 4.92 GiB, the
#: extra 0.75 GiB being the CUDA context and the KV cache. A lease sized to the
#: file would under-request by most of a gigabyte and let us over the ceiling
#: while every piece of bookkeeping said we were under it.
#:
#: Both tiers are abliterated and uncensored, which is the user's ordering
#: (abliterated -> heretic -> uncensored) and not a coincidence: a model that
#: hesitates is worse than useless when it is being asked to copy a file.
#:
#: They cannot both be resident. 6.7 + 4.9 is more than the 12 GiB card holds, so
#: asking for the deep tier means giving the fast one back - see D22.
TIERS: dict[str, dict[str, Any]] = {
    "fast": {"model": FAST_MODEL, "need_bytes": 7 * 1024 ** 3},
    "deep": {"model": DEEP_MODEL, "need_bytes": 7 * 1024 ** 3},
    "spare": {"model": SPARE_MODEL, "need_bytes": 2 * 1024 ** 3},
}

# How long the child waits for the daemon to answer a lease request before giving
# up. A lease is a hard prerequisite for loading, so a silent daemon must fail
# fast rather than hang a turn forever.
LEASE_TIMEOUT_S = 20.0


def default_config(model: str | None = None, **overrides: Any):
    """Build the fast-tier config, with the PATH fix from D26 applied.

    D26 is the reason the CUDA toolkit directory is prepended here rather than
    left to the ambient environment: the child must not inherit whatever PATH the
    desktop session happens to have, because that breaks the first time the user
    installs a different CUDA version.
    """
    from .brain import BrainConfig

    cuda_bin = DEFAULT_CUDA_BIN
    if cuda_bin and os.path.isdir(cuda_bin) and cuda_bin not in os.environ.get("PATH", ""):
        os.environ["PATH"] = cuda_bin + os.pathsep + os.environ.get("PATH", "")
    cfg: dict[str, Any] = {
        "model_path": str(model or DEFAULT_MODEL),
        "n_ctx": 4096,
        "n_batch": 512,
        "n_gpu_layers": -1,
        "max_tokens": 512,
        "idle_ttl_s": 90.0,
        "cuda_bin": cuda_bin,
    }
    cfg.update(overrides)
    return BrainConfig(**cfg)


class LeaseClient:
    """The child's only route to GPU memory: ask Plane A.

    It mirrors the shape of an `arbiter.Denial` on failure so that `Brain.load`,
    which already knows how to recognise a denial, needs no special case. That
    reuse is deliberate: if this class ever returns something the brain does not
    recognise, the lease is treated as *granted*, which is the wrong way round.
    """

    def __init__(self, client: ipc.PipeClient, *, holder: str = "brain",
                 need_bytes: int, priority: str = "FAST_TIER",
                 timeout: float = LEASE_TIMEOUT_S):
        self._client = client
        # Public, because this child also needs to ask the daemon other
        # questions - currently "which workspaces am I allowed to act in?".
        # Keeping it private meant the roots reader raised AttributeError on
        # every call, silently answered "no approved roots", and the model was
        # left guessing paths with nothing in its prompt to correct it by.
        self.client = client
        self.holder = holder
        self.need_bytes = need_bytes
        self.priority = priority
        self.timeout = timeout

    def __call__(self) -> Any:
        # Note the underscore. Ops are method names with the `op_` prefix
        # stripped, so the arbiter op is `arbiter_acquire`, not `arbiter.acquire`.
        # Getting this wrong fails closed - the daemon refuses an op it does not
        # implement - which is the right direction to fail, but the message
        # ("unknown op") points at the transport rather than at the typo.
        res = self._client.call(
            "arbiter_acquire",
            holder=self.holder,
            need_bytes=self.need_bytes,
            priority=self.priority,
        )
        if res.get("ok"):
            return res.get("result")
        return _SyntheticDenial(self.need_bytes, str(res.get("error", "lease failed")))


class _SyntheticDenial:
    """A local, duck-typed stand-in for `arbiter.Denial`.

    Returning the real class would import the arbiter's whole vocabulary into a
    process that should not care about leases beyond whether it got one. What
    matters is that `denied is True`, which is what `Brain.load` tests.
    """

    denied = True

    def __init__(self, need_bytes: int, reason: str):
        self.holder = "brain"
        self.need_bytes = need_bytes
        self.reason = reason
        self.free_bytes = 0
        self.reserve_bytes = 0
        self.preempted: list[str] = []

    def __str__(self) -> str:  # pragma: no cover - formatting only
        return f"DENIED brain: {self.reason}"


def _handler_factory(brain_box: dict[str, Any], lease: LeaseClient):
    """Build the request handler the pipe server will call."""

    def handle(request: ipc.Request) -> dict:
        op = request.op
        args = dict(request.args or {})

        if op == "ping":
            return {"pong": True, "pid": os.getpid()}

        if op == "shutdown":
            brain_box["stop"] = True
            return {"stopping": True}

        if op == "describe":
            brain = brain_box.get("brain")
            return brain.describe() if brain else {"loaded": False}

        if op == "status":
            brain = brain_box.get("brain")
            return brain.status() if brain else {"loaded": False}

        if op == "unload":
            brain = brain_box.get("brain")
            if brain is None:
                return {"unloaded": False, "reason": "not loaded"}
            brain.unload()
            brain_box.pop("brain", None)
            return {"unloaded": True}

        if op == "load":
            return _load(brain_box, lease)

        if op == "turn":
            return _turn(brain_box, lease, args)

        if op == "stream":
            return _stream(brain_box, lease, args)

        return {"error": f"unknown brain op {op!r}"}

    return handle


def _roots_reader(lease: LeaseClient) -> Callable[[], tuple[str, ...]]:
    """Ask the daemon which roots are approved, every turn.

    From the child, not from a copy passed in at spawn: the daemon is the only
    thing that knows the current set, and a child given a list at startup would
    keep advertising a workspace the user has since revoked.

    A failure answers with no roots rather than with a stale list. That means the
    prompt says nothing is approved, and every call is refused - which is the
    right way round: too little information produces refusals, too much produces
    access.
    """
    state: dict[str, Any] = {"cache": None, "at": 0.0}
    TTL = 5.0

    def read() -> tuple[str, ...]:
        now = time.monotonic()
        if state["cache"] is not None and now - state["at"] < TTL:
            return state["cache"]
        try:
            # The daemon's envelope, unwrapped by hand. `PipeClient.call`
            # returns whatever the handler returned, and the handler is
            # `Daemon.handle`, which answers `{"ok": ..., "result": ...}` - so
            # unlike a failing call this one does not raise, it comes back
            # wrapped. Assuming otherwise silently yielded no roots, and the
            # model was left inventing paths with nothing in its prompt to
            # correct it by.
            envelope = lease.client.call("workspace_roots") or {}
            payload = envelope.get("result", envelope)
            roots = tuple(sorted(str(r) for r in (payload.get("roots") or [])
                                 if r))
        except Exception:
            roots = ()
        state["cache"] = roots
        state["at"] = now
        return roots

    return read


def _overlay_reader(lease: LeaseClient):
    """The reflection overlay, read from the daemon every turn (D21).

    Asked for over the pipe rather than read from disk in this process, because the
    overlay is written by Plane A into *its* state directory and this is a leased
    child: it must not go looking for files it was not given. The same rule as the
    roots reader, for the same reason.

    A turn with no overlay is the normal case, and the reader says so by returning
    "" rather than raising — the assistant answering without any accumulated habits
    is the correct state, not an error.
    """
    def read() -> str:
        try:
            envelope = lease.client.call("reflect_overlay") or {}
            payload = envelope.get("result", envelope)
            return str(payload.get("overlay") or "")
        except Exception:
            return ""

    return read


def _load(brain_box: dict[str, Any], lease: LeaseClient) -> dict:
    existing = brain_box.get("brain")
    if existing is not None:
        return dict(existing.status(), already_loaded=True)

    from .brain import Brain, BrainError, default_system_prompt

    # The tier's model, not the default one.
    #
    # This was `default_config()` with no argument, which silently ignored the tier:
    # `serve` resolved `deep` to the 9B path, printed it, and then handed the load
    # a brain configured with `DEFAULT_MODEL`. The child announced
    # `tier=deep model=Qwen3.5-9B...` while holding the 4B, and the tier
    # comparison measured the same model twice - identical arguments, identical
    # 27s, identical 5081 MiB. A print statement that names the wrong thing is
    # worse than none: it is the one line anyone would trust.
    cfg = default_config(model=brain_box.get("model"))
    if not cfg.exists():
        return {"loaded": False, "error": f"model not found: {cfg.model_path}"}
    try:
        # The system prompt is not optional. Measured on this machine with this
        # model: the same two-word decision grammar returns `answer` with an
        # empty prompt and `tool` in 0.15s once the model has been told it has
        # tools. Without it the assistant is asked to choose between "tool" and
        # "answer" while never having been introduced to a tool, and then
        # narrates the call instead of making it -- D34's failure mode, reached
        # by a different road.
        #
        # It also has to name the approved roots. Measured without that: asked to
        # read a file it produced a *relative* path, and asked to archive a folder
        # it produced `C:/Users/YourUsername/Documents` -- a placeholder from
        # training data. The gate refused both, which is correct, but a refusal is
        # not assistance.
        roots = _roots_reader(lease)
        overlay = _overlay_reader(lease)
        brain = Brain(cfg, lease=lease,
                      system_prompt=default_system_prompt(ALL_TOOLS, roots(),
                                                          overlay()),
                      roots=roots, overlay=overlay)
        status = brain.load()
    except BrainError as exc:
        return {"loaded": False, "error": f"{type(exc).__name__}: {exc}"}
    brain_box["brain"] = brain
    # The loaded path comes from the config, not from the tier that was asked for.
    # If those ever disagree again, the discrepancy is visible in the first thing
    # anyone looks at after a load rather than in a VRAM measurement three turns
    # later.
    return dict(status, already_loaded=False, tier=brain_box.get("tier"),
                model=cfg.model_path)


def _turn(brain_box: dict[str, Any], lease: LeaseClient, args: dict) -> dict:
    loaded = _load(brain_box, lease)
    if not loaded.get("loaded"):
        return loaded
    brain = brain_box["brain"]
    prompt = str(args.get("prompt", "")).strip()
    if not prompt:
        return {"loaded": True, "error": "empty prompt"}
    try:
        action = brain.turn(prompt)
    except Exception as exc:  # noqa: BLE001 - never let a turn kill the child
        return {"loaded": True, "error": f"{type(exc).__name__}: {exc}"}
    return dict(action.to_dict(), loaded=True)


def _stream(brain_box: dict[str, Any], lease: LeaseClient, args: dict) -> dict:
    """Collect a streamed answer and return the pieces.

    Streaming is served as a single accumulated result for now. The chunks are
    preserved in `pieces` and the total is recorded, because the overlay needs
    `render_delta` to be fed *real* deltas and the honest way to get them over a
    request/response pipe is to define the framing first rather than bolt a second
    channel on afterwards. What must not happen is a fake stream: D36's lesson is
    that a mechanism which silently does nothing is worse than one that is absent.
    """
    loaded = _load(brain_box, lease)
    if not loaded.get("loaded"):
        return loaded
    brain = brain_box["brain"]
    prompt = str(args.get("prompt", "")).strip()
    if not prompt:
        return {"loaded": True, "error": "empty prompt"}
    try:
        pieces: list[str] = []
        for chunk in brain.stream(prompt):
            pieces.append(chunk)
    except Exception as exc:  # noqa: BLE001
        return {"loaded": True, "error": f"{type(exc).__name__}: {exc}"}
    return {"loaded": True, "pieces": pieces, "text": "".join(pieces)}


def parent_alive(pid: int | None = None) -> bool:
    """Whether the process that started us is still running.

    D3 says VRAM comes back when this process dies, and that the idle TTL is how
    it dies. But the sweeper that enforces the TTL runs *in the daemon* - so a
    daemon that dies takes the sweeper with it, and the child survives holding
    ~5 GiB with nothing left to kill it. Observed twice: a crashed probe left
    two orphans holding the pipe, and the card stayed busy until they were found
    by hand.

    So the child checks for itself.

    `os.getppid()` is the obvious answer and it does not work on Windows. Windows
    does not reparent orphans the way Unix does - the child simply keeps the dead
    parent's PID, forever - so that value looks fine long after the parent is
    gone. The reliable test is to hold a *handle* to the parent and ask the kernel
    whether it has exited, which is what this does.

    A failure to open the handle is treated as death, not as life. Every way of
    getting here other than "the parent is running" means the child has lost the
    thing that owns it, and the one failure that matters is the child surviving.
    """
    if pid is None:
        pid = os.getppid()
    if pid <= 0:
        return False
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


class _BasicLimit(ctypes.Structure):
    """`JOBOBJECT_BASIC_LIMIT_INFORMATION`, field by field.

    Declared in full rather than written as bytes at an offset, because the byte
    version is exactly the mistake this replaced. `LimitFlags` sits at 0x10; a
    previous version of this file wrote at 0x28, which is `ActiveProcessLimit` -
    so `SetInformationJobObject` returned success, kill-on-close was never armed,
    and the guard looked installed while doing nothing at all.

    A structure that cannot be read back is a structure that cannot be claimed, so
    `job_limit_flags` exists to read this one back and the tests do exactly that.
    """
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _ExtendedLimit(ctypes.Structure):
    """`JOBOBJECT_EXTENDED_LIMIT_INFORMATION`."""
    _fields_ = [
        ("BasicLimitInformation", _BasicLimit),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

#: Win32 functions are declared once, with their signatures stated.
#:
#: Without this, `CreateJobObjectW` returns a C `int` by default, and on 64-bit
#: Windows a HANDLE does not fit in 32 bits. The result is a truncated handle and
#: the reassuring `ERROR_INVALID_HANDLE` from the very next call - which reads as
#: "the API refused" rather than "the caller truncated it". Stating `c_void_p`
#: here is the difference between asking a question and getting a wrong answer.
_KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
_KERNEL32.CreateJobObjectW.restype = ctypes.c_void_p
_KERNEL32.CreateJobObjectW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p)
_KERNEL32.SetInformationJobObject.restype = ctypes.c_int
_KERNEL32.SetInformationJobObject.argtypes = (
    ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32,
)
_KERNEL32.QueryInformationJobObject.restype = ctypes.c_int
_KERNEL32.QueryInformationJobObject.argtypes = (
    ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32,
    ctypes.c_void_p,
)
_KERNEL32.AssignProcessToJobObject.restype = ctypes.c_int
_KERNEL32.AssignProcessToJobObject.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
_KERNEL32.OpenProcess.restype = ctypes.c_void_p
_KERNEL32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
_KERNEL32.CloseHandle.restype = ctypes.c_int
_KERNEL32.CloseHandle.argtypes = (ctypes.c_void_p,)


def job_kill_on_close() -> Any:
    """A Windows job that kills its members when the last handle closes.

    `parent_alive` is polling: it notices the parent is gone within two seconds.
    This is not polling. The kernel terminates the child the instant the last
    handle to the job is released, including when the owning process is killed
    without ever running a line of cleanup - a power loss, a `TerminateProcess`,
    a bluescreen. That is the case polling cannot cover, because polling requires
    a live process to do the polling.

    So this is the strong version and `parent_alive` is the fallback. The job is
    created here and assigned by the daemon, which holds the handle for as long as
    it wants the child alive; there is no other way to be killed.

    Raises `OSError` rather than returning something inert. A guard that quietly
    does nothing is worse than no guard, because it would be relied on.
    """
    job = _KERNEL32.CreateJobObjectW(None, None)
    if not job:
        raise OSError("CreateJobObjectW failed; the child could not be protected")

    info = _ExtendedLimit()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = _KERNEL32.SetInformationJobObject(
        job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(info), ctypes.sizeof(info),
    )
    if not ok:
        _KERNEL32.CloseHandle(job)
        raise OSError("SetInformationJobObject failed; kill-on-close not armed")
    return job


def job_limit_flags(job: Any) -> int:
    """Read `LimitFlags` back out of the job.

    Exists because `SetInformationJobObject` accepts a malformed structure
    without complaining. Without a way to ask what was actually stored, "armed"
    is a claim rather than an observation - and it was wrong once already.
    """
    info = _ExtendedLimit()
    ok = _KERNEL32.QueryInformationJobObject(
        job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(info), ctypes.sizeof(info), None,
    )
    if not ok:
        raise OSError("QueryInformationJobObject failed")
    return int(info.BasicLimitInformation.LimitFlags)


def job_assign(job: Any, pid: int) -> bool:
    """Put an existing process into the job. Returns whether it worked.

    Failing is reported, never raised: a child that is already running and *not*
    in the job is still protected by `parent_alive`, so a refusal here degrades to
    the weaker mechanism instead of taking the child's model away.
    """
    PROCESS_SET_QUOTA = 0x0100
    PROCESS_TERMINATE = 0x0001
    handle = _KERNEL32.OpenProcess(
        PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, int(pid)
    )
    if not handle:
        return False
    try:
        return bool(_KERNEL32.AssignProcessToJobObject(job, handle))
    finally:
        _KERNEL32.CloseHandle(handle)


def job_close(job: Any) -> None:
    """Release the handle. If members are still alive, this kills them.

    Named for what it does rather than for the Win32 call it makes, because the
    kill is the entire point: closing the job *is* the shutdown, and a reader who
    has to know that to use it correctly has not been told anything useful.
    """
    if job:
        _KERNEL32.CloseHandle(job)


def arm_kill_on_close(daemon: Any, pid: int) -> Any:
    """Put a just-started child into a job that dies with this daemon.

    A module function taking the daemon rather than a method on it, because the
    child must *not* import the daemon: that would be a circular import, and the
    child is a separate process whose whole purpose is to be disposable.

    Returns the handle, or None if the job could not be armed. None is a supported
    outcome and not an error: the child still watches for its parent, so the
    protection is thinner rather than absent. What is not supported is pretending
    it worked, so every failure is audited (D44).
    """
    try:
        job = job_kill_on_close()
    except OSError as why:
        daemon._audit(  # noqa: SLF001
            actor="daemon",
            channel=CHANNEL_SCHEDULED,
            tool="brain.job",
            decision="allow",
            reason=f"kill-on-close unavailable ({why}); the child will poll for "
                   f"its parent instead",
        )
        return None
    if not job_assign(job, pid):
        daemon._audit(  # noqa: SLF001
            actor="daemon",
            channel=CHANNEL_SCHEDULED,
            tool="brain.job",
            decision="allow",
            reason=f"could not put pid={pid} in the job; the child will poll for "
                   f"its parent instead",
        )
    return job


def resolve_tier(tier: str, model: str | None = None,
                 need_bytes: int | None = None) -> tuple[str, int]:
    """Turn a tier name into (model path, bytes to lease).

    Separated from `serve` so the decision can be asked without starting a child
    or touching the disk. A rule that can only be reached by booting a 5 GiB model
    is a rule nobody checks.

    An unknown tier raises rather than falling back to fast. Silently answering a
    "deep" request from the 4B would be a lie about which model spoke, and having
    two tiers is only worth anything if they are actually distinguishable.
    """
    spec = _tier_spec(tier)
    if spec is None:
        raise ValueError(
            f"unknown tier {tier!r}; known tiers are {sorted(TIERS)}"
        )
    return (
        model or str(spec["model"]),
        int(spec["need_bytes"] if need_bytes is None else need_bytes),
    )


def _tier_spec(tier: str) -> dict[str, Any] | None:
    """The tier's entry, from `state/models.json` if there is one.

    The child resolves the tier rather than being told which file to load (D22), and
    that is the right place for it: there is exactly one place where a model file is
    named, and now that a user can *choose* one it has to be a place that reads the
    choice rather than a literal in the middle of the program.

    `TIERS` stays as the built-in default and as the answer to "what are the known
    tier names" — a machine with no config must still have fast and deep, and the
    error message for an unknown tier has to be able to name them. What changed is
    that an operator-chosen `models.json` wins over the literals.

    Read here, in the child, rather than sent from the daemon: the daemon passes
    only a tier *name* over the wire, so a compromised or confused parent cannot
    hand the child an arbitrary path. The child's own file is the only thing that
    can move what it loads.
    """
    from . import modelconfig

    try:
        config = modelconfig.load()
    except Exception as exc:  # noqa: BLE001 - never block a turn on a config read
        print(f"[brain_server] models.json unusable ({type(exc).__name__}: {exc}); "
              f"using built-in tiers", file=sys.stderr)
        return TIERS.get(tier)

    resolved = modelconfig.to_brain_tiers(config)
    if tier in resolved:
        return resolved[tier]
    # A tier the config does not mention, or one that fell back. The literals are
    # the answer either way, and `TIERS` is what a caller can enumerate.
    return TIERS.get(tier)


def serve(pipe: str = ipc.PIPE_BRAIN_RESP, *, upstream: str = ipc.PIPE_BRAIN_REQ,
          authkey_path: str | os.PathLike | None = None,
          model: str | None = None, need_bytes: int | None = None,
          tier: str = "fast") -> int:
    """Run the child until told to stop. Returns a process exit code.

    Two pipes, and conflating them is a bug worth naming: ``pipe`` is the one the
    child *serves* on (the daemon connects in to ask for turns), while
    ``upstream`` is the one the child *calls out* on to ask for its VRAM lease.
    Passing the same name for both makes the child ask itself for permission,
    which fails in a way that looks like the arbiter refusing rather than the
    wiring being wrong.
    """
    # The authkey is passed in, not derived. Guessing at it relative to the repo
    # means a daemon with a custom state dir starts a child that cannot
    # authenticate to it, and the failure surfaces as an unexplained exit.
    authkey_path = Path(authkey_path or (ROOT / "state" / "authkey.bin"))
    authkey = ipc.load_authkey(authkey_path)
    to_daemon = ipc.PipeClient(upstream, authkey=authkey, timeout=LEASE_TIMEOUT_S)

    model, need_bytes = resolve_tier(tier, model, need_bytes)
    lease = LeaseClient(to_daemon, need_bytes=need_bytes)

    box: dict[str, Any] = {
        "stop": False,
        # Carried on the box rather than passed to _load so that the load cannot
        # reach for a model by any other route than the tier that named it.
        "model": model,
        "tier": tier,
    }
    server = ipc.PipeServer(pipe, _handler_factory(box, lease), authkey=authkey)

    print(f"[brain] pid={os.getpid()} serving on {pipe}", flush=True)
    print(f"[brain] leases from {upstream}", flush=True)
    print(f"[brain] tier={tier} model={Path(model).name}", flush=True)
    server.start()
    # Watched every two seconds rather than every 0.25: OpenProcess is cheap but
    # not free, and two seconds of a 90-second TTL is not a meaningful delay to
    # giving VRAM back.
    parent = os.getppid()
    last_check = time.monotonic()
    try:
        while not box.get("stop"):
            time.sleep(0.25)
            now = time.monotonic()
            if now - last_check >= 2.0:
                last_check = now
                if not parent_alive(parent):
                    # Not an error and not a crash: the daemon we belong to is
                    # gone, so the thing that would have reclaimed this process's
                    # VRAM no longer exists. Exiting is the whole point.
                    print("[brain] parent is gone; exiting so the VRAM returns",
                          flush=True)
                    box["stop"] = True
    except KeyboardInterrupt:
        pass
    finally:
        brain = box.get("brain")
        if brain is not None:
            try:
                brain.unload()
            except Exception:  # noqa: BLE001 - we are exiting regardless
                pass
        server.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    pipe = ipc.PIPE_BRAIN_RESP
    upstream = ipc.PIPE_BRAIN_REQ
    model = None
    authkey = None
    tier = "fast"
    for flag, slot in (("--pipe", "pipe"), ("--upstream", "upstream"),
                       ("--model", "model"), ("--authkey", "authkey"),
                       ("--tier", "tier")):
        if flag in argv:
            i = argv.index(flag)
            if i + 1 < len(argv):
                if slot == "pipe":
                    pipe = argv[i + 1]
                elif slot == "upstream":
                    upstream = argv[i + 1]
                elif slot == "model":
                    model = argv[i + 1]
                elif slot == "tier":
                    tier = argv[i + 1]
                else:
                    authkey = argv[i + 1]
    return serve(pipe=pipe, upstream=upstream, model=model,
                 authkey_path=authkey, tier=tier)


if __name__ == "__main__":
    sys.exit(main())
