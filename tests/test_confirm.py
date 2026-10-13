"""
Tests for D32 (confirmation) and D33 (quarantine delete).

These are the tests I most expected to be wrong, because the whole design assumes
the gap between "shown" and "approved" is the dangerous part. Every test below is
written from the position of an attacker who has already been shown a dialog.

The three independent refusals in op_confirm_approve each get their own test,
because the whole value of having three is that they fail independently. A single
combined test would pass while two of the three were broken.

Note on scope: `file.delete` is the only *destructive* tool with an executor, so it
is the only one that can be driven end to end. The other destructive tools are
covered structurally - they must not reach a confirmation at all, because asking
the user to approve a no-op is a security ritual with no security behind it.

API facts these tests encode (all verified by introspection, not by reading the
implementation, so that a change in the implementation shows up here as a failure
rather than being quietly satisfied):

  * op names are UNDERSORED: `tool_call`, `confirm_approve`, `confirm_deny`,
    `confirm_list`, `session_set_attended`, `workspace_remove`, `audit_verify`.
  * every executor takes a singular `path` string, not a `paths` list.
    `file.delete` -> path. `file.read` -> path, max_bytes. `file.create` -> path,
    content. `file.restore` -> batch.
  * `confirm.approve` returns `{"approved": True, "result": <executor output>}` -
    the executor's dict is NESTED under "result", not spliced in.
  * a refusal is `{"approved": False, "reason": ...}` with `ok: True` at the
    envelope. It is not an exception, because the UI has to render it.
  * `confirm.list` pending entries key on `id` and deliberately omit `args`, which
    is what keeps a secret out of the list view.
"""

from __future__ import annotations

import threading
import time

import pytest

from assistant import daemon as D
from assistant.manifest import Workspace
from assistant.policy import CHANNEL_PC, CHANNEL_TELEGRAM, Tier, ToolSpec


# --------------------------------------------------------------------------- fixtures
@pytest.fixture
def sandbox(tmp_path):
    """A directory the assistant is allowed to touch, plus one it is not."""
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    (allowed / "a.txt").write_text("alpha", encoding="utf-8")
    (allowed / "b.txt").write_text("beta", encoding="utf-8")
    (outside / "precious.txt").write_text("do not touch", encoding="utf-8")
    return allowed, outside


@pytest.fixture
def dg(tmp_path, sandbox):
    """A daemon whose state, quarantine and audit all live in tmp_path."""
    allowed, _ = sandbox
    d = D.Daemon(
        state=tmp_path / "state",
        vault=tmp_path / "vault",
        attended=True,
    )
    d.manifests.add(
        Workspace(name="allowed", root=str(allowed), allow_tiers=(Tier.READ,))
    )
    return d


# ---------------------------------------------------------------------------- helpers
def call(d, op, **args):
    return d.handle(D.ipc.Request(op=op, args=args, id=D.ipc.next_id()))


def ask(d, tool, channel=CHANNEL_PC, **args):
    """Request a tool; return the handle() envelope."""
    return call(d, "tool_call", tool=tool, channel=channel, args=args)


def confirmed(d, tool, **args):
    """Request a tool that must come back needing confirmation."""
    res = ask(d, tool, **args)
    assert res["ok"], res
    assert res["result"].get("needs_confirmation"), res["result"]
    return res["result"]


def approve(d, cid, scope="once", phash=None, **extra):
    """Approve a pending confirmation.

    `phash` is passed by default from the caller, never read from the daemon, so
    that the hash check is actually exercised. Omitting it skips the check
    entirely (`claimed` is falsy), which is itself worth a test.
    """
    kwargs = {"confirmation_id": cid, "scope": scope}
    if phash is not None:
        kwargs["payload_hash"] = phash
    kwargs.update(extra)
    return call(d, "confirm_approve", **kwargs)


def approved(d, cid, scope="once", phash=None, **extra):
    """Approve and require that it actually ran. Returns the executor output."""
    res = approve(d, cid, scope, phash, **extra)
    assert res["ok"], res
    assert res["result"]["approved"] is True, res["result"]
    return res["result"]["result"]


def records(d):
    return list(d.audit.read())


def decisions(d, tool=None):
    out = []
    for r in records(d):
        if tool is None or r.tool == tool:
            out.append((r.tool, r.decision, r.reason))
    return out


