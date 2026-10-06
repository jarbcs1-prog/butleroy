"""`email.*`, and the two rules the whole group depends on.

Three layers, in the order they can go wrong:

1. **Configuration and credentials.** The password is never an argument and is
   never in a log. Everything else here is downstream of that, because a mail
   assistant that leaks its own password is worse than one with no mail at all.
2. **The tools against a fake IMAP server.** No network. Every branch is
   reachable: a server that is down, a credential that is rejected, a message that
   is malformed, an expunge that fails after the local copy was written.
3. **The gate.** `folder` is refused as a path for every real mailbox name, so
   the argument is `mailbox`. That is measured in D70 and asserted here against the
   real gate rather than against a copy of the token list, so a change to the path
   screen shows up here instead of in production.

The IMAP fake is a real protocol double rather than a stub: it returns byte literals
in the shape `imaplib` actually produces, because D43 — generator output that is
legal in the generator's own language and unusable by its consumer — has already
cost this project two defects, and a stub that returned clean dicts would have
hidden whether `_uids` parses a real FETCH response.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from assistant import daemon as D
from assistant import email as E
from assistant.ipc import Request, next_id
from assistant.policy import CHANNEL_PC, CHANNEL_TELEGRAM, PolicyGate, ToolSpec, Tier
from assistant.tools import ALL_TOOLS


# --------------------------------------------------------------------------- #
# a real-shaped IMAP double
# --------------------------------------------------------------------------- #


def raw_message(subject="Quarterly numbers", sender="boss@example.com",
                body="The numbers are attached.", to="me@example.com"):
    """A genuine RFC822 byte string, built with the stdlib so it round-trips."""
    import email as stdlib_email

    msg = stdlib_email.message.EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = "Mon, 06 Oct 2026 09:00:00 +0000"
    msg["Message-ID"] = f"<{abs(hash(subject)) % 10**8}@example.com>"
    msg.set_content(body)
    return msg.as_bytes()


class FakeIMAP:
    """An IMAP server with the wire shapes `imaplib` really produces.

    Two details are load-bearing and were wrong in the first version of this fake:

    * a FETCH response interleaves the byte literal *between* parenthesised tuples
      — `[(b'1 (UID 7)', b'raw bytes'), b')']` — so a parser that indexes it gets
      the literal where it expects a message and reads headers out of raw bytes;
    * SEARCH answers with a count, and a message the server does not have simply
      never appears, so "no results" must be a path the code takes rather than an
      exception.
    """

    def __init__(self, *, messages=None, fail=None, reject_auth=False):
        # `messages or {...}` was the first version, and an empty dict is falsy -
        # so `FakeIMAP(messages={})` silently became a mailbox holding one
        # message, and the test asserting that an empty mailbox reads as "no mail"
        # was asserting against a fixture that could not be empty. The same shape
        # as D63: a default that is wrong for exactly the case you construct it
        # for. `is None` is the only check that means what it looks like.
        self.messages = dict(
            {"7": raw_message()} if messages is None else messages
        )
        self.fail = fail
        self.reject_auth = reject_auth
        self.calls = []
        self.selected = None
        self.expunge_count = 0
        self.stored = []
        self.deleted = []
        self.logged_out = False

    # -- imaplib surface ---------------------------------------------------

    def select(self, mailbox="INBOX", readonly=False):
        self.calls.append(("select", mailbox, readonly))
        self.selected = mailbox
        count = len(self.messages)
        return "OK", [str(count).encode()]

    def search(self, charset, *criteria):
        self.calls.append(("search", *criteria))
        if self.fail == "search":
            return "NO", [b"server unavailable"]
        return "OK", [str(len(self.messages)).encode()]

    def fetch(self, uid_set, want):
        self.calls.append(("fetch", uid_set, want))
        if self.fail == "fetch":
            return "NO", [b"connection lost"]
        # Honour the requested set. A fake that answers every FETCH with every
        # message cannot express "no such UID", so a test for reading a missing
        # message would pass against a client that silently returned the wrong
        # mail - which is the exact failure `email.read` exists to prevent.
        wanted = None
        if uid_set and uid_set not in ("1:*", ""):
            wanted = {p.strip('"') for p in uid_set.split(",") if p.strip()}
        out = []
        for uid, raw in self.messages.items():
            if wanted is not None and uid not in wanted:
                continue
            head = f"{uid} (UID {uid})".encode()
            out.append((head, raw))
            out.append(b")")
        return "OK", out

    def store(self, uid_set, command, flags):
        self.calls.append(("store", uid_set, command, flags))
        self.deleted.append(uid_set)
        return "OK", [b"flags updated"]

    def expunge(self):
        self.calls.append(("expunge",))
        self.expunge_count += 1
        if self.fail == "expunge":
            return "NO", [b"expunge failed"]
        return "OK", [b"expunged"]

    def append(self, mailbox, key, flags, raw):
        self.calls.append(("append", mailbox, key))
        self.stored.append((mailbox, key, raw))
        return "OK", [b"appended"]

    def list(self, directory, pattern):
        self.calls.append(("list", directory, pattern))
        return "OK", [b'(\\HasNoChildren) "/" "Drafts"']

    def logout(self):
        self.logged_out = True
        return "BYE", [b"bye"]


@pytest.fixture
def config():
    return E.EmailConfig(host="imap.example.com", username="me@example.com",
                         password="app-specific-secret",
                         password_source=E.PASSWORD_ENV_VAR)


@pytest.fixture
def sent_log():
    """Every message handed to SMTP, and never a real connection.

    `email.send`'s SMTP half has no seam of its own unless one is injected, and the
    first version of this file reached a live DNS lookup for `imap.example.com`
    while trying to test a send. That is the whole argument for the seam being
    part of the constructor rather than a module-level function.
    """
    return []


@pytest.fixture
def box(config, tmp_path, sent_log):
    return E.Mailbox(config, connect=lambda _cfg: FakeIMAP(),
                     smtp_send=lambda cfg, msg: (sent_log.append(msg)
                                                 or f"{cfg.username}@{cfg.host}"),
                     quarantine=tmp_path / "quarantine")


# --------------------------------------------------------------------------- #
# 1. configuration and credentials
# --------------------------------------------------------------------------- #


def write_config(tmp_path, **extra):
    data = {"email_host": "imap.example.com", "email_username": "me@example.com",
            "email_password": "app-specific-secret"}
    data.update(extra)
    path = tmp_path / "email.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_no_email_tool_takes_a_credential_as_an_argument():
    """The whole design, in one assertion.

    A password that arrives as a tool argument is a password that reaches the audit
    chain: append-only, hash-chained, the most-read file in the project. `_redact`
    would catch a key containing "password", but the answer is that the credential
    is never a parameter at all — and this is the test that says so, because the
    property is invisible in every other test.
    """
    secret_words = ("password", "passwd", "secret", "token", "credential", "auth")
    for spec in ALL_TOOLS:
        if not spec.name.startswith("email."):
            continue
        for arg in spec.parameters:
            assert not any(w in arg.lower() for w in secret_words), (
                f"{spec.name} takes {arg!r}; the password must be configuration"
            )


def test_the_password_never_reaches_an_audit_record():
    """Belt and braces against the rule above.

    The rule above says no tool *declares* a credential. This says that even if one
    did, or if a future tool passed the config through, the append-only log would
    not take it. Two independent protections, because "the audit log is append-only"
    and "the audit log contains my password" cannot both be true.
    """
    assert D._redact({"password": "hunter2"})["password"] == "<redacted>"
    assert D._redact({"email_password": "hunter2"})["email_password"] == "<redacted>"
    assert D._redact({"to": "a@b.com"})["to"] == "a@b.com", (
        "redaction must not swallow ordinary arguments, or the audit record becomes "
        "useless for everything else"
    )


def test_the_config_never_prints_its_own_password():
    """`repr` reaches tracebacks and log lines, so it is part of the credential.

    `TelegramConfig` already does this and the reason applies unchanged: an object
    that prints its own secret is a leak discovered by somebody else.
    """
    config = E.EmailConfig(host="imap.example.com", username="me@example.com",
                           password="app-specific-secret")
    assert "app-specific-secret" not in repr(config)
    assert "password hidden" in repr(config)
    blob = json.dumps(config.describe())
    assert "app-specific-secret" not in blob
    # But it says where the password came from, which is the fact a user needs
    # when deciding whether they still have one in plaintext on disk.
    assert "password_source" in config.describe()


def test_the_password_may_come_from_the_environment(tmp_path, monkeypatch):
    path = tmp_path / "email.json"
    path.write_text(json.dumps({
        "email_host": "imap.example.com", "email_username": "me@example.com",
    }), encoding="utf-8")
    monkeypatch.setenv(E.PASSWORD_ENV_VAR, "from-the-environment")
    config = E.load_config(path)
    assert config.password == "from-the-environment"
    assert config.password_source == E.PASSWORD_ENV_VAR


def test_a_missing_configuration_is_refused_not_defaulted(tmp_path):
    """Absence must be distinguishable from a working client with no mail.

    Same rule as `telegram.load_config`: a bridge that starts with nothing and
    quietly ignores everything looks exactly like one that works and nobody is
    writing to it. The first is a bug; the second is a Tuesday.
    """
    with pytest.raises(E.MailNotConfigured):
        E.load_config(tmp_path / "nope.json")


def test_a_missing_file_with_only_a_password_is_refused(tmp_path, monkeypatch):
    """Half a configuration is not a configuration.

    `LOCALAIZED_EMAIL_PASSWORD` carries the secret and nothing else. If the host and
    username could be guessed the result would be a client that looks configured
    and cannot connect, and the user would check the password first — which is the
    one thing that is already correct.
    """
    monkeypatch.setenv(E.PASSWORD_ENV_VAR, "from-the-environment")
    with pytest.raises(E.MailNotConfigured) as exc:
        E.load_config(tmp_path / "nope.json")
    assert E.PASSWORD_ENV_VAR in str(exc.value)


def test_a_cleared_environment_password_falls_back_to_the_file(tmp_path, monkeypatch):
    """An empty variable is a recoverable misconfiguration, not a reason to refuse.

    Refusing here would break a machine that was working an hour ago and whose only
    sin is a variable someone cleared — the least useful possible failure.
    """
    path = write_config(tmp_path)
    monkeypatch.setenv(E.PASSWORD_ENV_VAR, "")
    assert E.load_config(path).password == "app-specific-secret"


def test_an_empty_password_is_refused(tmp_path, monkeypatch):
    monkeypatch.delenv(E.PASSWORD_ENV_VAR, raising=False)
    path = tmp_path / "email.json"
    path.write_text(json.dumps({
        "email_host": "imap.example.com", "email_username": "me@example.com",
        "email_password": "",
    }), encoding="utf-8")
    with pytest.raises(E.MailNotConfigured):
        E.load_config(path)


def test_a_host_that_is_not_a_hostname_is_refused():
    with pytest.raises(E.MailError):
        E.EmailConfig(host="", username="a@b.com", password="x")
    with pytest.raises(E.MailError):
        E.EmailConfig(host="localhost", username="a@b.com", password="x")


# --------------------------------------------------------------------------- #
# 2. the tools, against the fake server
# --------------------------------------------------------------------------- #


def test_a_read_opens_no_connection_until_it_needs_one(config, tmp_path):
    """Constructing a client must not touch the network.

    D1 is the reason: the daemon is meant to be a ~300 MB always-on resident, and a
    module that opens a socket in its constructor makes every start pay for mail it
    was never asked about.
    """
    opened = []

    def connect(_cfg):
        opened.append(1)
        return FakeIMAP()

    box = E.Mailbox(config, connect=connect, quarantine=tmp_path / "q")
    assert opened == [], "constructing a Mailbox opened a connection"
    assert box.status()["connected"] is False
    box.list_mailbox()
    assert opened == [1], "the first read should connect exactly once"


def test_listing_returns_headers_and_counts(box):
    out = box.list_mailbox(limit=10)
    assert out["count"] == 1
    assert out["messages"][0]["subject"] == "Quarterly numbers"
    assert out["messages"][0]["from"] == "boss@example.com"
    # A list must not carry bodies: the full text of twenty messages is a lot of
    # the user's correspondence moving through a text window.
    assert "body" not in out["messages"][0]


def test_an_empty_mailbox_is_an_answer_not_an_error(box, config):
    """"No mail" is the answer to "what is in my inbox", and must read as one."""
    box._connect = lambda _c: FakeIMAP(messages={})
    out = box.list_mailbox()
    assert out["count"] == 0
    assert out["messages"] == []
    assert "no matching messages" in out["note"]


def test_uids_are_parsed_out_of_a_real_fetch_response(box):
    """The interleaved literal shape, asserted directly.

    `imaplib` returns `[(b'7 (UID 7)', b'<raw>'), b')']` — the byte literal sits
    *between* the tuples. Parsing that by index reads raw message bytes as a header
    tuple, which produces a Uid that is a whole email message. This is D43's shape
    and it is worth a test that names it.
    """
    server = FakeIMAP(messages={"7": raw_message(), "9": raw_message(subject="second")})
    box._connect = lambda _c: server
    out = box.list_mailbox()
    assert sorted(m["uid"] for m in out["messages"]) == ["7", "9"]


def test_reading_a_message_that_is_not_there_is_an_error(box):
    with pytest.raises(E.MailError):
        box.read_message(uid="99999")


def test_a_rejected_credential_is_its_own_error(config):
    """Not the same class as a network failure, and not the same fix.

    Conflating them is how a rotated password looks like a flaky network, and the
    user retries the wrong thing for an hour.
    """
    class Rejecting(FakeIMAP):
        def search(self, charset, *criteria):
            return "NO", [b"LOGIN failed: authentication credentials invalid"]

    box = E.Mailbox(config, connect=lambda _c: Rejecting())
    with pytest.raises(E.MailAuthError) as exc:
        box.list_mailbox()
    assert "app-specific" in str(exc.value).lower(), (
        "the message should tell the user what their provider actually wants"
    )


def test_a_dead_connection_is_retried_once_not_reported(box, config):
    """An IMAP server drops idle sessions without warning.

    That is an ordinary event, not an error to report, and a read that works until
    the server restarts is a read that looks broken at exactly the moment the user
    needs it.
    """
    import imaplib

    class FlakyOnce:
        def __init__(self):
            self.calls = 0

        def select(self, *a, **kw):
            self.calls += 1
            if self.calls == 1:
                raise imaplib.IMAP4.abort("server closed the connection")
            return "OK", [b"1"]

        def search(self, *a):
            return "OK", [b"1"]

        def fetch(self, *a):
            return "OK", [(b"7 (UID 7)", raw_message()), b")"]

    flaky = FlakyOnce()
    box._connect = lambda _c: flaky
    out = box.list_mailbox()
    assert out["count"] == 1
    assert flaky.calls >= 2, "the retry never happened"


def test_a_connection_is_not_reused_forever(box):
    """The cache expires, because a held session is a future error on a dead socket."""
    clock = {"t": 1000.0}
    box._clock = lambda: clock["t"]
    box.list_mailbox()
    assert box.connects == 1
    box.list_mailbox()
    assert box.connects == 1, "a fresh connection within the TTL should be reused"
    clock["t"] += E.IDLE_REUSE_S + 1
    box.list_mailbox()
    assert box.connects == 2, "an expired connection must be rebuilt, not reused"


def test_drafting_stores_a_message_and_sends_nothing(box):
    out = box.draft(to="them@example.com", subject="Hello", body="Hi there")
    assert out["drafted"] is True
    assert out["sent"] is False
    assert box._conn.stored, "a draft that stored nothing is not a draft"
    stored = box._conn.stored[-1]
    assert b"Subject: Hello" in stored[2], stored[2][:200]


def test_sending_files_a_copy_and_reports_honestly_when_it_cannot(box, sent_log):
    out = box.send(to="them@example.com", subject="Hello", body="Hi")
    assert out["sent"] is True
    assert out["copied_to_sent"] is True
    assert len(sent_log) == 1, "the message never reached SMTP"

    # The Sent copy is filed *after* the send. A failure to file it must not be
    # reported as a failure to send: the message has already left, and saying
    # otherwise is the one lie the user cannot detect.
    class NoAppend(FakeIMAP):
        def append(self, *a, **kw):
            raise OSError("mailbox is read-only")

    box._close()          # the cached session would otherwise be reused
    box._connect = lambda _c: NoAppend()
    out2 = box.send(to="them@example.com", subject="Hello", body="Hi")
    assert out2["sent"] is True, "a filing failure was reported as a send failure"
    assert out2["copied_to_sent"] is False
    assert "could not be filed" in out2["note"], out2


def test_deleting_files_a_copy_before_it_expunges(box, tmp_path):
    """Order is the whole point, and it is the difference between the two meanings.

    `\\Deleted` + `EXPUNGE` is gone from the server. Fetching first is what makes
    "delete" mean in `email.*` what it means in `file.delete` — recoverable on this
    machine — and it is why the result can honestly say `recoverable: True`.
    """
    out = box.delete(uid="7")
    assert out["deleted"] == 1
    assert out["recoverable"] is True
    batch = tmp_path / "quarantine" / out["quarantine_batch"]
    filed = list(batch.glob("*.eml"))
    assert len(filed) == 1, f"no local copy was kept in {batch}"
    assert b"Quarterly numbers" in filed[0].read_bytes()
    manifest = json.loads((batch / "manifest.json").read_text(encoding="utf-8"))
    assert manifest[0]["uid"] == "7"
    assert manifest[0]["subject"] == "Quarterly numbers"


def test_deleting_says_the_message_is_gone_from_the_server(box):
    """The note must not overstate recoverability.

    A copy exists on this machine. The message is gone from the mail store and a
    mail client elsewhere will no longer see it. Saying only "recoverable" would be
    the D36 shape: a plausible line that is not what happened.
    """
    out = box.delete(uid="7")
    assert "gone from the mail store" in out["note"]
    assert "quarantine" in out["note"]


def test_a_large_body_is_truncated_and_says_so(box):
    """D55's rule: the same number in two units is still a discrepancy to report."""
    big = raw_message(body="x" * 50_000)
    box._connect = lambda _c: FakeIMAP(messages={"7": big})
    record = box.read_message(uid="7")
    assert record["body_truncated"] is True
    assert len(record["body"]) <= E.BODY_CAP


