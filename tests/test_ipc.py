"""Tests for the named-pipe transport.

These use *real* pipes. A mock would prove nothing about ACLs, authkey
handshakes, or whether Windows will actually bind a name - which is the only
reason this module exists instead of a socket.
"""

from __future__ import annotations

import threading
import time
from multiprocessing.context import AuthenticationError

import pytest

from assistant.ipc import (
    ALL_PIPES,
    PIPE_SUP_REQ,
    PIPE_PREFIX,
    PROTOCOL,
    PipeClient,
    PipeServer,
    ProtocolError,
    Request,
    Response,
    Timeout,
    TransportError,
    UnknownPipe,
    assert_no_ports,
    describe_pipes,
    drain_notes,
    generate_authkey,
    load_authkey,
    store_authkey,
    validate_pipe_name,
)

# Unique per test so parallel runs and stale pipes from a crashed run cannot
# collide. Named pipes vanish when the last handle closes, but a hard kill can
# leave one behind briefly.
_COUNTER = iter(range(1, 10_000))


def unique_pipe(tag: str = "test") -> str:
    return rf"\\.\pipe\butleroy.test.{tag}.{next(_COUNTER)}"


@pytest.fixture(autouse=True)
def _clear_notes():
    drain_notes()
    yield
    drain_notes()


# -- naming ---------------------------------------------------------------


def test_pipe_names_are_named_pipes() -> None:
    for name in ALL_PIPES:
        assert validate_pipe_name(name).startswith(PIPE_PREFIX)


def test_a_tcp_style_name_is_refused() -> None:
    with pytest.raises(ProtocolError):
        validate_pipe_name("127.0.0.1:5555")
    with pytest.raises(ProtocolError):
        validate_pipe_name(PIPE_PREFIX + "x" * 300)


def test_transport_module_contains_no_network_api() -> None:
    assert_no_ports()


def test_describe_pipes_covers_the_documented_five() -> None:
    described = describe_pipes()
    assert len(described) == 5
    assert "never a socket" in described["A->UI overlay"]


# -- authkey --------------------------------------------------------------


def test_authkey_round_trips(tmp_path) -> None:
    key = generate_authkey()
    assert len(key) == 32
    path = tmp_path / "state" / "authkey"
    store_authkey(path, key)
    assert load_authkey(path) == key


def test_authkey_survives_every_possible_byte(tmp_path) -> None:
    """Regression: the store once wrote in text mode.

    A 32-byte random key contains a 0x0A byte roughly one time in eight, and
    Windows text-mode translation would rewrite it to 0x0D 0x0A on the way to
    disk. A single random round trip passes most of the time, so the only test
    that reliably catches this writes every byte value there is to write.
    """
    hostile = bytes(range(256)) * 2
    path = tmp_path / "authkey"
    store_authkey(path, hostile)
    assert load_authkey(path) == hostile
    assert path.stat().st_size == len(hostile)


def test_authkey_is_never_silently_overwritten(tmp_path) -> None:
    """Two daemons on one machine is a bug we want to hear about loudly."""
    path = tmp_path / "authkey"
    store_authkey(path, generate_authkey())
    with pytest.raises(FileExistsError):
        store_authkey(path, generate_authkey())


def test_truncated_authkey_is_refused(tmp_path) -> None:
    path = tmp_path / "authkey"
    path.write_bytes(b"short")
    with pytest.raises(ProtocolError):
        load_authkey(path)


# -- protocol framing -----------------------------------------------------


def test_request_round_trip() -> None:
    original = Request(op="file.read", args={"path": "C:/x.txt"}, id=7)
    assert Request.from_dict(original.to_dict()) == original


@pytest.mark.parametrize(
    "raw, why",
    [
        ([1, 2, 3], "a list, not an object"),
        ({"v": 99, "op": "x", "args": {}, "id": 1}, "a different protocol version"),
        ({"v": PROTOCOL, "args": {}, "id": 1}, "no op"),
        ({"v": PROTOCOL, "op": "", "args": {}, "id": 1}, "an empty op"),
        ({"v": PROTOCOL, "op": "x", "args": [], "id": 1}, "args that are not an object"),
        ({"v": PROTOCOL, "op": "x", "args": {}}, "no id"),
        ({"v": PROTOCOL, "op": "x", "args": {}, "id": "one"}, "a non-integer id"),
    ],
)
def test_malformed_requests_are_refused(raw, why) -> None:
    with pytest.raises(ProtocolError):
        Request.from_dict(raw)


def test_failed_response_raises_instead_of_returning_none() -> None:
    """A caller that forgets to check would otherwise get None from a denied
    request, which for a tool call is the most dangerous way to misbehave."""
    response = Response.from_dict({"v": PROTOCOL, "id": 1, "ok": False, "error": "denied"})
    with pytest.raises(ProtocolError, match="denied"):
        response.result_or_raise()