def delete_now(d, path, scope="once"):
    """The whole ceremony, for tests about something other than the ceremony.

    Handles both outcomes on purpose. A class grant may have cashed, in which case
    `tool_call` executes immediately and there is no confirmation to approve - the
    executor output comes straight back. Both are correct: that is the entire point
    of the second button. A helper that demanded a confirmation would have hidden
    the one behaviour these tests are actually about.
    """
    res = ask(d, "file.delete", path=str(path))
    assert res["ok"], res
    if res["result"].get("needs_confirmation"):
        c = res["result"]
        return approved(d, c["confirmation_id"], scope, c["payload_hash"])
    return res["result"]


# ------------------------------------------------- 1. the hash binds the payload
def test_a_confirmation_carries_a_payload_hash_and_preview(dg, sandbox):
    allowed, _ = sandbox
    res = confirmed(dg, "file.delete", path=str(allowed / "a.txt"))
    assert len(res["payload_hash"]) == 64  # sha256 hex
    p = res["preview"]
    assert p["action"].startswith("move to quarantine")
    assert p["count"] == 1
    assert str(allowed / "a.txt") in p["resolved"]


def test_a_different_target_is_a_different_hash(dg, sandbox):
    allowed, _ = sandbox
    a = confirmed(dg, "file.delete", path=str(allowed / "a.txt"))["payload_hash"]
    b = confirmed(dg, "file.delete", path=str(allowed / "b.txt"))["payload_hash"]
    assert a != b, "approving one file must not approve a different one"


def test_the_same_call_hashes_the_same_way(dg, sandbox):
    """Stability matters: a hash that changed every call could never be re-derived."""
    allowed, _ = sandbox
    p = str(allowed / "a.txt")
    first = confirmed(dg, "file.delete", path=p)["payload_hash"]
    second = confirmed(dg, "file.delete", path=p)["payload_hash"]
    assert first == second


def test_approving_with_a_stale_hash_is_refused_and_requeues(dg, sandbox):
    """The core of D32: what you saw is bound to what you approve.

    A caller that hands back a hash belonging to a *different* action is the
    simplest version of the attack, and it must not execute "approximately the
    right thing".
    """
    allowed, _ = sandbox
    real = confirmed(dg, "file.delete", path=str(allowed / "a.txt"))
    other = confirmed(dg, "file.delete", path=str(allowed / "b.txt"))

    res = approve(dg, real["confirmation_id"], phash=other["payload_hash"])
    assert res["ok"], res
    assert res["result"]["approved"] is False
    assert "changed" in res["result"]["reason"].lower()
    # a fresh confirmation is offered so the user is asked about the *new* reality
    assert "requeued" in res["result"], res["result"]
    assert res["result"]["requeued"]["payload_hash"] == real["payload_hash"]

    # and nothing happened
    assert (allowed / "a.txt").exists()
    assert (allowed / "b.txt").exists()


def test_a_glob_that_grew_under_the_dialog_is_refused_and_requeues(dg, sandbox):
    """The real-world version: nothing lied, the world moved.

    `*.txt` matched two files when the dialog was drawn. Somebody drops a third
    `.tmp`-adjacent file in before the user clicks. Approving the dialog must not
    quietly widen the set - which is precisely why D32 requires enumerating
    matched files rather than showing a pattern.
    """
    allowed, _ = sandbox
    c = confirmed(dg, "file.delete", path=str(allowed / "*.txt"))
    assert c["preview"]["count"] == 2

    (allowed / "c.txt").write_text("gamma", encoding="utf-8")

    res = approve(dg, c["confirmation_id"], phash=c["payload_hash"])
    assert res["result"]["approved"] is False
    assert (allowed / "a.txt").exists()
    assert (allowed / "c.txt").exists()

    # the requeue is the honest question: "these three, yes?"
    fresh = res["result"]["requeued"]
    assert fresh["preview"]["count"] == 3
    assert fresh["payload_hash"] != c["payload_hash"]


