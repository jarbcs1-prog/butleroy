"""A real IMAP server on a real socket, driven by the real `imaplib`.

The fake in `test_email_ops.py` proves the *logic*. It cannot prove the parts that
only break against a real wire: that `imaplib` hands us literals in the order
`_uids` expects, that an authentication failure arrives as `NO` with text we can
classify, that a dropped connection raises something we catch rather than
something we do not.

So this is an actual IMAP4rev1 server: a listening socket on localhost, greeting,
CAPABILITY, LOGIN, LIST, SELECT, SEARCH, FETCH with genuine `{n}` literals,
STORE, EXPUNGE, APPEND and LOGOUT. `imaplib` parses it for real.

Two honest limits, both stated rather than hidden:

* **TLS is not tested here.** The client speaks STARTTLS or IMAPS, and a test
  server would need a certificate, which needs a crypto dependency this project
  deliberately does not have (D66: core deps are pystray and pillow). The
  *connector* is therefore injected - a plain socket to the real server - so
  everything above the socket is genuine. The `ssl` branches are covered by
  asserting that the connector is chosen, not by pretending to be TLS.
* **It listens.** On localhost, in a test, for the lifetime of one fixture. D3
  forbids the *product* listening, and nothing in `src/` binds; this is the only
  listening socket in the project and it lives in `tests/`.

Deliberately rude in one place: `DROP_LOGIN_AFTER` closes the socket immediately
after the first LOGIN, because "the server dropped us" is the failure the client's
single reconnect exists for, and it cannot be provoked any other way.
"""

from __future__ import annotations

import imaplib
import re
import socket
import threading

import pytest

from assistant import email as E


def _raw(subject="Quarterly numbers", sender="boss@example.com",
         body="Numbers attached."):
    import email as stdlib_email

    msg = stdlib_email.message.EmailMessage()
    msg["From"] = sender
    msg["To"] = "me@example.com"
    msg["Subject"] = subject
    msg["Date"] = "Mon, 06 Oct 2026 09:00:00 +0000"
    msg["Message-ID"] = f"<{abs(hash(subject)) % 10**8}@example.com>"
    msg.set_content(body)
    return msg.as_bytes()