# -- live round trip ------------------------------------------------------


def test_round_trip_over_a_real_pipe() -> None:
    name = unique_pipe("roundtrip")
    key = generate_authkey()

    def handler(request: Request):
        return {"echo": request.op, "args": request.args}

    with PipeServer(name, handler, authkey=key):
        client = PipeClient(name, authkey=key, timeout=5.0)
        assert client.call("file.read", path="C:/x.txt") == {
            "echo": "file.read",
            "args": {"path": "C:/x.txt"},
        }
        assert client.ping() is False  # the handler does not know "ping"


def test_ping_round_trip() -> None:
    name = unique_pipe("ping")
    key = generate_authkey()
    with PipeServer(name, lambda r: "pong" if r.op == "ping" else None, authkey=key):
        assert PipeClient(name, authkey=key, timeout=5.0).ping() is True


def test_wrong_authkey_cannot_connect() -> None:
    """The boundary that matters: another process running as this user, with no
    secret, must not be able to drive an assistant that holds delete tools."""
    name = unique_pipe("auth")
    key = generate_authkey()
    with PipeServer(name, lambda r: "pong" if r.op == "ping" else "should not be reachable", authkey=key):
        intruder = PipeClient(name, authkey=generate_authkey(), timeout=2.0)
        with pytest.raises((OSError, EOFError, TransportError, AuthenticationError)):
            intruder.call("file.delete", path="C:/important.txt")
        # The server is still alive, proved with the *correct* key - a rejected
        # stranger must not take the listener down with them.
        assert PipeClient(name, authkey=key, timeout=5.0).ping() is True


def test_handler_exception_becomes_a_failed_response() -> None:
    """A worker must not be able to crash the daemon with a bad request."""
    name = unique_pipe("boom")
    key = generate_authkey()

    def handler(request: Request):
        raise ValueError("the disk is on fire")

    with PipeServer(name, handler, authkey=key):
        with pytest.raises(ProtocolError, match="ValueError: the disk is on fire"):
            PipeClient(name, authkey=key, timeout=5.0).call("system.info")


def test_handler_traceback_never_reaches_the_peer() -> None:
    name = unique_pipe("noleak")
    key = generate_authkey()
    secret = "C:/Users/PC Principal/secret/path"

    def handler(request: Request):
        raise FileNotFoundError(secret)

    with PipeServer(name, handler, authkey=key):
        with pytest.raises(ProtocolError) as caught:
            PipeClient(name, authkey=key, timeout=5.0).call("x")
    # The message is there for debugging...
    assert "FileNotFoundError" in str(caught.value)
    # ...but the path it complained about stays on this side of the pipe.
    assert "Traceback" not in str(caught.value)
    assert any(secret in note for note in drain_notes())


def test_server_survives_a_peer_that_hangs_up() -> None:
    name = unique_pipe("hangup")
    key = generate_authkey()
    served = threading.Event()

    def handler(request: Request):
        served.set()
        return "ok"

    with PipeServer(name, handler, authkey=key):
        from multiprocessing.connection import Client

        conn = Client(name, family="AF_PIPE", authkey=key)
        conn.close()  # hang up before saying anything
        time.sleep(0.2)
        assert PipeClient(name, authkey=key, timeout=5.0).call("ping") == "ok"
        assert served.is_set()


def test_client_times_out_rather_than_hanging_forever() -> None:
    name = unique_pipe("slow")
    key = generate_authkey()
    release = threading.Event()

    def handler(request: Request):
        release.wait(5.0)
        return "too late"

    with PipeServer(name, handler, authkey=key):
        try:
            with pytest.raises(Timeout, match="did not answer"):
                PipeClient(name, authkey=key, timeout=0.4).call("slow")
        finally:
            release.set()


def test_many_concurrent_clients() -> None:
    name = unique_pipe("many")
    key = generate_authkey()
    with PipeServer(name, lambda r: len(r.args.get("blob", "")), authkey=key, max_workers=8):
        results: list[int] = []
        lock = threading.Lock()

        def work(size: int) -> None:
            got = PipeClient(name, authkey=key, timeout=15.0).call("count", blob="x" * size)
            with lock:
                results.append(got)

        threads = [threading.Thread(target=work, args=(n * 100,)) for n in range(1, 13)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20.0)
        assert sorted(results) == [n * 100 for n in range(1, 13)]


def test_connecting_to_nothing_fails_fast() -> None:
    client = PipeClient(unique_pipe("absent"), authkey=generate_authkey(), timeout=1.0)
    assert client.ping() is False