def test_a_requeued_confirmation_can_be_approved_normally(dg, sandbox):
    """Refusing must not deadlock: the user gets a second chance at the new state."""
    allowed, _ = sandbox
    c = confirmed(dg, "file.delete", path=str(allowed / "*.txt"))
    (allowed / "c.txt").write_text("gamma", encoding="utf-8")

    first = approve(dg, c["confirmation_id"], phash=c["payload_hash"])
    fresh = first["result"]["requeued"]
    out = approved(dg, fresh["confirmation_id"], phash=fresh["payload_hash"])
    assert out["deleted"] == 3
    assert not (allowed / "c.txt").exists()


def test_omitting_the_hash_skips_the_binding(dg, sandbox):
    """Documented, and therefore tested: the check is opt-in by omission.

    This is a real weakness in the transport - a caller can skip the binding by
    leaving payload_hash out. It is recorded rather than hidden, because the only
    client is our own overlay, which always sends it. If a second client ever
    appears, this test is the reminder that the binding is opt-in.
    """
    allowed, _ = sandbox
    c = confirmed(dg, "file.delete", path=str(allowed / "a.txt"))
    out = approved(dg, c["confirmation_id"])  # no phash
    assert out["deleted"] == 1


# ------------------------------------- 2. the gate is re-evaluated at approve time
def test_a_revoked_workspace_is_caught_at_approve_time(dg, sandbox):
    """Authority is checked when it is exercised, not trusted from display time."""
    allowed, _ = sandbox
    c = confirmed(dg, "file.delete", path=str(allowed / "a.txt"))
    dg.manifests.remove("allowed")

    res = approve(dg, c["confirmation_id"], phash=c["payload_hash"])
    assert res["ok"], res
    assert res["result"]["approved"] is False
    assert "revok" in res["result"]["reason"].lower()
    assert (allowed / "a.txt").exists()
    assert any("revok" in r[2].lower() for r in decisions(dg))


def test_losing_presence_between_display_and_approval_is_caught(dg, sandbox):
    """A grant the user established by being there is void once they are not."""
    allowed, _ = sandbox
    c = confirmed(dg, "file.delete", path=str(allowed / "a.txt"))
    call(dg, "session_set_attended", attended=False, channel=CHANNEL_PC)

    res = approve(dg, c["confirmation_id"], phash=c["payload_hash"])
    assert res["result"]["approved"] is False
    # The user-facing reason speaks plain English ("you are not at the keyboard");
    # the word "unattended" belongs in the audit record, not on the screen.
    assert "keyboard" in res["result"]["reason"].lower()
    assert any(
        "unattended" in r[2].lower() for r in decisions(dg)
    ), "the audit must record why"
    assert (allowed / "a.txt").exists()


# ------------------------------------------------------------ 3. bad approvals refuse
def test_approving_an_unknown_confirmation_is_refused(dg):
    res = call(dg, "confirm_approve", confirmation_id="c999999", scope="once")
    assert res["ok"] is False
    assert "no pending confirmation" in res["error"]


def test_approving_twice_is_refused_because_the_first_one_worked(dg, sandbox):
    allowed, _ = sandbox
    c = confirmed(dg, "file.delete", path=str(allowed / "a.txt"))
    approved(dg, c["confirmation_id"], phash=c["payload_hash"])
    res = approve(dg, c["confirmation_id"], phash=c["payload_hash"])
    assert res["ok"] is False
    assert "no pending confirmation" in res["error"]


@pytest.mark.parametrize(
    "scope", ["always", "forever", "permanent", "always_allow", "yolo", "*"]
)
def test_there_is_no_always_allow_scope_at_all(dg, sandbox, scope):
    """The most important negative test in the file.

    D32 removed permanent T3 grants. A UI that later grows an "always allow"
    button, or a caller that guesses the scope string, must not be able to mint
    one - and the file must still be there afterwards.
    """
    allowed, _ = sandbox
    c = confirmed(dg, "file.delete", path=str(allowed / "a.txt"))
    res = approve(dg, c["confirmation_id"], scope=scope, phash=c["payload_hash"])
    assert res["ok"] is False, res
    assert "scope" in res["error"].lower()
    assert (allowed / "a.txt").exists(), f"scope {scope!r} executed the delete"


def test_the_daemon_has_no_always_scope_constant():
    """Not merely unused - absent. A constant is a feature waiting to be used."""
    names = [n for n in dir(D.Daemon) if "ALWAYS" in n.upper() or "PERMANENT" in n.upper()]
    assert names == [], f"unexpected scope-blessing constants: {names}"