def test_a_malformed_message_is_read_not_fatal(box):
    """One bad email must not make the mailbox unreadable."""
    box._connect = lambda _c: FakeIMAP(messages={"7": b"not a message at all"})
    record = box.read_message(uid="7")
    assert record["subject"] == "", "a malformed message should read as empty, not raise"


# --------------------------------------------------------------------------- #
# 3. the gate: `mailbox`, not `folder`
# --------------------------------------------------------------------------- #


def test_the_argument_is_named_mailbox_not_folder():
    """D70's measured finding, asserted on the registry.

    `policy._check_paths` screens argument keys containing "dir" *and* "folder", so
    every real IMAP mailbox name would be refused as a path outside every approved
    root. The tools use `mailbox`, which contains neither token. Asserted here so a
    rename back to `folder` fails in the suite rather than in production.
    """
    for spec in ALL_TOOLS:
        if not spec.name.startswith("email."):
            continue
        assert "folder" not in spec.parameters, (
            f"{spec.name} declares 'folder', which the path screen refuses for "
            "every real mailbox name (D70)"
        )
        if "mailbox" in spec.parameters:
            assert spec.parameters["mailbox"].get("required") is False, (
                f"{spec.name} should default to the configured mailbox"
            )


def test_every_real_mailbox_name_survives_the_path_screen():
    """The claim behind the naming, against the real gate.

    Four names from four providers, because the argument has to work for the user
    rather than for the one mailbox we happened to test with.
    """
    gate = PolicyGate(tools={s.name: s for s in ALL_TOOLS}, allowed_roots=["G:/approved"])
    gate.allowed_roots = ["G:/approved"]
    for name in ("INBOX", "Sent", "[Gmail]/All Mail", "Archive/2026"):
        for tool in ("email.list", "email.read", "email.search"):
            verdict = gate.evaluate(tool, channel=CHANNEL_PC, args={"mailbox": name})
            assert verdict.decision == "allow", (
                f"{tool} with mailbox={name!r} was refused: {verdict.reason}"
            )


