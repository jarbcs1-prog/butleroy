"""IMAP. The last transport in scope, and the first one that carries a credential.

What this is
------------
Six tools' worth of mail access over `imaplib`, which is stdlib. That matters more
than usual here: Plane A is deliberately dependency-free (D66 — core deps are
`pystray` and `pillow` only), and a mail client is exactly the kind of thing that
arrives as a 40 MB dependency with its own TLS stack and its own idea about where
credentials live. `imaplib` is in the standard library, so this module adds
nothing to the install and nothing to the resident footprint.

Three rules, and why each is here rather than assumed
------------------------------------------------------

**No listening socket, one outbound connection, to a configured host.** D3 said
"no TCP anywhere" and that is still true of how our components talk to *each
other* — named pipes, never sockets. Telegram already narrowed this honestly
rather than breaking it quietly, and mail does the same thing: an IMAP session is
an outbound connection to a server the user named. `_connect` refuses any host
that is not the configured one, so no message content can redirect it, and
nothing here ever binds or listens.

**The password is read from configuration, never from an argument.** This is the
sharpest difference from every other tool here. A password that arrives as a tool
argument is a password that lands in the audit chain — append-only, hash-chained,
and the most-read file in the project. `_redact` would catch a key containing
"password", but the right answer is that the credential is never a parameter at
all: `EmailConfig` holds it, `load_config` reads it from disk or the environment,
and no `email.*` tool has a credential argument. A test asserts the absence.

**An environment credential that is set but wrong is refused, not ignored.** Same
rule as `telegram.load_config`, and for the same reason: silently falling back
would make a typo look exactly like a success, and the user would keep using the
old password without knowing why.

The `folder` problem, measured rather than assumed
--------------------------------------------------
D63 found that the policy gate screens any string argument whose *key* contains
"dir", so a documented argument called `subdir` was refused as a path outside
every approved root. The obvious spelling for a mail folder is `folder`, and the
path screen contains "folder" too. Measured against the real gate:

    mailbox = 'INBOX'    -> allow        folder = 'INBOX'            -> DENY
    mailbox = 'Sent'     -> allow        folder = 'Sent'             -> DENY
    mailbox = '[Gmail]/All Mail' -> allow folder = '[Gmail]/All Mail' -> DENY

Every real IMAP mailbox name is refused. So the argument is `mailbox`, and the
registry says so. Carving "folder" out of the path screen was the alternative and
it is the same trade D63 rejected: a hole in the rule is still a hole, and a rule
the model cannot express is not a rule.

A note on naming
----------------
The module is `email.py` because the tools are `email.*`, and the stdlib module it
would otherwise shadow is imported as `email` *inside* this one where needed. The
shadowing is real and it is confined: nothing else in the project imports the stdlib
`email` package, and a `from __future__ import annotations` plus absolute imports
(`from . import ipc`) mean no implicit relative import can pick up the wrong one.

Deleting a message is recoverable, and that is a decision not an accident
---------------------------------------------------------------------------
`file.delete` moves its target into `state/quarantine/` and writes a manifest,
because "delete" that cannot be undone is not what this project means by the word.
IMAP offers no such thing natively: `\Deleted` followed by `EXPUNGE` is gone from
the server, and the only copy is whatever the client kept. So `email.delete`
fetches the message first, writes it into the same quarantine tree with a manifest
entry, and only then marks and expunges.

That makes the operation recoverable on *this* machine and honest about the fact
that it is not recoverable on the server: the user's copy is filed, and the
message is gone from the mail store. The preview says both, because a dialog that
said only "delete" would be describing a weaker action than the one taken.
"""

from __future__ import annotations

import email as stdlib_email
import imaplib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

__all__ = [
    "EmailConfig",
    "MailError",
    "MailAuthError",
    "MailNotConfigured",
    "Mailbox",
    "load_config",
    "PASSWORD_ENV_VAR",
    "DEFAULT_MAILBOX",
]

