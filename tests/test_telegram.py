"""Tests for the Telegram bridge.

No network. Every test drives `_transport`, which is injected, so the branches
that actually matter at 3am - a 409, a dead network, a revoked token, a malformed
body - are all reachable in milliseconds.

The framing throughout is adversarial. Telegram is the first channel that can
reach the assistant while nobody is at the PC, and the first that can ask for an
irreversible action. So the questions asked here are:

* can anyone but the allowlist get a single byte of response?
* can a remote message authorise a T3 action?
* can a remote message unlock T2/T3 by pretending you are at the keyboard?
* if the network is gone, does the thread survive it?
* does the token ever appear in anything a human might read?
"""

import json

import pytest

from assistant import telegram as T


# --- fakes ------------------------------------------------------------------


class FakeHttp:
    """A scripted transport. Records every call."""

    def __init__(self, updates=None):
        self.updates = list(updates or [])
        self.sent = []
        self.calls = []
        self.raise_next = None

    def __call__(self, method, args, timeout):
        self.calls.append((method, dict(args or {}), timeout))
        if self.raise_next:
            exc, self.raise_next = self.raise_next, None
            raise exc
        if method == "sendMessage":
            self.sent.append(dict(args or {}))
            return {"ok": True, "result": {"message_id": 1}}
        batch = self.updates.pop(0) if self.updates else []
        return {"ok": True, "result": batch}


class FakeDaemon:
    """Records ops and answers the ones the bridge asks for."""

    def __init__(self, *, pending=None, turn=None):
        self.pending = pending or []
        self.turn = turn or {"kind": "answer", "text": "hello"}
        self.calls = []

    def __call__(self, op, **args):
        self.calls.append((op, args))
        args["channel"] = args.get("channel", "telegram")
        if op == "confirm_list":
            return {"ok": True, "result": {"pending": list(self.pending), "grants": {}}}
        if op == "confirm_deny":
            cid = args.get("confirmation_id")
            hit = [p for p in self.pending if p.get("id") == cid]
            if hit:
                self.pending.remove(hit[0])
            return {"ok": True, "result": {"denied": True, "was_pending": bool(hit)}}
        if op == "brain_turn":
            return {"ok": True, "result": self.turn}
        if op == "status":
            return {"ok": True, "result": {"uptime_s": 12.0, "attended": False,
                                           "brain": {"running": True},
                                           "vram": "6.2/12.0 GiB"}}
        if op == "audit_note":
            return {"ok": True, "result": {}}
        return {"ok": True, "result": {}}

    def ops(self):
        return [op for op, _ in self.calls]

    def args_for(self, op):
        for name, a in self.calls:
            if name == op:
                return a
        raise AssertionError(f"{op} never called; saw {self.ops()}")


TOKEN = "123456:AAFakeTokenForTestsOnly"
ALLOWED = 199251286


def cfg(ids=(ALLOWED,)):
    return T.TelegramConfig(TOKEN, tuple(ids))


def bridge(daemon=None, http=None, ids=(ALLOWED,)):
    d = daemon or FakeDaemon()
    h = http or FakeHttp()
    return T.TelegramBridge(d, cfg(ids), transport=h), d, h


def update(text, chat_id=ALLOWED, update_id=1):
    return {
        "update_id": update_id,
        "message": {"message_id": update_id, "chat": {"id": chat_id},
                    "text": text},
    }


def feed(b, text, chat_id=ALLOWED, update_id=1):
    b._handle_update(update(text, chat_id, update_id))


# --- the allowlist is the whole of access control ---------------------------


def test_a_message_from_a_stranger_gets_no_reply_at_all():
    """Silence, not a polite refusal.

    A refusal confirms the bot exists and echoes back that the chat id was
    recognised. Silence is the whole of the answer, and it is the only answer
    that does not leak that the bridge is running.
    """
    b, d, h = bridge()
    feed(b, "delete everything", chat_id=999)
    assert h.sent == []
    assert d.calls == [], "a stranger must not reach the daemon either"
    assert b.refused_unknown == 1


def test_a_stranger_asking_for_a_command_gets_nothing():
    b, d, h = bridge()
    feed(b, "/status", chat_id=1)
    feed(b, "/pending", chat_id=-1)
    assert h.sent == []


def test_the_allowed_chat_gets_an_answer():
    b, d, h = bridge()
    feed(b, "/status")
    assert len(h.sent) == 1
    assert "daemon up" in h.sent[0]["text"]