def test_the_refusal_for_folder_is_still_real_so_the_rule_is_not_dead():
    """The screen must still refuse a genuine path argument.

    D63's rule runs both ways: a hole in the rule is still a hole. If `folder`
    stopped being screened *and* nothing else screened paths either, the names in
    this file would pass for the wrong reason — which is exactly what happened to
    two of the ten tests in D68.
    """
    gate = PolicyGate(tools={s.name: s for s in ALL_TOOLS}, allowed_roots=["G:/approved"])
    gate.allowed_roots = ["G:/approved"]
    gate.register(ToolSpec("test.probe", Tier.READ, "probe",
                           parameters={"folder": {"type": "str"}}))
    verdict = gate.evaluate("test.probe", channel=CHANNEL_PC,
                            args={"folder": "INBOX"})
    assert verdict.decision == "deny", (
        "the path screen stopped refusing a path-shaped argument, so the "
        "'folder' finding above is now passing for the wrong reason"
    )


# --------------------------------------------------------------------------- #
# 4. through the daemon: tiers, refusal, and the header-injection guard
# --------------------------------------------------------------------------- #


@pytest.fixture
def dg(tmp_path):
    daemon = D.Daemon(state=tmp_path / "state", attended=True)
    daemon.mail = E.Mailbox(
        E.EmailConfig(host="imap.example.com", username="me@example.com",
                      password="secret"),
        connect=lambda _c: FakeIMAP(), quarantine=tmp_path / "quarantine",
    )
    return daemon


