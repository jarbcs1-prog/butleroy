"""The Telegram tools, wired.

Three tools, one bridge. What is checked here is the part a bridge test cannot
see: that the T3 one shows you the words before it delivers them, that the T0 one
reads what has already arrived rather than consuming what has not, and that the T1
one can only ever write to the one file.

`tests/test_telegram.py` owns the bridge and its 47 tests. Nothing here opens a
socket: the transport is a fake, and the only real work is appending a line to a
file in a temporary directory.
"""

import json
import pathlib

import pytest

from assistant import telegram as T
from assistant.daemon import Daemon, DaemonError, Refused


ALLOWED = 199251286
OTHER = 555000111
TOKEN = "123456:AAFakeTokenForTestsOnly"


# ---------------------------------------------------------------- fakes


class FakeHttp:
    """A scripted transport, so no test can reach the network."""

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


def update(text, chat_id=ALLOWED, update_id=1):
    return {
        "update_id": update_id,
        "message": {"message_id": update_id, "chat": {"id": chat_id},
                    "text": text},
    }


@pytest.fixture
def desk(tmp_path):
    """A daemon with no bridge: the state a fresh install is actually in."""
    return Daemon(state=tmp_path / "state", vault=tmp_path / "vault")


@pytest.fixture
def wired(desk):
    """A daemon with a bridge attached, over a fake transport.

    The fake is kept on the daemon rather than read back off the bridge: the
    bridge's transport is `_transport`, and a test that reaches into a private
    attribute to check what was sent is asserting the implementation instead of
    the behaviour.
    """
    desk.http = FakeHttp()
    desk.telegram = T.TelegramBridge(
        lambda op, **a: {"ok": True, "result": {}},
        T.TelegramConfig(TOKEN, (ALLOWED,)),
        transport=desk.http,
    )
    desk.telegram_error = ""
    return desk


def feed(desk, text, chat_id=ALLOWED, update_id=1):
    desk.telegram._handle_update(update(text, chat_id, update_id))


# ---------------------------------------------------------------- registration


def test_all_three_telegram_tools_are_registered():
    """D5: the registry is the whole surface, and a tool with no executor is a
    refusal the model cannot tell apart from a policy decision."""
    for name in ("telegram.poll", "telegram.send_draft", "telegram.send"):
        assert name in Daemon._EXECUTORS, name


def test_the_send_has_a_preview_because_it_is_external_and_irreversible():
    """A T3 external tool with no preview is refused at import by `_check_tables`.
    Asserted here too, because the reason it has one is worth pinning: the words
    are the risk."""
    assert Daemon._PREVIEWS["telegram.send"] == Daemon._preview_telegram_send
    spec = {s.name: s for s in _all_tools()}["telegram.send"]
    assert spec.tier.name == "IRREVERSIBLE"
    assert spec.external


def test_there_is_no_second_door_to_the_send():
    """Registering the tool left `op_telegram_send` in place, and ops are
    tier-free and reachable by name through the pipe.

    So while the registered tool sat behind a preview and a payload-bound
    confirmation, anything holding a pipe handle could send a Telegram message by
    asking for the op instead - no dialog, no hash, no tier. The dialog would have
    been theatre for anyone who read the module.

    `tests/test_voice_ops.py` catches this by asserting the general rule (no
    executable tool has an op); this pins the two names that were the problem, so
    that reintroducing a convenience op fails here with the reason attached.
    """
    by_op = {k.replace("_", ".", 1) for k in Daemon._OPS}
    assert by_op.isdisjoint(Daemon._EXECUTORS)
    assert "telegram_send" not in Daemon._OPS
    assert "telegram_status" not in Daemon._OPS
    # and the delivery logic is still in exactly one place, reached by the tool
    assert hasattr(Daemon, "_telegram_send")
    assert not hasattr(Daemon, "op_telegram_send")


def test_no_op_method_mentions_telegram_at_all():
    """The last way back in would be a fresh `op_` added later.

    The check is on method *names*, not on the source text: the docstring on
    `_telegram_send` has to name `op_telegram_send` in order to explain why it is
    not one, and a grep over `getsource` cannot tell that sentence apart from a
    call. Asserting on names is asserting on the thing that is actually reachable.
    """
    ops = [m for m in dir(Daemon) if m.startswith("op_") and "telegram" in m]
    assert ops == [], f"a telegram op is back: {ops}"


def _all_tools():
    from assistant.tools import ALL_TOOLS

    return ALL_TOOLS


# ---------------------------------------------------------------- the preview


def test_the_preview_is_the_message_itself(wired):
    """For a send, the payload is the risk: a phone number, a password, a
    confident wrong answer the user did not check. None of that is knowable from
    the tool's name, so the dialog has to show it."""
    p = wired._preview_telegram_send({"text": "your disk is nearly full"})
    assert p["message"] == "your disk is nearly full"
    assert p["characters"] == len("your disk is nearly full")
    assert p["reversible"] is False
    assert "cannot be recalled" in p["note"]


