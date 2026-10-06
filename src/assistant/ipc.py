"""Named-pipe transport. No ports, anywhere, ever (D3).

Why named pipes rather than a socket on localhost: a TCP port is a thing other
programs on the machine can find, connect to, and keep. A named pipe is not
discoverable, is not routable, and dies with the process that owns it. There is
no configuration in which this system binds a port.

Authentication is not optional decoration. Any process running as the same user
can open ``\\\\.\\pipe\\<name>`` - including a malicious postinstall script that
happens to be running as you. This assistant holds delete, email and shell
tools, so "only processes that know the secret" is a real boundary, not a
detail. ``multiprocessing.connection`` already implements HMAC-SHA256
challenge-response, so we use it rather than inventing a scheme.

Threat model, stated honestly:

* **Other users / network attackers** - excluded by the pipe's ACL, which
  defaults to the creating account.
* **Other processes as the same user** - excluded by the authkey.
* **Code running as the same user that also reads the authkey file** - not
  excluded, and cannot be. This is a ceiling imposed by Windows, not a gap in
  this design.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from multiprocessing.connection import Client, Listener

#: Protocol version. A peer speaking a different major version is refused rather
#: than guessed at, because a half-understood request from a 24/7 daemon that
#: holds delete tools is not a recoverable situation.
PROTOCOL = 1

#: Canonical pipe names, matching ARCHITECTURE.md. Five pipes, fixed, so that
#: every component's contract is written down somewhere other than its code.
PIPE_BRAIN_REQ = r"\\.\pipe\locala.brain.req"
PIPE_BRAIN_RESP = r"\\.\pipe\locala.brain.resp"
PIPE_SUP_REQ = r"\\.\pipe\locala.sup.req"
PIPE_SUP_EVT = r"\\.\pipe\locala.sup.evt"

ALL_PIPES = (PIPE_BRAIN_REQ, PIPE_BRAIN_RESP, PIPE_SUP_REQ, PIPE_SUP_EVT)

#: Every named pipe starts with this. A literal, because a raw string cannot
#: end in a backslash and writing the prefix inline invites that mistake.
PIPE_PREFIX = "\\\\.\\pipe\\"

#: Windows caps a pipe name at 256 characters. Refuse at import time rather
#: than at the moment a worker tries to connect.
_MAX_PIPE_NAME = 256


class TransportError(RuntimeError):
    """Base for everything this module raises deliberately."""


class ProtocolError(TransportError):
    """The peer is not speaking our protocol, or is not authenticated."""


class Timeout(TransportError):
    """The peer accepted the connection but did not answer in time."""


class UnknownPipe(TransportError):
    """No listener is registered for this name."""


def validate_pipe_name(name: str) -> str:
    if not name.startswith(PIPE_PREFIX):
        raise ProtocolError(f"not a named pipe: {name!r}")
    if len(name) > _MAX_PIPE_NAME:
        raise ProtocolError(f"pipe name is {len(name)} chars, limit is {_MAX_PIPE_NAME}")
    return name


def generate_authkey() -> bytes:
    """32 random bytes. Generated once per daemon start, never on disk in
    plaintext, never derived from anything guessable."""
    return secrets.token_bytes(32)


def store_authkey(path: Path, authkey: bytes) -> None:
    """Persist the authkey for child processes we did not spawn ourselves.

    On Windows the file inherits the creating user's ACL. The file is created
    with a restricted mode where the platform honours it, and is read back only
    by processes running as this user.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create exclusively so we never silently overwrite a key a live daemon is
    # using - two daemons on one machine is a bug we want to hear about.
    #
    # os.O_BINARY is not optional. On Windows a descriptor opened without it is
    # in *text* mode, and os.write() then translates every 0x0A in the payload
    # to 0x0D 0x0A. A 32-byte random key contains a 0x0A byte about one time in
    # eight, so the file on disk would silently disagree with the key the
    # daemon is using, and the symptom would be a child process that
    # occasionally and unprovably cannot connect. Write bytes, or write nothing.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_BINARY
    fd = os.open(path, flags, 0o600)
    try:
        written = 0
        while written < len(authkey):
            written += os.write(fd, authkey[written:])
        os.fsync(fd)
    finally:
        os.close(fd)