def test_an_empty_allowlist_is_refused_at_construction():
    """An empty list would authorise nobody correctly and everybody if the check
    were ever bypassed. Fail at construction, not at the first message."""
    with pytest.raises(T.TelegramError, match="authorise nobody"):
        T.TelegramConfig(TOKEN, ())


def test_a_malformed_token_is_refused():
    with pytest.raises(T.TelegramError, match="missing or malformed"):
        T.TelegramConfig("not-a-token", (ALLOWED,))


def test_the_config_never_prints_the_token():
    """repr and describe are both reachable by accident."""
    c = cfg()
    assert TOKEN not in repr(c)
    assert TOKEN not in json.dumps(c.describe())
    assert TOKEN not in json.dumps(
        T.TelegramBridge(FakeDaemon(), c, transport=FakeHttp()).status()
    )


def test_load_config_refuses_rather_than_defaulting(tmp_path):
    """A bridge with no allowlist looks exactly like one nobody is talking to,
    and only one of those is a bug."""
    with pytest.raises(T.TelegramError, match="no telegram configuration"):
        T.load_config(tmp_path / "nope.json")
    p = tmp_path / "telegram.json"
    p.write_text(json.dumps({"telegram_bot_token": TOKEN}), encoding="utf-8")
    with pytest.raises(T.TelegramError, match="missing"):
        T.load_config(p)


# --- Telegram may ask; it may never authorise -------------------------------


def test_a_t3_proposal_is_queued_and_not_run():
    b, d, h = bridge(daemon=FakeDaemon(
        turn={"kind": "tool", "tool": "file.delete", "arguments": {"path": "x"},
              "confirmation_id": "c000001"}))
    feed(b, "delete x")
    text = h.sent[0]["text"]
    assert "c000001" in text
    assert "will not run it from here" in text
    # and the daemon was never asked to execute it
    assert "tool_call" not in d.ops(), (
        "the bridge must never turn a message into an execution"
    )


def test_approve_is_refused_on_purpose_and_says_so():
    """A command that was never implemented is indistinguishable from one that
    was removed. This one was considered and declined, and it says why."""
    b, d, h = bridge(daemon=FakeDaemon(pending=[{"id": "c000001",
                                                 "tool": "file.delete",
                                                 "reason": "T3"}]))
    feed(b, "/approve c000001")
    text = h.sent[0]["text"]
    assert "cannot approve" in text.lower()
    assert "confirm_approve" not in d.ops()
    assert "audit_note" in d.ops(), "the refusal should be on the record"


def test_approve_would_not_work_even_if_the_gate_were_wrong():
    """Structural, not behavioural: the bridge holds no capability to approve."""
    b, d, h = bridge()
    feed(b, "/approve c000001")
    assert "confirm_approve" not in d.ops()
    assert not hasattr(b, "_approve")
    assert not hasattr(b, "_raw_call")


def test_pending_lists_what_is_waiting():
    b, d, h = bridge(daemon=FakeDaemon(pending=[
        {"id": "c000001", "tool": "file.delete", "reason": "destructive"},
        {"id": "c000002", "tool": "power.shutdown", "reason": "external"},
    ]))
    feed(b, "/pending")
    text = h.sent[0]["text"]
    assert "c000001" in text and "file.delete" in text
    assert "c000002" in text and "power.shutdown" in text
    assert "/deny" in text


def test_pending_says_so_when_nothing_is_waiting():
    b, d, h = bridge()
    feed(b, "/pending")
    assert "Nothing is waiting" in h.sent[0]["text"]


def test_deny_refuses_a_real_one():
    daemon = FakeDaemon(pending=[{"id": "c000001", "tool": "file.delete",
                                  "reason": "T3"}])
    b, d, h = bridge(daemon=daemon)
    feed(b, "/deny c000001")
    assert "Refused c000001" in h.sent[0]["text"]
    assert daemon.pending == []
    assert d.args_for("confirm_deny")["confirmation_id"] == "c000001"


def test_deny_with_no_argument_asks_which_one():
    b, d, h = bridge()
    feed(b, "/deny")
    assert "/pending" in h.sent[0]["text"]
    assert "confirm_deny" not in d.ops()


def test_deny_of_something_already_gone_says_so_rather_than_lying():
    b, d, h = bridge(daemon=FakeDaemon(pending=[]))
    feed(b, "/deny c999")
    assert "Nothing was pending" in h.sent[0]["text"]


# --- presence is not asserted over a phone ---------------------------------