#: The environment variable that may carry the password instead of the file.
#: Spelled out in INSTALL.md alongside the Telegram token.
PASSWORD_ENV_VAR = "LOCALAIZED_EMAIL_PASSWORD"

#: Where a deleted message is filed. Same tree `file.delete` uses, deliberately:
#: one place to look for "what did the assistant remove", not two.
DEFAULT_MAILBOX = "INBOX"

#: Connect and command timeouts. Generous for a home connection, short enough that
#: a dead server cannot hold a turn open: a T0 read that blocks for two minutes
#: looks identical to a hung assistant.
CONNECT_TIMEOUT_S = 20.0
COMMAND_TIMEOUT_S = 30.0

#: How long an established connection is reused before being rebuilt. IMAP servers
#: drop idle sessions on their own schedule and without warning, so a cached
#: connection is an optimisation with an expiry rather than a pool to trust.
IDLE_REUSE_S = 240.0

#: Bodies are truncated for display. A megabyte-long message must not be able to
#: wedge the transcript, and truncation is always reported (D55's rule: the same
#: number in two units is still a discrepancy a reader has to be told about).
BODY_CAP = 4000


class MailError(RuntimeError):
    """Something about the mail request was wrong."""


class MailAuthError(MailError):
    """The server rejected the credential. Distinct from a network failure.

    Conflating these is how a rotated password looks like a flaky network, and the
    user retries the wrong thing for an hour.
    """


class MailNotConfigured(MailError):
    """There is no mail configuration at all.

    Its own class, and never a default. A bridge that starts with no allowlist and
    quietly ignores everything looks exactly like a working one with nobody talking
    to it, and the first is a bug while the second is a Tuesday.
    """


@dataclass
class EmailConfig:
    """Host, port, account and password. The password is never an argument.

    `describe()` deliberately omits it and `__repr__` is overridden, for the reason
    `TelegramConfig` is: a config object ends up in a log line, a traceback or a
    support paste, and "the object printed its own password" is the kind of leak
    that is discovered by someone else.
    """

    host: str
    username: str
    password: str = field(repr=False)
    port: int = 993
    #: True for implicit TLS (IMAPS, the default and what every provider offers).
    #: False means STARTTLS, which is plaintext-then-upgrade and is refused below
    #: unless the user asked for it explicitly.
    ssl: bool = True
    mailbox: str = DEFAULT_MAILBOX
    password_source: str = "unknown"

    def __post_init__(self) -> None:
        if not self.host or "." not in self.host:
            raise MailError(
                f"email host {self.host!r} does not look like a hostname; "
                "refusing rather than guessing"
            )
        if not self.username:
            raise MailError("email username is empty")
        if not self.password:
            raise MailError("email password is empty")

    def describe(self) -> dict[str, Any]:
        """Everything except the password. This is what `status` may show.

        `password_source` is safe because it names the *location* and never the
        value, which is the distinction that matters when someone is deciding
        whether they still have a plaintext credential on disk.
        """
        return {
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "ssl": self.ssl,
            "mailbox": self.mailbox,
            "password_source": self.password_source,
        }

    def __repr__(self) -> str:
        return (
            f"<EmailConfig {self.username}@{self.host}:{self.port} "
            f"password hidden>"
        )