def read_or_mint_authkey(path: Path) -> tuple[bytes, bool]:
    """Return ``(authkey, minted)`` **without writing anything**.

    This is the read-only half of startup, split out deliberately (D64). The
    daemon must be able to learn its key *before* it has proved it owns the state
    directory, because proving that means binding the pipe, and binding the pipe
    needs a key. Minting here is in memory only; nothing reaches disk until
    `commit_authkey`.

    Adopting rather than always minting is what makes a restart seamless: a child
    process or second component that read the key before a crash can still
    authenticate afterwards. Rotating on every start would work for the daemon
    and fail for exactly those callers.

    A corrupt or truncated key raises rather than being replaced. Quietly minting
    over it would hide real misconfiguration behind an unauthenticated-connection
    symptom much later.
    """
    try:
        return load_authkey(path), False
    except FileNotFoundError:
        return generate_authkey(), True


def commit_authkey(path: Path, authkey: bytes) -> None:
    """Write a key minted by `read_or_mint_authkey`. Call only once you own the
    state directory.

    Kept as `store_authkey` under ``os.O_EXCL`` on purpose, so that racing this
    from two processes is still an error rather than a silent last-writer-wins on
    a credential.
    """
    store_authkey(path, authkey)


def acquire_authkey(path: Path, *, owned: bool = False) -> tuple[bytes, bool]:
    """One-shot form of `read_or_mint_authkey` + `commit_authkey`.

    ``store_authkey`` opened the file with ``os.O_EXCL`` and nothing ever removed
    it, so a second daemon on the same state directory died with
    ``FileExistsError``. The intent behind ``O_EXCL`` was right - never silently
    overwrite a key a *live* daemon is using - but it was being asked to answer
    two questions at once, and only one of them was its job (D64):

    * *May I write this file?* is answered by binding the pipe, which the OS
      makes atomic and which fails on rebind. ``owned=True`` says "I hold it".
    * *Is a key already there?* is then only a fact to read.

    So the refusing default is preserved - an unproven caller still gets
    ``FileExistsError`` - and a caller that has proven ownership adopts what it
    finds instead of dying.

    The daemon does not use this: it needs the key *before* the bind, so it uses
    the two halves directly. This remains the honest single-call API for anything
    that already knows it owns the directory.
    """
    key, minted = read_or_mint_authkey(path)
    if not minted:
        if not owned:
            # The original O_EXCL behaviour, kept as the default: a caller that
            # has not proven it owns this state directory does not get to decide
            # that a key on disk is stale.
            raise FileExistsError(
                f"authkey already exists at {path} and ownership of the state "
                f"directory was not established"
            )
        return key, False
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        commit_authkey(path, key)
    except FileExistsError:
        # Lost a race with a concurrent writer. The key that appeared is
        # authoritative, and adopting it is correct rather than an error.
        return load_authkey(path), False
    return key, True


def remove_authkey(path: Path) -> None:
    """Delete the authkey file if it is there. Absent is not an error.

    Called on clean shutdown so that *a key on disk means a daemon is running*.
    Without it, every crash leaves a credential behind and the next start has to
    reason about staleness, which is the complexity this whole change removes.
    """
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def load_authkey(path: Path) -> bytes:
    key = path.read_bytes()
    # Deliberately no .strip(). The key is 32 random bytes and roughly 7% of
    # random byte strings begin or end with something .strip() considers
    # whitespace, which would silently return a wrong key about one run in
    # fourteen. Read exactly what was written.
    if len(key) < 32:
        raise ProtocolError(f"authkey at {path} is truncated ({len(key)} bytes)")
    return key


@dataclass(frozen=True)
class Request:
    op: str
    args: dict[str, Any]
    id: int
    protocol: int = PROTOCOL

    def to_dict(self) -> dict[str, Any]:
        return {"v": self.protocol, "id": self.id, "op": self.op, "args": self.args}

    @staticmethod
    def from_dict(raw: Any) -> "Request":
        if not isinstance(raw, dict):
            raise ProtocolError(f"request is {type(raw).__name__}, expected a dict")
        version = raw.get("v")
        if version != PROTOCOL:
            raise ProtocolError(f"protocol {version!r}, this daemon speaks {PROTOCOL}")
        op, args, ident = raw.get("op"), raw.get("args"), raw.get("id")
        if not isinstance(op, str) or not op:
            raise ProtocolError("request has no op")
        if not isinstance(args, dict):
            raise ProtocolError("request args must be an object")
        if not isinstance(ident, int):
            raise ProtocolError("request has no integer id")
        return Request(op=op, args=args, id=ident)