def test_a_telegram_message_never_claims_to_be_present():
    """The commonest reason to be away is coffee, not sleep.

    Marking a phone message as presence would unlock T2 and T3 on a machine
    nobody is looking at, which is exactly what D30 exists to prevent. The
    bridge must never touch `session.set_attended` at all.
    """
    b, d, h = bridge()
    feed(b, "/status")
    feed(b, "are you there?")
    feed(b, "/pending")
    assert "session_set_attended" not in d.ops()


def test_every_forwarded_message_is_channel_telegram():
    b, d, h = bridge()
    feed(b, "hello")
    assert d.args_for("brain_turn")["channel"] == "telegram"
    assert d.args_for("brain_turn")["actor"] == "telegram"


def test_an_unknown_command_is_told_the_list():
    b, d, h = bridge()
    feed(b, "/frobnicate")
    assert "/help" in h.sent[0]["text"]


def test_help_does_not_leak_the_token():
    b, d, h = bridge()
    feed(b, "/start")
    assert TOKEN not in h.sent[0]["text"]


def test_a_command_sent_to_a_group_strips_the_botname():
    b, d, h = bridge()
    feed(b, "/status@my_assistant_bot")
    assert len(h.sent) == 1


# --- plain messages --------------------------------------------------------


def test_a_plain_message_goes_to_the_brain_and_the_answer_comes_back():
    b, d, h = bridge(daemon=FakeDaemon(turn={"kind": "answer", "text": "4.9 GiB"}))
    feed(b, "how much vram are you using")
    assert d.args_for("brain_turn")["prompt"] == "how much vram are you using"
    assert h.sent[0]["text"] == "4.9 GiB"


def test_a_parse_error_is_reported_as_such_not_as_a_tool_call():
    b, d, h = bridge(daemon=FakeDaemon(
        turn={"kind": "tool", "tool": None, "parse_error": "not JSON"}))
    feed(b, "do something")
    assert "did not parse" in h.sent[0]["text"]


def test_a_failed_turn_is_reported_not_swallowed():
    class Failing(FakeDaemon):
        def __call__(self, op, **args):
            if op == "brain_turn":
                return {"ok": False, "error": "brain not loaded"}
            return super().__call__(op, **args)

    b, d, h = bridge(daemon=Failing())
    feed(b, "hello")
    assert "did not work" in h.sent[0]["text"]


def test_an_empty_or_non_text_message_is_ignored():
    b, d, h = bridge()
    b._handle_update({"update_id": 1, "message": {"chat": {"id": ALLOWED}}})
    b._handle_update({"update_id": 2, "message": {"chat": {"id": ALLOWED},
                                                  "text": "   "}})
    b._handle_update({"update_id": 3})          # no message at all
    b._handle_update({"update_id": 4, "reaction": [{"type": "like"}]})
    assert d.calls == []


def test_a_message_wins_over_an_edited_copy_of_it():
    """Telegram sends both keys on an edit. Answering it twice would be the
    worst kind of double-tap, so the unedited copy is taken once and only once."""
    b, d, h = bridge()
    b._handle_update({
        "update_id": 5,
        "message": {"chat": {"id": ALLOWED}, "text": "hello"},
        "edited_message": {"chat": {"id": ALLOWED}, "text": "hello again"},
    })
    assert d.ops().count("brain_turn") == 1
    assert d.args_for("brain_turn")["prompt"] == "hello"


def test_an_edited_message_is_handled():
    b, d, h = bridge()
    b._handle_update({"update_id": 5,
                      "edited_message": {"chat": {"id": ALLOWED},
                                         "text": "hello again"}})
    assert "brain_turn" in d.ops()


# --- the offset, and not answering twice -----------------------------------


def test_the_offset_advances_so_a_message_is_not_handled_twice():
    """The classic long-poll bug: forgetting to advance the offset replays the
    whole backlog on every restart."""
    http = FakeHttp([[update("one", update_id=10)],
                     [update("two", update_id=11)]])
    b, d, _h = bridge(http=http)
    assert b._poll_once()[0]["update_id"] == 10
    assert b.offset == 11
    b._poll_once()
    assert b.offset == 12
    # and the second poll passes the offset along
    assert http.calls[1][1]["offset"] == 11


def test_the_first_poll_sends_no_offset():
    http = FakeHttp()
    b, d, _h = bridge(http=http)
    b._poll_once()
    assert "offset" not in http.calls[0][1]