def test_two_different_messages_bind_two_different_confirmations(wired):
    """D32: the hash covers the text, so approving a send you were shown cannot
    be replayed as approval of a different send. This is the check that says
    whether D32 has anything to do with words at all."""
    a = wired._payload_hash("telegram.send", {"text": "one"},
                            wired._preview_telegram_send({"text": "one"}))
    b = wired._payload_hash("telegram.send", {"text": "two"},
                            wired._preview_telegram_send({"text": "two"}))
    assert a != b


def test_a_long_preview_says_it_was_cut(wired):
    """A preview that silently truncates is a preview that lies about its own
    length. The cut is flagged and pointed at the staged draft."""
    text = "y" * (wired._TG_PREVIEW_CHARS + 400)
    p = wired._preview_telegram_send({"text": text})
    assert len(p["message"]) == wired._TG_PREVIEW_CHARS
    assert p["preview_truncated"] is True
    assert p["characters"] == len(text), "the true length is what the model needs"
    assert "cut at" in p["note"]


def test_a_preview_survives_a_daemon_with_no_telegram(desk):
    """It must not raise. `_preview_for` has no exception handling and this runs
    while a confirmation is being built, so a preview that raised would take the
    whole dialog down instead of explaining that the send would fail."""
    p = desk._preview_telegram_send({"text": "hello"})
    assert "would_be_refused" in p
    assert "not configured" in p["would_be_refused"]
    assert p["message"] == "hello", "the text is still worth showing"


def test_a_preview_flags_a_send_that_would_be_refused(wired):
    """The dialog is the last place the user can see a mistake, so the three
    refusals the executor will raise are all named in the preview."""
    over = wired._preview_telegram_send({"text": "z" * (T.TELEGRAM_TEXT_MAX + 1)})
    assert str(T.TELEGRAM_TEXT_MAX) in over["would_be_refused"]

    stranger = wired._preview_telegram_send({"text": "hi", "chat_id": 999})
    assert str(ALLOWED) in stranger["would_be_refused"], "the refusal names the allowlist"


# ---------------------------------------------------------------- who it goes to


def test_one_phone_needs_no_chat_id(wired):
    """With exactly one chat on the allowlist the answer is unambiguous, so
    requiring the id would be ceremony on the common case."""
    chat_id, why = wired._telegram_recipient({})
    assert chat_id == ALLOWED and why == ""


def test_several_chats_is_the_users_decision_not_the_assistants(wired):
    """Two chats, no `chat_id`: picking one of the user's own conversations is
    not a choice a T3 tool should make on his behalf."""
    wired.telegram.config.allowed_chat_ids = (ALLOWED, OTHER)
    chat_id, why = wired._telegram_recipient({})
    assert chat_id is None
    assert str(ALLOWED) in why and str(OTHER) in why


def test_an_explicit_allowed_chat_is_taken(wired):
    chat_id, why = wired._telegram_recipient({"chat_id": ALLOWED})
    assert chat_id == ALLOWED and why == ""


def test_a_stranger_is_refused_and_the_refusal_says_who_is_allowed(wired):
    """A refusal a user cannot act on is an obstacle; one that names the allowed
    ids is a correction."""
    chat_id, why = wired._telegram_recipient({"chat_id": 999})
    assert chat_id is None
    assert str(ALLOWED) in why


def test_a_chat_id_that_is_not_a_number_is_refused(wired):
    chat_id, why = wired._telegram_recipient({"chat_id": "my phone"})
    assert chat_id is None
    assert "whole number" in why


def test_no_allowlist_means_nobody_to_send_to(desk):
    """Not "send to anyone" - nobody. An empty allowlist is the state where the
    answer is a refusal, not a broadcast."""
    desk.telegram = T.TelegramBridge(
        lambda op, **a: {}, T.TelegramConfig(TOKEN, (ALLOWED,)), transport=FakeHttp())
    desk.telegram.config.allowed_chat_ids = ()
    chat_id, why = desk._telegram_recipient({})
    assert chat_id is None and why


# ---------------------------------------------------------------- sending


def test_a_send_reaches_the_transport(wired):
    out = wired._exec_telegram_send({"text": "backup finished"})
    assert out["sent"] is True
    assert out["chat_id"] == ALLOWED
    assert wired.http.sent == [{"chat_id": ALLOWED, "text": "backup finished"}]


def test_an_over_long_message_is_refused_rather_than_cut(wired):
    """`say` would truncate silently and the user would receive half a sentence
    with no sign that half was missing. A refusal is one the model can act on."""
    with pytest.raises(DaemonError) as caught:
        wired._exec_telegram_send({"text": "z" * (T.TELEGRAM_TEXT_MAX + 1)})
    assert str(T.TELEGRAM_TEXT_MAX) in str(caught.value)
    assert "send_draft" in str(caught.value), "the refusal must offer the way out"
    assert wired.http.sent == [], "nothing was sent"