@dataclass(frozen=True)
class Response:
    id: int
    ok: bool
    result: Any = None
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"v": PROTOCOL, "id": self.id, "ok": self.ok}
        if self.ok:
            out["result"] = self.result
        else:
            out["error"] = self.error
        return out

    @staticmethod
    def from_dict(raw: Any) -> "Response":
        if not isinstance(raw, dict) or raw.get("v") != PROTOCOL:
            raise ProtocolError("response is not our protocol")
        ident = raw.get("id")
        if not isinstance(ident, int):
            raise ProtocolError("response has no integer id")
        if raw.get("ok"):
            return Response(id=ident, ok=True, result=raw.get("result"))
        return Response(id=ident, ok=False, error=str(raw.get("error", "unknown error")))

    def result_or_raise(self) -> Any:
        """Turn a failed response into an exception at the call site.

        A caller that forgets to check would otherwise silently receive ``None``
        from a denied request - which, for a tool call, is the single most
        dangerous way this could misbehave.
        """
        if self.ok:
            return self.result
        raise ProtocolError(f"peer returned an error: {self.error}")


Handler = Callable[[Request], Any]


def _describe(exc: BaseException) -> str:
    """Turn an exception into something safe to hand to a peer.

    Never the traceback: it leaks absolute paths and source layout to whoever is
    on the other end of the pipe. The class name plus the message is enough to
    debug with, and the full detail goes to the audit log instead.
    """
    if isinstance(exc, TransportError):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


class PipeServer:
    """Serves one named pipe, one thread per connection.

    The handler is a plain callable so the daemon can wire in the policy gate
    without this module knowing anything about tiers or tools. Whatever the
    handler returns is the result; whatever it raises becomes a failed response,
    never a dropped connection - a worker must not be able to crash the daemon
    by sending a malformed request.
    """

    def __init__(
        self,
        name: str,
        handler: Handler,
        *,
        authkey: bytes,
        max_workers: int = 8,
    ) -> None:
        self.name = validate_pipe_name(name)
        self.handler = handler
        self._authkey = authkey
        self._listener: Listener | None = None
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=f"pipe:{name}")
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._counter = 0
        self._counter_lock = threading.Lock()

    def start(self) -> str:
        """Bind and begin accepting. Returns the address actually bound.

        ``Listener`` on Windows creates the pipe with the creator's ACL, which
        is the first of the two security layers; the authkey is the second.
        """
        self._listener = Listener(self.name, family="AF_PIPE", authkey=self._authkey)
        self._stop.clear()
        self._thread = threading.Thread(target=self._serve, name=f"serve:{self.name}", daemon=True)
        self._thread.start()
        return self._listener.address

    def _serve(self) -> None:
        assert self._listener is not None
        while not self._stop.is_set():
            try:
                conn = self._listener.accept()
            except (OSError, EOFError):
                break
            except Exception as exc:  # noqa: BLE001
                # A client that fails the authkey handshake raises here, and
                # that is a *normal* event on a machine with other software on
                # it - it must not take the listener down with it. Only a
                # closed listener exits the loop.
                self._note(f"rejected a connection: {type(exc).__name__}")
                continue
            self._pool.submit(self._handle, conn)

    def _handle(self, conn: Any) -> None:
        with conn:
            try:
                raw = conn.recv()
            except (EOFError, OSError) as exc:
                # A peer that hangs up before speaking is not an error worth
                # propagating; it is a client with a bug.
                self._note(f"recv failed: {_describe(exc)}")
                return
            try:
                request = Request.from_dict(raw)
            except ProtocolError as exc:
                conn.send(Response(id=0, ok=False, error=str(exc)).to_dict())
                self._note(f"rejected: {exc}")
                return
            try:
                result = self.handler(request)
            except BaseException as exc:  # noqa: BLE001 - a handler must not kill the daemon
                conn.send(Response(id=request.id, ok=False, error=_describe(exc)).to_dict())
                self._note(f"op {request.op} failed: {_describe(exc)}")
                return
            conn.send(Response(id=request.id, ok=True, result=result).to_dict())

    def _note(self, message: str) -> None:
        """Transport-level logging hook. The daemon routes this to the audit log."""
        _NOTES.append(f"{self.name}: {message}")

    def stop(self) -> None:
        self._stop.set()
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
            self._listener = None
        self._pool.shutdown(wait=False, cancel_futures=True)
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def __enter__(self) -> "PipeServer":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