def test_denying_removes_it_and_is_audited(dg, sandbox):
    allowed, _ = sandbox
    c = confirmed(dg, "file.delete", path=str(allowed / "a.txt"))
    res = call(dg, "confirm_deny", confirmation_id=c["confirmation_id"])
    assert res["ok"], res
    assert res["result"]["was_pending"] is True
    assert (allowed / "a.txt").exists()
    assert any(dc == "denied" for _, dc, _ in decisions(dg))


def test_denying_something_already_denied_reports_false_rather_than_raising(dg, sandbox):
    """A double deny is a UI race, not an error worth taking the daemon down for."""
    allowed, _ = sandbox
    c = confirmed(dg, "file.delete", path=str(allowed / "a.txt"))
    call(dg, "confirm_deny", confirmation_id=c["confirmation_id"])
    res = call(dg, "confirm_deny", confirmation_id=c["confirmation_id"])
    assert res["ok"], res
    assert res["result"]["was_pending"] is False


# ----------------------------------------------------------------- 4. the class grant
def test_a_class_grant_lets_the_next_identical_call_through(dg, sandbox):
    allowed, _ = sandbox
    p = str(allowed / "a.txt")
    delete_now(dg, p, scope="class")

    # the user is deleting this directory's tmp files; a second one should not
    # need a second dialog. The second call must name a file that still EXISTS -
    # asking about an already-quarantined one would prove nothing.
    second = allowed / "b.txt"
    second.write_text("beta2", encoding="utf-8")
    res = ask(dg, "file.delete", path=str(second))
    assert res["ok"], res
    assert res["result"]["deleted"] == 1
    assert not res["result"].get("needs_confirmation")


def test_a_class_grant_does_not_cover_a_different_root(dg, sandbox, tmp_path):
    allowed, outside = sandbox
    other = tmp_path / "other"
    other.mkdir()
    (other / "x.txt").write_text("x", encoding="utf-8")
    dg.manifests.add(
        Workspace(name="other", root=str(other), allow_tiers=(Tier.READ,))
    )

    delete_now(dg, allowed / "a.txt", scope="class")
    res = ask(dg, "file.delete", path=str(other / "x.txt"))
    assert res["result"].get("needs_confirmation"), res["result"]
    assert (other / "x.txt").exists()


def test_a_class_grant_is_not_cashable_over_telegram(dg, sandbox):
    """The grant was established by someone sitting at the keyboard.

    A Telegram message arriving inside the window must still have to be confirmed
    at the PC. If this ever passes, remote channels can spend a local grant.
    """
    allowed, _ = sandbox
    p = str(allowed / "a.txt")
    delete_now(dg, p, scope="class")

    res = ask(dg, "file.delete", channel=CHANNEL_TELEGRAM, path=p)
    assert res["result"].get("needs_confirmation"), res["result"]


def test_a_class_grant_does_not_loosen_t2(dg, sandbox):
    """A delete grant is a delete grant. It must not become a general licence."""
    allowed, _ = sandbox
    delete_now(dg, allowed / "a.txt", scope="class")

    # file.move is T2 with no executor, so asking about it proves nothing: both
    # "the gate said confirm" and "there is no handler" end the same way. Use
    # file.restore, which is T2 *and* executable, so the only way it can succeed
    # is if the gate waved a T2 through on the strength of a T3 grant.
    res = ask(dg, "file.restore", batch="no-such-batch")
    assert res["ok"], res
    assert res["result"].get("needs_confirmation"), res["result"]


def test_a_grant_dies_when_presence_is_lost(dg, sandbox):
    """'I'll be here for the next ten minutes.' If the user is not, the premise is gone."""
    allowed, _ = sandbox
    p = str(allowed / "a.txt")
    delete_now(dg, p, scope="class")
    assert dg._grants, "a class grant should have been recorded"

    call(dg, "session_set_attended", attended=False, channel=CHANNEL_PC)
    assert dg._grants == {}
    assert any("grant" in r[2].lower() for r in decisions(dg))

    (allowed / "b.txt").write_text("beta2", encoding="utf-8")
    res = ask(dg, "file.delete", path=p)
    assert res["result"].get("needs_confirmation"), res["result"]