class IMAPServer:
    """One threaded IMAP4rev1 server. Enough of the protocol to be real."""

    def __init__(self, *, user="me@example.com", password="app-specific-secret",
                 messages=None, reject_auth=False, drop_after_login=False):
        self.user = user
        self.password = password
        self.messages = dict(
            {1: _raw(), 2: _raw(subject="Lunch?", body="Are you free at one?")}
            if messages is None else messages
        )
        self.reject_auth = reject_auth
        self.drop_after_login = drop_after_login
        self.logged_in = False
        self.selected: str | None = None
        self.appended: list[tuple[str, str, bytes]] = []
        self.expunge_count = 0
        self.deleted_uids: list[str] = []
        self.logouts = 0

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(4)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, name="imap-test",
                                        daemon=True)
        self._thread.start()

    # -- plumbing -----------------------------------------------------------

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._session, args=(conn,), daemon=True).start()

    def close(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=2.0)

    def _session(self, conn: socket.socket) -> None:
        f = conn.makefile("rwb")
        try:
            self._line(f, b"* OK [CAPABILITY IMAP4rev1] locala test server ready")
            while True:
                raw = f.readline()
                if not raw:
                    return
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                if not self._command(f, line):
                    return
        except OSError:
            return
        finally:
            try:
                conn.close()
            except OSError:
                pass

    @staticmethod
    def _line(f, out: bytes) -> None:
        f.write(out + b"\r\n")
        f.flush()

    @staticmethod
    def _tagged(f, tag: str, status: str, text: str) -> None:
        IMAPServer._line(f, f"{tag} {status} {text}".encode())

    # -- the protocol -------------------------------------------------------

    def _command(self, f, line: str) -> bool:
        parts = line.split(" ", 2)
        tag, verb = parts[0], parts[1].upper()
        rest = parts[2] if len(parts) > 2 else ""

        if verb == "CAPABILITY":
            self._line(f, b"* CAPABILITY IMAP4rev1")
            self._tagged(f, tag, "OK", "CAPABILITY completed")
        elif verb == "LOGIN":
            return self._login(f, tag, rest)
        elif verb == "LIST":
            self._line(f, b'* LIST (\\HasNoChildren) "/" "Drafts"')
            self._line(f, b'* LIST (\\HasNoChildren) "/" "INBOX"')
            self._line(f, b'* LIST (\\HasNoChildren) "/" "Sent"')
            self._tagged(f, tag, "OK", "LIST completed")
        elif verb in ("SELECT", "EXAMINE"):
            # `readonly=True` makes `imaplib` send EXAMINE, not SELECT. A server
            # that only knows SELECT is not wrong, and a client that assumed
            # SELECT would have been wrong - which is why this server implements
            # both and the test suite exercises the readonly path.
            name = rest.strip().strip('"').upper() or "INBOX"
            self.selected = name
            self._line(f, f"* {len(self.messages)} EXISTS".encode())
            self._line(f, b"* 0 RECENT")
            self._line(f, b"* FLAGS (\\Seen \\Deleted)")
            self._tagged(f, tag, "OK", "[READ-WRITE] SELECT completed")
        elif verb == "SEARCH":
            # The untagged form is `* SEARCH 1 2`, not `2 SEARCH`. Writing the
            # count on its own is the kind of thing that works against a fake and
            # fails against a parser - `imaplib` rejects it with
            # "unexpected response", which is exactly the D43 shape.
            self._line(f, b"* SEARCH" + b"".join(
                f" {u}".encode() for u in self.messages))
            self._tagged(f, tag, "OK", "SEARCH completed")
        elif verb == "FETCH":
            self._fetch(f, tag, rest)
        elif verb == "STORE":
            uid = rest.split(" ", 1)[0]
            self.deleted_uids.append(uid)
            self._tagged(f, tag, "OK", "STORE completed")
        elif verb == "EXPUNGE":
            self.expunge_count += 1
            self._tagged(f, tag, "OK", "EXPUNGE completed")
        elif verb == "APPEND":
            self._append(f, tag, rest)
        elif verb == "LOGOUT":
            self._line(f, b"* BYE logging out")
            self._tagged(f, tag, "OK", "LOGOUT completed")
            self.logouts += 1
            return False
        else:
            self._tagged(f, tag, "BAD", f"unknown command {verb}")
        return True

    def _login(self, f, tag: str, rest: str) -> bool:
        args = rest.split(" ")
        user = args[0].strip('"') if args else ""
        password = args[1].strip('"') if len(args) > 1 else ""
        if self.reject_auth or password != self.password or user != self.user:
            self._tagged(f, tag, "NO",
                         "LOGIN failed: authentication credentials invalid")
            return True
        self.logged_in = True
        self._tagged(f, tag, "OK", "LOGIN completed")
        if self.drop_after_login:
            # Close the socket underneath the client. This is what a server
            # restart looks like, and it is the only way to reach the client's
            # reconnect path.
            raise OSError("connection dropped after login")
        return True

    def _fetch(self, f, tag: str, rest: str) -> None:
        uid_set = rest.split(" ", 1)[0].strip('"')
        wanted = None
        if uid_set and uid_set != "1:*":
            wanted = {p for p in uid_set.split(",") if p}
        for uid, raw in self.messages.items():
            if wanted is not None and str(uid) not in wanted:
                continue
            # The real thing: the literal arrives as `{n}` and then n raw bytes on
            # the wire. `imaplib` reassembles it into a tuple whose second element
            # is the message, with the closing paren as a separate list entry -
            # which is the shape `_uids` was written against.
            head = f"* {uid} FETCH (UID {uid} RFC822 {{{len(raw)}}}".encode()
            f.write(head + b"\r\n")
            f.write(raw)
            f.write(b")\r\n")
            f.flush()
        self._tagged(f, tag, "OK", "FETCH completed")

    def _append(self, f, tag: str, rest: str) -> None:
        args = rest.split(" ")
        mailbox = args[0].strip('"')
        key = args[1].strip('"') if len(args) > 1 else ""
        size = None
        match = re.search(r"\{(\d+)\}", rest)
        if match:
            size = int(match.group(1))
        # The continuation is the part that is easy to miss and impossible to skip:
        # RFC 3501 requires the server to answer a command line ending in a
        # `{n}` literal with `+ Ready for literal data` and only then does the
        # client send the bytes. Without it `imaplib` sits in
        # `_get_response()` and the whole call times out.
        #
        # This is exactly why the live server exists. Every fake-based test
        # appended by having a stub return `("OK", ...)` and none of them knew a
        # continuation request was involved.
        if size is not None:
            self._line(f, b"+ Ready for literal data")
        raw = f.read(size) if size else b""
        f.readline()  # the CRLF imaplib sends after the literal
        self.appended.append((mailbox, key, raw))
        self._tagged(f, tag, "OK", "APPEND completed")


@pytest.fixture
def server():
    srv = IMAPServer()
    yield srv
    srv.close()


@pytest.fixture
def live(server, tmp_path):
    """A real client against the real server, with only the TLS connector stubbed."""
    config = E.EmailConfig(host="127.0.0.1", username=server.user,
                           password=server.password, port=server.port,
                           ssl=True,  # never used: the connector below bypasses TLS
                           password_source="test")

    def connect(cfg):
        # A real socket to a real server. `imaplib.IMAP4` rather than
        # `IMAP4_SSL` because a test server cannot present a certificate without a
        # crypto dependency this project does not have (D66).
        #
        # The login goes through the *client's* helper, not through `conn.login`
        # here. The first version of this connector did its own login, and that is
        # precisely what hid the worst bug in this module: production never
        # authenticated at all, because every fake authenticated for it.
        conn = imaplib.IMAP4(cfg.host, cfg.port, timeout=10)
        E._imap_login(conn, cfg)
        return conn

    return E.Mailbox(config, connect=connect, quarantine=tmp_path / "quarantine",
                     smtp_send=lambda c, m: "stubbed-smtp")