def req(d, op, **args):
    return d.handle(Request(op=op, args=args, id=next_id()))


def test_a_read_is_t0_and_runs(dg):
    out = req(dg, "tool_call", tool="email.list", args={},
              channel=CHANNEL_PC, actor="test")
    assert out["ok"], out
    assert out["result"]["count"] == 1


def test_send_and_delete_always_ask(dg):
    """D13 rule: T3 confirms from every channel and executes nothing."""
    for tool, args in (("email.send", {"to": "a@b.com", "subject": "s", "body": "b"}),
                       ("email.delete", {"uid": "7"})):
        for channel in (CHANNEL_PC, CHANNEL_TELEGRAM):
            out = req(dg, "tool_call", tool=tool, args=args, channel=channel)
            assert out["ok"], out
            body = out["result"]
            assert body.get("needs_confirmation"), (
                f"{tool} from {channel} did not ask: {body}"
            )


def test_a_send_with_a_line_break_in_the_recipient_is_refused(dg):
    """`email.send` is irreversible and external, so this is a real attack.

    A newline in `to` is not a typo: it is a header injection, and `Cc:` in a To:
    header is a Bcc the user never saw. Refused rather than stripped, because
    stripping changes the message that was about to be sent.
    """
    for field in ("to", "cc", "subject"):
        out = req(dg, "tool_call", tool="email.send",
                  args={"to": "a@b.com", "subject": "s", "body": "b",
                        field: "them@example.com\nBcc: spy@evil.test"},
                  channel=CHANNEL_PC)
        assert not out["ok"], f"header injection through {field} was accepted: {out}"
        assert "line break" in out["error"] or "refused" in out["error"].lower()