def test_a_grant_dies_when_a_consequential_action_runs(dg, sandbox):
    """Any T2+ action voids it: the user did something else, and the context moved."""
    allowed, _ = sandbox
    p = str(allowed / "a.txt")
    delete_now(dg, p, scope="class")
    assert dg._grants

    # file.restore is T2, so it needs its own confirmation even though a T3 class
    # grant is live. That is the whole point of this test.
    out = delete_now(dg, allowed / "b.txt", scope="once")
    c = confirmed(dg, "file.restore", batch=out["quarantine_batch"])
    assert c["confirmation_id"].startswith("c")
    approve(dg, c["confirmation_id"], phash=c["payload_hash"], scope="once")
    assert (allowed / "b.txt").exists()
    assert dg._grants == {}, "a T2 action must void outstanding class grants"


def test_a_grant_is_not_recorded_when_the_execution_fails(dg, sandbox, monkeypatch):
    """A failed action must not be usable to obtain a standing permission.

    The grant is written after the executor returns, so a raising executor means
    no grant. Tested by making the executor raise, which is the only honest way to
    produce a failure - a delete of zero files would return successfully.
    """
    allowed, _ = sandbox
    p = str(allowed / "a.txt")

    def boom(self, args):
        raise OSError("disk went away")

    monkeypatch.setitem(D.Daemon._EXECUTORS, "file.delete", boom)
    c = confirmed(dg, "file.delete", path=p)
    res = approve(dg, c["confirmation_id"], scope="class", phash=c["payload_hash"])
    assert res["ok"] is False, res
    assert dg._grants == {}


def test_a_grant_expires_on_its_own(dg, sandbox):
    """Expiry is enforced by comparing against the clock, so the test moves the
    deadline rather than the clock.

    Patching `time.monotonic` would patch it for the entire process - `daemon.time`
    is the stdlib module object - and the arbiter, the lease sweeper and pytest's
    own timing all read it. Expiring one grant must not be able to stall anything
    else, so the test does exactly that: it only moves the one deadline.
    """
    allowed, _ = sandbox
    p = str(allowed / "a.txt")
    delete_now(dg, p, scope="class")
    assert dg._grants, "a class grant should have been recorded"

    for key in list(dg._grants):
        dg._grants[key] = time.monotonic() - 1.0

    assert dg._has_grant("file.delete", {"path": p}) is False
    assert dg._grants == {}, "an expired grant must be pruned, not merely ignored"


# ------------------------------------------------- 5. T3 without an executor never asks
def _an_unrunnable_destructive_tool(dg) -> str:
    """A destructive T3 tool with no executor — **constructed**, not found.

    This example was looked up in the registry, and that was a mistake that cost
    three test runs. It was `package.install`, then `file.empty_recycle_bin`, then
    `email.delete`; each time one of them gained an executor and quietly stopped
    being an example, and the test failed for a reason that had nothing to do with
    what it tests. The last failure said so plainly, and it was right: with
    `email.delete` built, every destructive T3 tool can now run, so a lookup finds
    nothing and the property goes unexercised — not because it is false, but
    because nothing in the registry happens to have it today.

    The property is "a destructive T3 with no executor must fail before the
    dialog". That is a statement about `op_tool_call`, which looks the executor up
    in `Daemon._EXECUTORS` and the tier up in the gate. Registering a spec the
    executor table has never heard of reproduces the condition exactly, and keeps
    reproducing it as the registry grows — which is the difference between a test
    that covers a property and one that covers a coincidence.

    The name is prefixed so it cannot collide with a real tool, and it is
    registered rather than monkeypatched so the gate and `tool_check` see the same
    spec the daemon would.
    """
    from assistant.policy import ToolSpec, Tier
    from assistant.tools import ALL_TOOLS

    name = "test.only.unrunnable_destructive"
    assert name not in ALL_TOOLS, "a real tool took the test's name"
    assert name not in D.Daemon._EXECUTORS, "the test's tool must have no executor"
    dg.gate.register(
        ToolSpec(name, Tier.IRREVERSIBLE, "a destructive tool with no executor",
                 destructive=True, parameters={"path": {"type": "str"}})
    )
    return name