def load_config(path: Path) -> EmailConfig:
    """Read the mail configuration. Raises rather than defaulting.

    Shape mirrors `telegram.load_config` on purpose — same rules, same reasons, so
    a reader who knows one knows the other:

    * the password may come from `LOCALAIZED_EMAIL_PASSWORD` so the credential need
      not exist on disk at all;
    * host, port, username and the default mailbox always come from the file,
      because those are configuration rather than secrets and belong somewhere a
      human edits them;
    * an environment password that is set to the empty string *is* ignored, because
      a cleared variable is a recoverable misconfiguration and refusing it would
      break a machine that was working an hour ago;
    * and an environment password that is set but missing the file's other keys is
      refused, not half-honoured.
    """
    env_password = os.environ.get(PASSWORD_ENV_VAR, "")
    if not path.exists():
        if env_password:
            raise MailNotConfigured(
                f"no email configuration at {path}, and {PASSWORD_ENV_VAR} carries "
                "only the password. The file is still required for email_host and "
                "email_username."
            )
        raise MailNotConfigured(
            f"no email configuration at {path}. Create it, or set "
            f"{PASSWORD_ENV_VAR} plus the host and username in the file."
        )

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MailNotConfigured(f"{path} is not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise MailNotConfigured(f"{path}: top level must be an object")

    missing = [k for k in ("email_host", "email_username") if k not in data]
    if missing:
        raise MailNotConfigured(f"{path} is missing {missing}")

    password = env_password or str(data.get("email_password", "") or "")
    if not password:
        raise MailNotConfigured(
            f"{path} has no email_password, and {PASSWORD_ENV_VAR} is not set"
        )
    source = (
        PASSWORD_ENV_VAR if env_password
        else f"{path} (plaintext on disk)"
    )

    return EmailConfig(
        host=str(data["email_host"]),
        username=str(data["email_username"]),
        password=password,
        port=int(data.get("email_port", 993)),
        ssl=bool(data.get("email_ssl", True)),
        mailbox=str(data.get("email_mailbox", DEFAULT_MAILBOX)),
        password_source=source,
    )


#: `imaplib`'s date_list handling, in one sentence, because getting it wrong costs
#: a real failure: `append()` accepts a timezone-aware `datetime` and formats it,
#: or a *fully quoted* string it assumes is already correct. Anything else — a bare
#: `dd-Mon-yyyy`, an RFC 2822 date taken from the message's own Date header — is
#: rejected with `ValueError: date_time not of a known type` before a byte reaches
#: the server. So this passes a `datetime` and lets imaplib do the formatting,
#: rather than hand-building a string in a format whose quoting rules are not
#: documented in the docstring it is called from.
def _imap_datetime(when: datetime | None = None) -> datetime:
    """An aware datetime, which is the one form `append` is happy with."""
    return when or datetime.now().astimezone()


def _text(raw: Any) -> str:
    """A server literal as text. `imaplib` returns bytes; `str(b'12')` is `"b'12'"`.

    Every literal crosses this function and nowhere else, because the alternative —
    `str()` at each call site — produced a count of `b'12'` that raised in `int()`
    and a UID list full of message bodies. One conversion, one place to get it
    wrong.
    """
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return str(raw or "")


def _header(msg: Any, name: str, default: str = "") -> str:
    """One decoded header, tolerant of a malformed message.

    `str` on a header can raise on a message with a broken charset, and a mail
    client that dies on one bad email is a mail client that cannot be used.
    """
    try:
        value = msg.get(name, default)
    except Exception:  # noqa: BLE001
        return default
    if isinstance(value, tuple):  # a header parsed as a tuple stays one
        value = value[0] if value else default
    try:
        return str(value)
    except Exception:  # noqa: BLE001
        return default


def summarise(raw: bytes, *, body_cap: int = BODY_CAP) -> dict[str, Any]:
    """Headers plus a bounded body, from a raw RFC822 message.

    Used by both the reads and the delete-preview, so what the user is shown before
    approving is produced by the same code that will read it afterwards. A preview
    built by a second implementation is a preview that can disagree.
    """
    msg = stdlib_email.message_from_bytes(raw)
    body, truncated, attachments = "", False, 0
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            disposition = str(part.get("Content-Disposition", ""))
            if "attachment" in disposition.lower():
                attachments += 1
                continue
            if part.get_content_type() == "text/plain":
                try:
                    body = part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", "replace"
                    )
                except Exception:  # noqa: BLE001
                    body = str(part.get_payload())
                break
    else:
        try:
            payload = msg.get_payload(decode=True)
            body = (payload or b"").decode(msg.get_content_charset() or "utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = str(msg.get_payload())

    if len(body) > body_cap:
        body, truncated = body[:body_cap], True

    return {
        "from": _header(msg, "From"),
        "to": _header(msg, "To"),
        "cc": _header(msg, "Cc"),
        "subject": _header(msg, "Subject"),
        "date": _header(msg, "Date"),
        "message_id": _header(msg, "Message-ID"),
        "in_reply_to": _header(msg, "In-Reply-To"),
        "attachments": attachments,
        "body": body,
        "body_truncated": truncated,
        "body_chars": len(body),
    }


class Mailbox:
    """One IMAP session, opened on demand and reused briefly.

    The `connect` factory is injected for the same reason `TelegramBridge`'s
    transport is: every branch — a server that is not there, a credential that is
    rejected, a message that is malformed, an expunge that fails halfway — must be
    reachable in a test with no network. A real client that only works when the
    internet cooperates is not a client you can change safely.

    Connections are *cached*, not pooled, and the cache expires: an IMAP server
    closes idle sessions without warning, so a held-open connection is a future
    `NOOP`-on-a-dead-socket error. Every operation tolerates a stale connection by
    reconnecting once, which is the difference between a read that works and a
    read that works until the server restarts.
    """

    def __init__(
        self,
        config: EmailConfig,
        *,
        connect: Callable[[EmailConfig], Any] | None = None,
        smtp_send: Callable[[EmailConfig, Any], str] | None = None,
        quarantine: Path | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self._connect = connect or _imap_connect
        # The SMTP half is injectable for the same reason the IMAP half is. It is
        # not a detail: without it there is no way to test `email.send` at all,
        # because the send would leave the machine. The first version of this
        # module had no seam here and the test reached a real DNS lookup for
        # `imap.example.com` — which is what finally made the hole obvious.
        self._smtp = smtp_send or _smtp_send
        self._quarantine = Path(quarantine) if quarantine else None
        self._clock = clock
        self._conn: Any = None
        self._opened_at = 0.0
        self.connects = 0
        self.last_error: str | None = None

    # -- connection ---------------------------------------------------------

    def _session(self, *, fresh: bool = False) -> Any:
        if not fresh and self._conn is not None:
            if self._clock() - self._opened_at < IDLE_REUSE_S:
                return self._conn
            self._close()
        self._conn = self._connect(self.config)
        self._opened_at = self._clock()
        self.connects += 1
        self.last_error = None
        return self._conn

    def _close(self) -> None:
        if self._conn is None:
            return
        try:
            self._conn.logout()
        except Exception:  # noqa: BLE001 - a dead socket has nothing to log out of
            pass
        self._conn = None

    def _run(self, method: str, *args: Any, retry: bool = True,
             **kwargs: Any) -> tuple[str, Any]:
        """One IMAP command, reconnecting once on a stale connection.

        The retry is bounded and silent on purpose. A server that dropped us is an
        ordinary event, not an error to report; a server that rejects us twice is
        a real failure and is raised.
        """
        conn = self._session(fresh=False)
        try:
            status, data = getattr(conn, method)(*args, **kwargs)
        except (imaplib.IMAP4.abort, OSError) as exc:
            self._close()
            if not retry:
                self.last_error = f"{type(exc).__name__}: {exc}"
                raise MailError(f"the mail server connection failed: {exc}") from None
            # One reconnect. `fresh=True` skips the cache, so a stale socket is
            # replaced rather than reused.
            conn = self._session(fresh=True)
            status, data = getattr(conn, method)(*args, **kwargs)
        except imaplib.IMAP4.error as exc:
            # NOT retried, and NOT the same class as an abort. `imaplib` raises a
            # bare `IMAP4.error` when the server answers NO — which is what a
            # rejected LOGIN looks like — and this branch is the only place that
            # can turn that into a `MailAuthError`.
            #
            # Found by running against a real server: the fake returned
            # `(status, data)` pairs, so `_check` saw the failure and classified
            # it, and every fake-based test of `MailAuthError` passed against a
            # client that in production raised a raw `IMAP4.error` and never
            # reached `_check` at all. The distinction between "wrong password"
            # and "network is down" is the entire reason the class exists, and it
            # did not exist until a real server answered NO.
            raise self._classify(exc) from None
        return self._check(status, data, method)

    @staticmethod
    def _classify(exc: Exception) -> MailError:
        """A raw `IMAP4.error` as one of our errors.

        Same rule as `_check`, on a different input: an authentication failure is
        a different problem from a broken command, and the user fixes them
        differently.
        """
        text = str(exc)
        lowered = text.lower()
        if any(w in lowered for w in ("authentication", "authenticate", "login",
                                      "credentials", "invalid login")):
            return MailAuthError(
                "the mail server rejected the credential. Check email_username and "
                "the password; most providers want an app-specific password rather "
                "than the account password."
            )
        return MailError(f"the mail server refused the command: {text}")

    def _check(self, status: str, data: Any, method: str) -> tuple[str, Any]:
        """Turn an IMAP status into an exception or a pass.

        `NO` covers both "no such message" and "bad password", and the two are not
        the same fact about the world: one is a wrong request and the other is a
        rotated credential. The text says which, and the class follows, because a
        caller that retries a rejected password is wasting the user's time.
        """
        if status == "OK":
            return status, data
        text = " ".join(_text(b) for b in (data or []))
        lowered = text.lower()
        if any(w in lowered for w in ("authentication", "authenticate", "login",
                                      "credentials", "invalid login")):
            raise MailAuthError(
                "the mail server rejected the credential. Check email_username and "
                "the password; most providers want an app-specific password rather "
                "than the account password."
            )
        self.last_error = f"{method}: {status} {text}".strip()
        raise MailError(f"{method} failed: {status} {text}".strip())

    def select(self, mailbox: str | None = None, *, readonly: bool = False) -> str:
        name = mailbox or self.config.mailbox
        _, data = self._run("select", f'"{name}"' if " " in name else name,
                            readonly=readonly)
        # SELECT answers with the message count, which is also the proof that the
        # mailbox name was understood rather than silently mapped to INBOX.
        #
        # Decoded rather than `str()`-ed, which is D43 in miniature and was caught
        # by the test that asserts a count: `str(b"12")` is `"b'12'"`, and
        # `int("b'12'")` raises. Every literal out of imaplib is bytes; the one
        # place that forgot produced a read tool that failed on every call.
        return _text(data[0]).split()[0] if data and data[0] else "0"

    # -- reads --------------------------------------------------------------

    def list_mailbox(self, *, mailbox: str | None = None, limit: int = 20,
                     unread_only: bool = False, fetch: bool = True) -> dict[str, Any]:
        """The newest messages, headers only unless `fetch` is set."""
        name = mailbox or self.config.mailbox
        total = self.select(name, readonly=True)
        if unread_only:
            self._run("search", None, "UNSEEN")
        else:
            self._run("search", None, "ALL")
        _, found = self._run("fetch", "1:*", "(UID)")
        uids = self._uids(found)[-max(1, int(limit)):]
        if not uids:
            return {"mailbox": name, "count": 0, "total": int(total or 0),
                    "messages": [], "note": f"{name} has no matching messages"}

        rows = []
        for uid in uids:
            item = self._headers(uid, name)
            if item:
                rows.append(item)
        rows.reverse()  # newest last, which is how a mailbox reads
        return {"mailbox": name, "count": len(rows), "total": int(total or 0),
                "messages": rows}

    def read_message(self, *, uid: str, mailbox: str | None = None) -> dict[str, Any]:
        name = mailbox or self.config.mailbox
        self.select(name, readonly=True)
        raw = self._fetch_raw(uid)
        if raw is None:
            raise MailError(f"no message with UID {uid} in {name}")
        out = summarise(raw)
        out.update({"uid": str(uid), "mailbox": name})
        return out

    def search_mailbox(self, *, query: str, mailbox: str | None = None,
                       limit: int = 20) -> dict[str, Any]:
        """Search server-side, then read the hits.

        The server does the matching rather than us downloading everything and
        grepping locally: a mailbox is a user's private correspondence and the
        point of a search tool is not to move all of it through this process.
        """
        name = mailbox or self.config.mailbox
        self.select(name, readonly=True)
        self._run("search", None, "TEXT", f'"{query}"')
        _, found = self._run("fetch", "1:*", "(UID)")
        uids = self._uids(found)[-max(1, int(limit)):]
        rows = [self._headers(u, name) for u in uids]
        return {
            "mailbox": name,
            "query": query,
            "count": len([r for r in rows if r]),
            "messages": [r for r in rows if r],
            "note": ("IMAP TEXT search matches headers and body; it is not a "
                     "full-text index and ordering is server-defined"),
        }

    # -- writes -------------------------------------------------------------

    def draft(self, *, to: str, subject: str, body: str,
              cc: str = "") -> dict[str, Any]:
        """Stage a message in the server's Drafts folder. Sends nothing.

        T1 because the only irreversible thing about it happens when a *human*
        later opens the folder and clicks send. There is no T3-worthy act here, and
        labelling it T3 would teach the user to approve dialogs reflexively.
        """
        name = self.config.mailbox
        self.select(name)
        msg = stdlib_email.message.EmailMessage()
        msg["To"] = to
        if cc:
            msg["Cc"] = cc
        msg["Subject"] = subject
        msg.set_content(body)

        drafts = self._drafts_mailbox()
        self.select(drafts)
        stamp = time.strftime("%Y%m%d%H%M%S")
        stored = f"{self.config.username}/drafts/{stamp}-{abs(hash(subject)) % 10**6}"
        # The IMAP date_list is `dd-Mon-yyyy` with English month abbreviations -
        # NOT the RFC 2822 date in the message's own Date header. Passing the
        # header's value makes `imaplib` raise `ValueError: date_time not of a
        # known type` before a byte is sent, which is how this was found: against
        # a real server, not against a fake that accepted whatever it was given.
        status, data = self._run("append", drafts, stored,
                                 _imap_datetime(), msg.as_bytes())
        if status != "OK":
            raise MailError(f"the draft could not be stored: {status}")
        return {
            "drafted": True,
            "sent": False,
            "mailbox": drafts,
            "imap_key": stored,
            "to": to,
            "cc": cc,
            "subject": subject,
            "characters": len(body),
            "note": "nothing has been sent; open the Drafts folder to read and send it",
        }

    def send(self, *, to: str, subject: str, body: str, cc: str = "",
             reply_to: str = "") -> dict[str, Any]:
        """Append to Sent, then SMTP. T3 external, and the payload is the risk."""
        name = self.config.mailbox
        self.select(name)

        msg = stdlib_email.message.EmailMessage()
        msg["To"] = to
        if cc:
            msg["Cc"] = cc
        msg["Subject"] = subject
        msg["From"] = self.config.username
        if reply_to:
            msg["In-Reply-To"] = reply_to
            msg["References"] = reply_to
        msg.set_content(body)

        sender = self._smtp(self.config, msg)
        sent_at = time.time()

        # The Sent copy is best-effort and deliberately *after* the send. A failure
        # to file a copy must not be reported as a failure to send — the message
        # has already left, and saying otherwise would be the one lie the user
        # cannot detect.
        filed = False
        try:
            self.select(name)
            stamp = time.strftime("%Y%m%d%H%M%S")
            self._run("append", name, f"{self.config.username}/sent/{stamp}",
                      _imap_datetime(), msg.as_bytes())
            filed = True
        except Exception:  # noqa: BLE001
            pass

        return {
            "sent": True,
            "to": to,
            "cc": cc,
            "subject": subject,
            "characters": len(body),
            "sent_at": sent_at,
            "sender": sender,
            "copied_to_sent": filed,
            **({} if filed else {"note":
                "delivered, but the Sent copy could not be filed"}),
        }

    def delete(self, *, uid: str, mailbox: str | None = None) -> dict[str, Any]:
        """File the message locally, *then* mark it deleted and expunge.

        The order is the point. `\\Deleted` + `EXPUNGE` is gone from the server and
        the only copy is the one the client kept, so fetching first is what makes
        the word "delete" mean the same thing here as it does in `file.delete`.
        """
        name = mailbox or self.config.mailbox
        self.select(name)
        raw = self._fetch_raw(uid)
        if raw is None:
            raise MailError(f"no message with UID {uid} in {name}")

        record = summarise(raw)
        batch = None
        if self._quarantine is not None:
            batch = self._file_locally(uid, raw, record, name)

        self._run("store", f"{uid}", "+FLAGS", "(\\Deleted)")
        self._run("expunge")

        return {
            "deleted": 1,
            "uid": str(uid),
            "mailbox": name,
            "subject": record.get("subject", ""),
            "from": record.get("from", ""),
            "quarantine_batch": batch,
            "recoverable": batch is not None,
            "note": (
                "the message is gone from the mail store; a copy is filed in the "
                "assistant's quarantine"
                if batch else
                "the message is gone from the mail store and no local copy was kept"
            ),
        }

    # -- internals ----------------------------------------------------------

    def _file_locally(self, uid: str, raw: bytes, record: dict[str, Any],
                      mailbox: str) -> str | None:
        """Write the message and a manifest entry. Returns the batch name."""
        assert self._quarantine is not None
        batch = "mail-" + time.strftime("%Y%m%d-%H%M%S")
        dest = self._quarantine / batch
        try:
            dest.mkdir(parents=True, exist_ok=True)
            flat = f"{int(uid):08d}.eml" if str(uid).isdigit() else f"{uid}.eml"
            (dest / flat).write_bytes(raw)
            manifest = dest / "manifest.json"
            existing = []
            if manifest.is_file():
                try:
                    existing = json.loads(manifest.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    existing = []
            existing.append({
                "kind": "mail", "uid": str(uid), "mailbox": mailbox,
                "filed_as": flat, "at": time.time(),
                "subject": record.get("subject", ""),
                "from": record.get("from", ""), "message_id": record.get("message_id", ""),
            })
            manifest.write_text(json.dumps(existing, indent=2), encoding="utf-8")
            return batch
        except OSError as exc:
            # Not fatal to the delete, but the caller must know the copy was not
            # kept, because `recoverable` depends on it and a wrong `recoverable`
            # is worse than an error.
            self.last_error = f"quarantine write failed: {exc}"
            return None

    def _headers(self, uid: str, mailbox: str) -> dict[str, Any] | None:
        """Headers for one UID, read without the body."""
        raw = self._fetch_raw(uid, want_body=False)
        if raw is None:
            return None
        record = summarise(raw, body_cap=0)
        record.pop("body", None)
        record.update({"uid": str(uid), "mailbox": mailbox, "preview": True})
        return record

    def _fetch_raw(self, uid: str, *, want_body: bool = True) -> bytes | None:
        want = "(RFC822)" if want_body else "(BODY.PEEK[HEADER])"
        _, data = self._run("fetch", str(uid), want)
        for item in data or []:
            if isinstance(item, tuple) and len(item) >= 2 and item[1]:
                return item[1]
        return None

    @staticmethod
    def _uids(fetched: Any) -> list[str]:
        """UIDs out of a FETCH response, newest last.

        The response interleaves literals with the messages that reference them, so
        this walks it rather than indexing. An empty result is legitimate — an
        empty mailbox answers with no tuples at all.
        """
        uids: list[str] = []
        for item in fetched or []:
            if not isinstance(item, tuple):
                continue
            head = _text(item[0])
            marker = head.rfind("UID ")
            if marker != -1:
                digits = ""
                for ch in head[marker + 4:]:
                    if ch.isdigit():
                        digits += ch
                    else:
                        break
                if digits:
                    uids.append(digits)
        return uids

    def _drafts_mailbox(self) -> str:
        """Find a Drafts folder, falling back to a conventional name.

        Folder names are localised and vendor-specific ("Drafts", "Brouillons",
        "Entwürfe", "[Gmail]/Drafts"), so this asks the server rather than
        guessing — and says which one it chose, so a user looking at their own mail
        client is not left wondering.
        """
        try:
            _, listing = self._run("list", '""', "*")
            for item in listing or []:
                line = _text(item)
                if '"' not in line:
                    continue
                names.append(line.rsplit('"', 2)[-2])
            for candidate in names:
                if candidate.strip().lower() in ("drafts", "draft"):
                    return candidate
            for candidate in names:
                if "draft" in candidate.lower() or "entw" in candidate.lower():
                    return candidate
        except Exception:  # noqa: BLE001 - a server that will not list still has INBOX
            pass
        return "Drafts"

    def close(self) -> None:
        self._close()

    def status(self) -> dict[str, Any]:
        return {
            "configured": True,
            "connected": self._conn is not None,
            "connects": self.connects,
            "last_error": self.last_error,
            **self.config.describe(),
        }


def _imap_connect(config: EmailConfig) -> Any:
    """Open *and authenticate* one IMAP session.

    The login is here, not left to the first command, and that is not a style
    choice. Every IMAP server requires authentication before SELECT, so a client
    that connects and then issues SELECT is talking to a server in the
    `AUTH`/`NONAUTH` state that will answer `SEARCH illegal in state AUTH`.

    It was exactly that bug, and it survived every fake-based test, because each
    fake's `connect` did its own login — the seam was hiding the fact that
    production never authenticated at all. Found by pointing the real client at a
    real server: `tests/test_email_live.py`.

    `ssl=False` is plaintext. It exists because some corporate servers only offer
    STARTTLS, and it is a real downgrade, so the caller has to name it in the
    config rather than get it by omitting a field.
    """
    if config.ssl:
        conn: Any = imaplib.IMAP4_SSL(config.host, config.port,
                                      timeout=CONNECT_TIMEOUT_S)
    else:
        conn = imaplib.IMAP4(config.host, config.port, timeout=CONNECT_TIMEOUT_S)
        try:
            conn.starttls()
        except Exception as exc:  # noqa: BLE001
            raise MailError(
                f"{config.host}:{config.port} refused STARTTLS ({exc}); refusing "
                "to send the password in the clear. Set email_ssl to true."
            ) from None
    _imap_login(conn, config)
    return conn


def _imap_login(conn: Any, config: EmailConfig) -> None:
    """Authenticate, turning a rejection into `MailAuthError`.

    Separate from `_imap_connect` so the test server's connector can reuse it,
    which is what keeps the live tests honest: they authenticate through the same
    code path production does rather than through a private `conn.login`.
    """
    try:
        conn.login(config.username, config.password)
    except imaplib.IMAP4.error as exc:
        raise MailAuthError(
            "the mail server rejected the credential. Check email_username and "
            "the password; most providers want an app-specific password rather "
            "than the account password."
        ) from None


def _smtp_send(config: EmailConfig, msg: Any) -> str:
    """Hand the message to SMTP. The one place mail leaves the machine."""
    import smtplib

    if config.ssl:
        server: Any = smtplib.SMTP_SSL(config.host, 465, timeout=COMMAND_TIMEOUT_S)
    else:
        server = smtplib.SMTP(config.host, 587, timeout=COMMAND_TIMEOUT_S)
        server.starttls()
    try:
        try:
            server.login(config.username, config.password)
        except smtplib.SMTPAuthenticationError as exc:
            raise MailAuthError(
                f"SMTP rejected the credential ({exc}). Most providers require an "
                "app-specific password here, not the account password."
            ) from None
        server.send_message(msg)
        return f"{config.username}@{config.host}"
    finally:
        try:
            server.quit()
        except Exception:  # noqa: BLE001
            pass