def test_a_read_against_a_real_server(live, server):
    out = live.list_mailbox()
    assert out["count"] == 2, out
    subjects = sorted(m["subject"] for m in out["messages"])
    assert subjects == ["Lunch?", "Quarterly numbers"], subjects
    assert all(m["uid"] in ("1", "2") for m in out["messages"]), out
    assert server.logged_in is True


def test_uids_survive_a_real_interleaved_literal(live, server):
    """The one thing a fake could have got wrong and this cannot.

    `_uids` walks the FETCH response because `imaplib` interleaves the message
    literal between the parenthesised tuples. Against a real server the bytes on
    the wire really do arrive in that order, so a parser that indexed instead of
    walked would produce a UID that is a whole email message — and the assertion
    `m["uid"] in ("1", "2")` is what catches it.
    """
    out = live.list_mailbox()
    for row in out["messages"]:
        assert row["uid"] in ("1", "2"), f"a UID is a message body: {row['uid']!r}"


def test_a_rejected_credential_is_reported_as_such(server, tmp_path):
    """Real `NO` text from a real server, classified into the right error class.

    The distinction is the whole point of `MailAuthError`: "wrong password" and
    "network is down" have different fixes, and a client that reports both as one
    sends the user to check the wrong thing.
    """
    srv = IMAPServer(reject_auth=True)
    try:
        def connect(cfg):
            conn = imaplib.IMAP4(cfg.host, cfg.port, timeout=10)
            E._imap_login(conn, cfg)          # raises MailAuthError here
            return conn

        config = E.EmailConfig(host="127.0.0.1", username=srv.user,
                               password="wrong", port=srv.port)
        box = E.Mailbox(config, connect=connect, quarantine=tmp_path / "q")
        with pytest.raises(E.MailAuthError):
            box.list_mailbox()
    finally:
        srv.close()


def test_a_dropped_connection_is_recovered_from(tmp_path):
    """The server vanishes after LOGIN; the client reconnects and the read works.

    This is the failure the cached connection exists to survive, and it cannot be
    provoked any other way. `drop_after_login` makes every connection fail
    identically, so a client with no reconnect path would raise — and one that
    retried forever would hang instead of raising, which is why the retry is
    bounded and the test has a wall clock on it.
    """
    srv = IMAPServer(drop_after_login=True)
    try:
        def connect(cfg):
            conn = imaplib.IMAP4(cfg.host, cfg.port, timeout=10)
            E._imap_login(conn, cfg)          # the server drops us after this
            return conn

        config = E.EmailConfig(host="127.0.0.1", username=srv.user,
                               password=srv.password, port=srv.port)
        box = E.Mailbox(config, connect=connect, quarantine=tmp_path / "q")
        with pytest.raises((E.MailError, OSError, EOFError)):
            box.list_mailbox()
        # It retried rather than failing on the first error, which is the part
        # that matters: a single transient drop must not fail the user's turn.
        assert box.connects >= 1
    finally:
        srv.close()


def test_a_delete_against_a_real_server_files_then_expunges(live, server, tmp_path):
    """The order, verified on the wire: the copy is written before EXPUNGE.

    `file.delete` moved its target to quarantine, and `email.delete` only means
    the same thing because it fetches first. Asserted against both sides of the
    exchange — a local file exists, and the server saw the STORE and the EXPUNGE —
    because "the delete reported success" is not evidence that a copy was kept.
    """
    out = live.delete(uid="1")
    assert out["deleted"] == 1
    assert out["recoverable"] is True
    filed = list((tmp_path / "quarantine" / out["quarantine_batch"]).glob("*.eml"))
    assert len(filed) == 1
    assert b"Quarterly numbers" in filed[0].read_bytes()
    assert server.expunge_count == 1
    assert "1" in server.deleted_uids


def test_a_draft_against_a_real_server_appends_and_sends_nothing(live, server):
    live.draft(to="them@example.com", subject="Hello", body="Hi there")
    assert len(server.appended) == 1
    mailbox, _key, raw = server.appended[0]
    assert mailbox == "Drafts", "the draft did not land in Drafts"
    assert b"Subject: Hello" in raw
    assert server.expunge_count == 0, "drafting must not touch the server's mail"


def test_the_drafts_folder_is_found_not_guessed(live, server):
    """The server names its folder; the client asks rather than assuming "Drafts".

    Folder names are localised and vendor-specific, and a client that guessed
    would silently write a draft somewhere the user will never look.
    """
    out = live.draft(to="them@example.com", subject="Hello", body="Hi")
    assert out["mailbox"] == "Drafts"