def test_a_destructive_tool_without_an_executor_never_asks(dg):
    """The user must not go through a security ceremony for a no-op.

    Asking the user to approve a destructive T3 that cannot run would be
    theatre: the approval would be recorded, and then nothing would happen. It
    has to fail before the dialog.

    The tool is constructed rather than found - see `_an_unrunnable_destructive_tool`
    for why that changed, and why it is not merely a tidiness point.
    """
    res = ask(dg, _an_unrunnable_destructive_tool(dg))
    assert res["ok"] is False
    assert res.get("not_implemented") or res["result"].get("not_implemented"), res
    assert dg._pending == {}, "nothing may be left pending for a tool that cannot run"
    assert "confirm_list" in D.Daemon._OPS
    listing = call(dg, "confirm_list")
    assert listing["result"]["pending"] == []


def test_the_gap_stays_visible_in_status(dg):
    """A tool that cannot run must still be *registered*, or the gap is invisible."""
    status = call(dg, "status")["result"]
    unrunnable = _an_unrunnable_destructive_tool(dg)
    assert unrunnable not in status["tools_executable"]
    # tools_registered is a count; tools_executable is the list of names.
    assert isinstance(status["tools_registered"], int)
    assert isinstance(status["tools_executable"], list)
    # Every registered tool has an executor now, so the count and the list length
    # are equal and this comparison has stopped carrying information. What still
    # carries it: the gate accepts a registered tool the executor table has never
    # heard of, and that tool reports itself as unexecutable rather than being
    # quietly folded into the executable list.
    assert status["tools_registered"] == len(status["tools_executable"])
    ghost = "test.only.ghost_in_status"
    assert ghost not in D.Daemon._EXECUTORS
    dg.gate.register(ToolSpec(ghost, Tier.IRREVERSIBLE, "registered, never built",
                              destructive=True))
    assert ghost not in call(dg, "status")["result"]["tools_executable"], (
        "a tool with no executor was advertised as executable in status"
    )


# -------------------------------------------------------------------- 6. D33 quarantine
def test_delete_quarantines_rather_than_erasing(dg, sandbox):
    """Hard-deleting would be the wrong implementation for a system whose premise
    is reversibility. The user-visible result is identical; a mistake is a move."""
    allowed, _ = sandbox
    victim = allowed / "a.txt"
    out = delete_now(dg, victim)

    assert out["recoverable"] is True
    assert out["deleted"] == 1
    assert not victim.exists()
    batch_dir = allowed  # noqa: F841 - readability only
    root = out["quarantine_root"]
    kept = list((D.Path(root)).rglob("*.txt")) if hasattr(D, "Path") else None
    assert kept is None or kept, "the bytes must still exist somewhere"


def test_the_quarantined_bytes_are_really_still_there(dg, sandbox):
    from pathlib import Path

    allowed, _ = sandbox
    victim = allowed / "a.txt"
    out = delete_now(dg, victim)
    root = Path(out["quarantine_root"])
    found = [p for p in root.rglob("*") if p.is_file() and p.suffix == ".txt"]
    assert len(found) == 1
    assert found[0].read_text(encoding="utf-8") == "alpha"
    assert (root / "manifest.json").exists()


def test_delete_then_restore_round_trips(dg, sandbox):
    allowed, _ = sandbox
    a, b = allowed / "a.txt", allowed / "b.txt"
    # One call, so both files land in the SAME quarantine batch. Deleting them
    # separately creates two batches, and restoring one obviously cannot bring
    # back the other - that is a fact about quarantine, not a round-trip failure.
    out = delete_now(dg, str(allowed / "*.txt"))
    assert out["deleted"] == 2, out

    c = confirmed(dg, "file.restore", batch=out["quarantine_batch"])
    approve(dg, c["confirmation_id"], phash=c["payload_hash"], scope="once")
    assert a.read_text(encoding="utf-8") == "alpha"
    assert b.read_text(encoding="utf-8") == "beta"


def test_restore_requires_a_real_batch_and_does_nothing_without_one(dg):
    # T2, so it asks first; the failure only becomes visible after approval,
    # and it must be a failure rather than a silent no-op that reports success.
    c = confirmed(dg, "file.restore", batch="no-such-batch")
    res = approve(dg, c["confirmation_id"], phash=c["payload_hash"], scope="once")
    assert res["ok"] is False, res
    # The message names the quarantine batch, not the tool. "no quarantine batch
    # 'no-such-batch'" is more useful to the user than a bare "restore failed".
    assert "quarantine batch" in res["error"].lower()