def test_an_unusable_recipient_is_refused_before_the_dialog(dg):
    out = req(dg, "tool_call", tool="email.send",
              args={"to": "not an address", "subject": "s", "body": "b"},
              channel=CHANNEL_PC)
    assert not out["ok"], out
    assert "usable address" in out["error"], out["error"]


def test_an_unconfigured_daemon_refuses_and_says_which_problem_it_is(tmp_path):
    """Two different problems with two different fixes must not read alike.

    "No mail configured" and "the server rejected the credential" are not the same
    answer, and an executor that returned `None` would push that distinction onto
    every caller to reinvent.
    """
    bare = D.Daemon(state=tmp_path / "state", attended=True)
    assert bare.mail is None
    out = req(bare, "tool_call", tool="email.list", args={}, channel=CHANNEL_PC)
    assert not out["ok"], out
    assert "not configured" in out["error"], out["error"]
    # And the rest of the assistant still works.
    assert req(bare, "tool_call", tool="system.gpu_state", args={},
               channel=CHANNEL_PC)["ok"]


def test_the_send_preview_shows_the_body_in_full(dg):
    """For a send, the payload *is* the risk — the same argument as telegram.send.

    Asserted on the body rather than on "a preview was built", because a preview
    that exists and does not show the words is the D36 failure one layer up.
    """
    body = "Here is the password you asked for: hunter2"
    preview = dg._preview_email_send({"to": "a@b.com", "subject": "creds",
                                      "body": body})
    assert body in preview["note"], preview
    assert preview["to"] == "a@b.com"


