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
import os
import sys
import time
from pathlib import Path
from typing import Any, Iterator

from . import ipc
from .arbiter import as_denial, format_denial

ROOT = Path(__file__).resolve().parent.parent.parent

DEFAULT_MODEL = (
    ROOT / "models" / "Qwen3.5-4B-Uncensored-HauhauCS-Aggressive-Q8_0.gguf"
)
DEFAULT_CUDA_BIN = r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v13.3\bin"

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


def _load(brain_box: dict[str, Any], lease: LeaseClient) -> dict:
    existing = brain_box.get("brain")
    if existing is not None:
        return dict(existing.status(), already_loaded=True)

    from .brain import Brain, BrainError, default_system_prompt

    cfg = default_config()
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
        brain = Brain(cfg, lease=lease, system_prompt=default_system_prompt())
        status = brain.load()
    except BrainError as exc:
        return {"loaded": False, "error": f"{type(exc).__name__}: {exc}"}
    brain_box["brain"] = brain
    return dict(status, already_loaded=False)


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


def serve(pipe: str = ipc.PIPE_BRAIN_RESP, *, upstream: str = ipc.PIPE_BRAIN_REQ,
          authkey_path: str | os.PathLike | None = None,
          model: str | None = None, need_bytes: int = 5 * 1024 ** 3) -> int:
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
    lease = LeaseClient(to_daemon, need_bytes=need_bytes)

    box: dict[str, Any] = {"stop": False}
    server = ipc.PipeServer(pipe, _handler_factory(box, lease), authkey=authkey)

    print(f"[brain] pid={os.getpid()} serving on {pipe}", flush=True)
    print(f"[brain] leases from {upstream}", flush=True)
    print(f"[brain] model={model or DEFAULT_MODEL.name}", flush=True)
    server.start()
    try:
        while not box.get("stop"):
            time.sleep(0.25)
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
    for flag, slot in (("--pipe", "pipe"), ("--upstream", "upstream"),
                       ("--model", "model"), ("--authkey", "authkey")):
        if flag in argv:
            i = argv.index(flag)
            if i + 1 < len(argv):
                if slot == "pipe":
                    pipe = argv[i + 1]
                elif slot == "upstream":
                    upstream = argv[i + 1]
                elif slot == "model":
                    model = argv[i + 1]
                else:
                    authkey = argv[i + 1]
    return serve(pipe=pipe, upstream=upstream, model=model, authkey_path=authkey)


if __name__ == "__main__":
    sys.exit(main())