def test_the_quarantine_cannot_be_used_to_delete_our_own_source(dg):
    """D33 must not become a way around the self-protection deny-path."""
    res = call(
        dg, "tool_call", tool="file.delete", channel=CHANNEL_PC,
        args={"path": str(D.ROOT / "src" / "assistant" / "policy.py")},
    )
    assert res["ok"] is False
    # The message names the protected location and cites D21, which is more
    # useful to a human than the word "denied". Assert on the substance.
    err = res["error"].lower()
    assert "protected" in err and "d21" in err
    assert (D.ROOT / "src" / "assistant" / "policy.py").exists()


# ----------------------------------------------------------------- 7. globs enumerated
def test_a_glob_is_enumerated_not_shown_as_a_pattern(dg, sandbox):
    """A human approving '3 files' and one approving '11,000' are making
    different decisions. Only the first is informed."""
    allowed, _ = sandbox
    for name in ("c.txt", "d.txt"):
        (allowed / name).write_text(name, encoding="utf-8")
    res = confirmed(dg, "file.delete", path=str(allowed / "*.txt"))
    p = res["preview"]
    assert p["count"] == 4
    # `resolved` is a human string, not a list; the enumeration lives in
    # `targets`, one entry per match, each with its own resolved path.
    shown = [t["path"] for t in p["targets"]]
    assert len(shown) == p["count"]
    assert "*.txt" not in " ".join(shown)
    assert all(str(allowed).lower() in s.lower() for s in shown)


def test_a_glob_that_matches_nothing_reports_the_count_rather_than_pretending(dg, sandbox):
    allowed, _ = sandbox
    res = confirmed(dg, "file.delete", path=str(allowed / "*.nope"))
    assert res["preview"]["count"] == 0
    assert res["preview"]["truncated"] is False


def test_an_over_long_preview_is_truncated_but_still_states_the_truth(dg, sandbox):
    """Truncation is allowed; lying about the count is not."""
    allowed, _ = sandbox
    for i in range(210):
        (allowed / f"f{i:04d}.txt").write_text("x", encoding="utf-8")
    res = confirmed(dg, "file.delete", path=str(allowed / "*.txt"))
    p = res["preview"]
    assert p["truncated"] is True
    assert len(p["targets"]) <= D.Daemon._PREVIEW_LIMIT
    # 210 created + the 2 the fixture already made = 212. The count must be the
    # true number, not the number that fitted on screen.
    assert p["count"] == 212, p["count"]
    assert str(212) in p["note"], p["note"]


# ------------------------------------------------------------------------ 8. secrecy
def test_the_displayed_args_are_redacted_but_the_executed_args_are_not(dg, sandbox):
    """The dialog must not display a secret, and the executor must still get it."""
    allowed, _ = sandbox
    c = confirmed(
        dg, "file.delete", path=str(allowed / "a.txt"),
        api_token="hunter2-should-never-be-displayed",
    )
    assert "hunter2" not in repr(c)
    assert "hunter2" not in repr(c["args"])
    # but the raw args the executor will use are intact
    assert dg._pending[c["confirmation_id"]]["_raw_args"]["api_token"].startswith("hunter2")


def test_confirm_list_reports_pending_and_grants(dg, sandbox):
    allowed, _ = sandbox
    c = confirmed(dg, "file.delete", path=str(allowed / "a.txt"))
    out = call(dg, "confirm_list")
    assert out["ok"], out
    r = out["result"]
    assert [p["id"] for p in r["pending"]] == [c["confirmation_id"]]
    assert r["grants"] is not None

    # A second T3 raises a SECOND confirmation; the first one from the top of this
    # test is still pending, so deny it before asserting the queue emptied.
    call(dg, "confirm_deny", confirmation_id=c["confirmation_id"])
    delete_now(dg, allowed / "a.txt", scope="class")
    after = call(dg, "confirm_list")["result"]
    assert after["pending"] == []
    assert after["grants"]


def test_confirm_list_cannot_leak_the_secret(dg, sandbox):
    """The list view deliberately omits args. If that ever changes, this fails."""
    allowed, _ = sandbox
    confirmed(
        dg, "file.delete", path=str(allowed / "a.txt"),
        api_token="hunter2",
    )
    assert "hunter2" not in repr(call(dg, "confirm_list"))