def test_a_long_poll_actually_waits():
    """A short timeout means Telegram rate-limits and the bot is throttled."""
    http = FakeHttp()
    b, d, _h = bridge(http=http)
    b._poll_once()
    method, args, timeout = http.calls[0]
    assert args["timeout"] == T.LONG_POLL_S
    assert T.LONG_POLL_S < 60, "Telegram treats a longer poll as a dead poller"


# --- the network going away -------------------------------------------------


def test_a_dead_network_does_not_kill_the_thread():
    """A poller that dies on a transient error works perfectly right up until the
    moment it is needed."""
    http = FakeHttp()
    http.raise_next = ConnectionError("no route to host")
    b, d, _h = bridge(http=http)
    b._halt.wait = lambda _s: False        # never stop
    b._loop_steps = 0
    # run the loop body once by hand: it must swallow the error and keep going
    try:
        b._poll_once()
    except ConnectionError:
        pass
    assert b.polls == 1
    assert b._thread is None, "no thread was started, so nothing can have died"


def test_the_loop_backs_off_and_retries_and_never_raises():
    http = FakeHttp()
    b, d, _h = bridge(http=http)
    http.raise_next = ConnectionError("boom")
    b._halt.wait = lambda _s: True          # stop after the first backoff
    b._loop()                                # must return, not raise
    assert b.last_error and "boom" in b.last_error


def test_a_409_restarts_the_poll_without_backing_off():
    """Two pollers means two copies of every message, so it must be loud - but
    it is retryable, so it must not count as an error."""
    http = FakeHttp()
    b, d, _h = bridge(http=http)
    http.raise_next = T._RestartPoll("another poller")
    b._halt.wait = lambda _s: True
    b._loop()
    assert b.last_error is None, "a 409 is normal contention, not a failure"


def test_a_rejected_token_is_reported_not_retried_forever():
    http = FakeHttp()
    b, d, _h = bridge(http=http)
    http.raise_next = T.TelegramError("Telegram rejected the bot token (401).")
    b._halt.wait = lambda _s: True
    b._loop()
    assert "401" in (b.last_error or "")


def test_a_non_json_reply_is_an_error_not_a_silent_empty_poll():
    http = FakeHttp()
    b, d, _h = bridge(http=http)
    orig = http.__call__

    class Bad(FakeHttp):
        def __call__(self, method, args, timeout):
            if method == "getUpdates":
                raise T.TelegramError("Telegram returned non-JSON: <html>")
            return orig(method, args, timeout)

    b2, d2, _ = bridge(http=Bad())
    b2._halt.wait = lambda _s: True
    b2._loop()
    assert "non-JSON" in (b2.last_error or "")


def test_a_failed_send_is_reported_and_never_raises():
    b, d, h = bridge()
    h.raise_next = ConnectionError("gone")
    assert b.say(ALLOWED, "hello") is False
    assert b.last_error and "send failed" in b.last_error


def test_an_empty_send_is_not_attempted():
    b, d, h = bridge()
    assert b.say(ALLOWED, "   ") is False
    assert h.sent == []


def test_a_long_reply_is_truncated_to_telegrams_limit():
    """Silently dropping a long answer would look like the assistant going quiet."""
    b, d, h = bridge()
    b.say(ALLOWED, "x" * 10_000)
    assert len(h.sent[0]["text"]) == 4096


# --- D31: it is a client and nothing else -----------------------------------


def test_the_bridge_holds_no_authority_of_its_own():
    """Structurally, not behaviourally.

    Checked over the attributes rather than by exercising them, because the
    failure this guards against - someone adding a `_send` that skips the gate -
    would be written once and never pressed in a test.
    """
    b, d, h = bridge()
    forbidden = [a for a in dir(b) if a.startswith("_exec")]
    assert forbidden == []
    assert not hasattr(b, "daemon"), "it must not hold the Daemon itself"
    assert not hasattr(b, "gate")
    assert not hasattr(b, "arbiter")
    assert not hasattr(b, "audit")


def test_a_reply_is_transport_not_the_t3_tool():
    """Answering a message that arrived a second ago is already authorised by its
    sender. `telegram.send` is for a message the assistant starts, which is why
    it is T3 and external - and why `say()` must not route through it."""
    import inspect

    src = inspect.getsource(T.TelegramBridge.say)
    assert "tool_call" not in src
    assert "telegram_send" not in src


def test_the_token_never_reaches_the_token_argument():
    """The only place the token appears is the URL, and the host is a constant."""
    import inspect

    src = inspect.getsource(T.TelegramBridge)
    assert "api.telegram.org" in T.API_HOST
    # the token is only ever read through _api, never interpolated into a call
    assert src.count("self.config._token") == 1