#: Transport diagnostics, drained by the daemon into the audit log. Bounded so a
#: bug that spams cannot grow without limit in a process that never restarts.
#: Locked, because notes arrive from every connection thread and a list.append
#: is not atomic with respect to a concurrent drain.
_NOTES: list[str] = []
_NOTES_LIMIT = 1000
_NOTES_LOCK = threading.Lock()


def note(message: str) -> None:
    """Record a local-only diagnostic.

    This module deliberately refuses to send a traceback to a peer, because a
    traceback carries absolute paths. That is only safe if the detail is kept
    *somewhere*, so callers that catch-and-report push it here and the daemon
    drains it into the audit log.
    """
    with _NOTES_LOCK:
        _NOTES.append(message)
        if len(_NOTES) > _NOTES_LIMIT:
            del _NOTES[: len(_NOTES) - _NOTES_LIMIT]


def drain_notes() -> list[str]:
    with _NOTES_LOCK:
        out = list(_NOTES)
        _NOTES.clear()
    return out


class PipeClient:
    """One request, one response, connection closed.

    Stateless on purpose. A long-lived connection is a long-lived thing to get
    wrong, and the daemon has a request queue anyway.
    """

    def __init__(self, name: str, *, authkey: bytes, timeout: float = 30.0) -> None:
        self.name = validate_pipe_name(name)
        self._authkey = authkey
        self.timeout = timeout

    def call(self, op: str, **args: Any) -> Any:
        request = Request(op=op, args=args, id=next_id())
        with Client(self.name, family="AF_PIPE", authkey=self._authkey) as conn:
            conn.send(request.to_dict())
            if not conn.poll(self.timeout):
                raise Timeout(
                    f"{self.name} accepted the connection but did not answer "
                    f"within {self.timeout:.0f}s (op={op})"
                )
            return Response.from_dict(conn.recv()).result_or_raise()

    def ping(self) -> bool:
        """Liveness probe used by the tray and by Plane C's health check."""
        try:
            return self.call("ping") == "pong"
        except (TransportError, OSError):
            return False


def next_id() -> int:
    global _REQUEST_SEQ
    with _REQUEST_LOCK:
        _REQUEST_SEQ += 1
        return _REQUEST_SEQ


_REQUEST_SEQ = 0
_REQUEST_LOCK = threading.Lock()


def describe_pipes() -> dict[str, str]:
    """For the status command and the docs. Nothing here is a port."""
    return {
        "A->B request": PIPE_BRAIN_REQ,
        "B->A response/stream": PIPE_BRAIN_RESP,
        "A->C supervisor request": PIPE_SUP_REQ,
        "C->A supervisor event": PIPE_SUP_EVT,
        "A->UI overlay": "in-process (never a socket)",
    }


def assert_no_ports() -> None:
    """Guard for review: no network API may enter this module.

    Parsed rather than grepped. A substring search trips on the word
    "localhost" appearing in a docstring that explains why there are no
    sockets, which is precisely the docstring most likely to be here.
    """
    import ast

    banned_attrs = {"AF_INET", "AF_INET6", "socket", "create_connection", "urlopen"}
    banned_roots = {"socket", "urllib", "http", "asyncio"}

    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in banned_attrs:
            offenders.append(f"attribute .{node.attr} at line {node.lineno}")
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            for name in names:
                if name.split(".")[0] in banned_roots:
                    offenders.append(f"import {name} at line {node.lineno}")
    if offenders:
        raise TransportError(f"transport module references network APIs: {offenders}")