# ----------------------------------------------------- 9. a slow T3 must not stall us
def test_the_executor_runs_outside_the_daemon_lock(dg, sandbox, monkeypatch):
    """The executor does filesystem I/O. It must not block the gate for every
    other caller while it moves bytes - the daemon is 24/7 and multi-threaded."""
    allowed, _ = sandbox
    release = threading.Event()

    def slow(self, args):
        release.wait(timeout=5)
        return {"deleted": 0}

    monkeypatch.setitem(D.Daemon._EXECUTORS, "file.delete", slow)
    c = confirmed(dg, "file.delete", path=str(allowed / "a.txt"))
    worker = threading.Thread(
        target=approve,
        args=(dg, c["confirmation_id"]),
        kwargs={"phash": c["payload_hash"]},
        daemon=True,
    )
    worker.start()
    time.sleep(0.2)
    # an unrelated T0 must complete while the T3 executor is still blocked
    done = threading.Event()

    def unrelated():
        ask(dg, "file.read", path=str(allowed / "b.txt"))
        done.set()

    threading.Thread(target=unrelated, daemon=True).start()
    try:
        assert done.wait(timeout=3), "the gate blocked behind a slow executor"
    finally:
        release.set()
        worker.join(timeout=5)


# ------------------------------------------------------------------------ 10. audit
def test_every_stage_of_a_t3_is_in_the_audit_chain(dg, sandbox):
    allowed, _ = sandbox
    c = confirmed(dg, "file.delete", path=str(allowed / "a.txt"))
    approved(dg, c["confirmation_id"], phash=c["payload_hash"])

    tools = {t for t, _, _ in decisions(dg)}
    assert "file.delete" in tools, "the request itself must be recorded"
    assert any(
        dc == "confirmed" and cid in reason
        for _, dc, reason in decisions(dg)
        for cid in (c["confirmation_id"],)
    )
    assert dg.audit.verify().ok


def test_the_confirmation_is_audited_even_when_it_is_refused(dg, sandbox):
    """A refusal that leaves no trace is indistinguishable from a request that
    never happened - which is exactly the gap an attacker wants."""
    allowed, _ = sandbox
    c = confirmed(dg, "file.delete", path=str(allowed / "a.txt"))
    dg.manifests.remove("allowed")
    approve(dg, c["confirmation_id"], phash=c["payload_hash"])
    assert any(dc == "refused" for _, dc, _ in decisions(dg))
    assert dg.audit.verify().ok


# --------------------------------------------------------------------- 11. constants
def test_the_class_grant_window_is_the_number_the_user_chose(dg):
    assert D.Daemon.CLASS_GRANT_SECONDS == 600.0


def test_the_confirm_ops_exist_under_the_underscored_names(dg):
    for op in ("confirm_approve", "confirm_deny", "confirm_list", "tool_call"):
        assert op in D.Daemon._OPS


# ------------------------------------------------- 5. revoking a class grant


def test_revoking_a_grant_makes_the_next_call_ask_again(dg, sandbox):
    allowed, _ = sandbox
    p = str(allowed / "a.txt")
    delete_now(dg, p, scope="class")

    scopes = [g["scope"] for g in call(dg, "confirm_list")["result"]["grants"]]
    assert scopes, "no grant was recorded to revoke"
    out = call(dg, "confirm_grant_revoke", scope=scopes[0])
    assert out["ok"] is True, out

    res = ask(dg, "file.delete", path=str(allowed / "b.txt"))
    assert res["result"].get("needs_confirmation"), (
        "a revoked grant still cashed - revocation changed nothing")


def test_revoking_an_unknown_scope_is_refused_loudly(dg, sandbox):
    out = call(dg, "confirm_grant_revoke", scope="file.delete|no-such-root")
    assert out["ok"] is False
    assert "scope" in out["error"], (
        f"the refusal does not say what was wrong: {out}")


def test_a_revocation_is_audited(dg, sandbox):
    allowed, _ = sandbox
    delete_now(dg, str(allowed / "a.txt"), scope="class")
    scope = call(dg, "confirm_list")["result"]["grants"][0]["scope"]
    call(dg, "confirm_grant_revoke", scope=scope)
    assert ("confirm.grant_revoke", "allow") in [
        (t, dd) for t, dd, _ in decisions(dg)], (
        "a revocation left no audit record")