def test_an_empty_message_is_refused(wired):
    with pytest.raises(DaemonError):
        wired._exec_telegram_send({"text": "   "})
    assert wired.http.sent == []


def test_sending_without_a_bridge_says_so(desk):
    with pytest.raises(DaemonError) as caught:
        desk._exec_telegram_send({"text": "hi"})
    assert "not configured" in str(caught.value)


def test_a_failed_send_raises_and_never_reports_sent(wired):
    """The dangerous shape is `{"sent": True}` on a transport that failed. The
    op already refuses in that case; this asserts it survives the executor."""
    wired.http.raise_next = RuntimeError("network down")
    with pytest.raises((Refused, DaemonError)) as caught:
        wired._exec_telegram_send({"text": "hi"})
    assert "network down" in str(caught.value)


# ---------------------------------------------------------------- the T0 read


def test_the_read_returns_the_words_that_arrived(wired):
    feed(wired, "how much memory is free", update_id=1)
    out = wired._exec_telegram_poll({})
    assert out["showing"] == 1
    assert out["messages"][0]["text"] == "how much memory is free"
    assert out["configured"] is True


def test_the_read_does_not_consume_anything_not_yet_handled(wired):
    """`_poll_once` is the only thing that advances the cursor. A read that
    polled would delete updates the loop had not handled, and no error would ever
    be raised - data loss wearing a read tool's clothes."""
    feed(wired, "one", update_id=1)
    wired.http.calls.clear()
    before = wired.telegram.offset
    wired._exec_telegram_poll({})
    assert wired.telegram.offset == before
    assert wired.http.calls == [], "the read spoke to the network"


def test_the_read_says_why_it_is_empty(wired):
    """An empty list with no explanation reads as "Telegram is broken". Two very
    different causes - not configured, or nothing has arrived - are both named."""
    fresh = wired._exec_telegram_poll({})
    assert fresh["messages"] == []
    assert fresh["note"], "an empty read must say which empty it is"


def test_the_read_says_so_when_nothing_is_configured(desk):
    out = desk._exec_telegram_poll({})
    assert out["configured"] is False
    assert out["messages"] == []
    assert "telegram" in out["note"].lower()


def test_the_read_limit_is_coerced_not_trusted(wired):
    for i in range(1, 6):
        feed(wired, f"m{i}", update_id=i)
    assert wired._exec_telegram_poll({"limit": 2})["showing"] == 2
    assert wired._exec_telegram_poll({"limit": "nonsense"})["showing"] == 5
    assert wired._exec_telegram_poll({"limit": None})["showing"] == 5


# ---------------------------------------------------------------- the T1 stage


def test_a_draft_is_written_and_nothing_is_sent(wired):
    out = wired._exec_telegram_send_draft({"text": "here is what I found"})
    assert out["staged"] is True
    assert out["sent"] is False
    assert wired.http.sent == [], "staging must not deliver anything"
    rows = [json.loads(line) for line in
            pathlib.Path(out["path"]).read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["text"] == "here is what I found"
    assert rows[-1]["chat_id"] == ALLOWED
    assert rows[-1]["sent"] is False


def test_a_draft_can_only_ever_land_in_one_file(wired):
    """No `path` argument. A T1 write tool that takes a path from the caller is an
    arbitrary-write primitive wearing a messaging name, and the whole value of
    this tool is that it cannot be one."""
    import inspect

    src = inspect.getsource(Daemon._exec_telegram_send_draft)
    assert 'args.get("path")' not in src and "args.get('path')" not in src
    out = wired._exec_telegram_send_draft({"text": "x", "path": "C:/Windows/x.txt"})
    assert pathlib.Path(out["path"]).name == "telegram_drafts.jsonl"
    assert out["path"].endswith("telegram_drafts.jsonl")


def test_drafts_append_and_the_count_is_real(wired):
    wired._exec_telegram_send_draft({"text": "first"})
    out = wired._exec_telegram_send_draft({"text": "second"})
    assert out["staged_total"] == 2
    lines = pathlib.Path(out["path"]).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["text"] == "first", "appending, not overwriting"


def test_an_undecided_draft_is_recorded_rather_than_refused(wired):
    """Staging a message whose recipient is still unknown is exactly the state a
    draft exists for. Refusing would leave nowhere to put it."""
    wired.telegram.config.allowed_chat_ids = (ALLOWED, OTHER)
    out = wired._exec_telegram_send_draft({"text": "who gets this?"})
    assert out["chat_id"] is None
    rows = [json.loads(line) for line in
            pathlib.Path(out["path"]).read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["undecided"], "the ambiguity is written down with it"
    assert rows[-1]["chat_id"] is None


def test_an_empty_draft_is_refused(wired):
    with pytest.raises(DaemonError):
        wired._exec_telegram_send_draft({"text": "  "})
    assert not (wired.state / "telegram_drafts.jsonl").exists()