def test_the_delete_preview_describes_the_message_not_the_uid(dg):
    """"Delete UID 41" is not something a person can check.

    So the preview fetches the message and shows what it is — which is also a live
    check that the UID resolves before the user approves destroying it.
    """
    preview = dg._preview_email_delete({"uid": "7"})
    assert preview.get("subject") == "Quarterly numbers"
    assert preview.get("from") == "boss@example.com"
    assert "gone from" in preview["note"], preview


def test_a_delete_preview_for_a_missing_uid_still_explains_itself(dg):
    """A preview must never raise, and must never read as though nothing is wrong.

    `_preview_for` has no exception handling and runs while a confirmation is being
    built, so a preview that raised would take the whole confirmation down with it.
    """
    preview = dg._preview_email_delete({"uid": "99999"})
    assert "could not read" in preview["note"], preview
    assert "cancel" in preview["note"], (
        "the user must be told that cancelling is the way out of this"
    )


def test_email_status_reports_configuration_without_the_password(dg):
    out = req(dg, "mail_status")["result"]
    assert out["configured"] is True
    assert "password" not in json.dumps(out).replace("password_source", ""), (
        "status must never contain the password"
    )


def test_mail_status_is_reachable_but_not_an_executor(dg):
    """The D59 rule: a status read is an op, never a registered tool.

    An op is reachable by name through the pipe with no tier check and no preview.
    That is fine for "is this configured" and wrong for anything that acts, which is
    why `email.send` has no op of its own.
    """
    assert "mail_status" in D.Daemon._OPS
    assert "email.send" not in D.Daemon._OPS, (
        "an op for a T3 tool is a second door to it, with no dialog"
    )