"""Tests for the Plane A daemon wiring.

Framing throughout is adversarial: every test asks what a caller, a bug, or an
attacker would have to get past, and asserts on the daemon's behaviour rather
than on its intent. The daemon is the only component that holds a policy gate, a
set of leases, a signed audit chain and a pointer at the user's notes, all in
one always-on process, so it is the component where "it should be fine" is least
acceptable.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from assistant import daemon as D

REPO_ROOT = Path(__file__).resolve().parent.parent
from assistant.audit import AuditLog
from assistant.ipc import Request, next_id
from assistant.policy import CHANNEL_PC, CHANNEL_SCHEDULED, CHANNEL_TELEGRAM, Tier

PIPE_A = "\\\\.\\pipe\\test.daemon.req"


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """A stand-in vault with the real one's shape: excluded dirs are siblings of
    readable ones, never children, so containment is actually exercised."""
    root = tmp_path / "vault"
    for name in ("evernote", ".opencode", "datasets", ".smart-env", "other"):
        (root / name).mkdir(parents=True)
    (root / "evernote" / "note-one.md").write_text("# one", encoding="utf-8")
    (root / "evernote" / "note-two.md").write_text("# two", encoding="utf-8")
    (root / "datasets" / "corpus.md").write_text("training data", encoding="utf-8")
    (root / ".smart-env" / "state.md").write_text("env state", encoding="utf-8")
    return root


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "README.md").write_text("hello", encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "deep.md").write_text("deep", encoding="utf-8")
    return root


@pytest.fixture
def dg(tmp_path: Path, vault: Path) -> D.Daemon:
    """A daemon with an isolated state dir and a stub vault.

    The arbiter is left as the real one: the two tests that exercise it read
    real nvidia-smi, which is a genuine integration check on this machine and the
    only place the VRAM numbers are ever validated. It is not mocked because a
    mock would only prove the mock works.
    """
    return D.Daemon(state=tmp_path / "state", vault=vault, attended=True)


def req(d: D.Daemon, op: str, **args) -> dict:
    """Go through the real Request type. handle() is what a peer on the named
    pipe calls, so a test that passes a bare dict is testing a different entry
    point than the one in production."""
    return d.handle(Request(op=op, args=args, id=next_id()))


def records(d: D.Daemon) -> list[dict]:
    return [r.to_dict() for r in d.audit.read()]


def tools_called(d: D.Daemon) -> list[str]:
    return [r["tool"] for r in records(d)]


# --------------------------------------------------------------------------- #
# the structural claims in the module docstring
# --------------------------------------------------------------------------- #


def test_no_model_runtime_is_ever_imported():
    """D1: Plane A is ~300 MB and zero VRAM. The cheapest possible proof is that
    importing it does not drag in a CUDA runtime, because a transitive import is
    exactly how a 'small always-on resident' quietly becomes 6 GB.

    This runs in a SUBPROCESS, and the reason is worth recording. The test
    originally asserted `'llama_cpp' not in sys.modules` in-process, which passed
    only because no other test module happened to import it first. Once
    test_brain.py existed and legitimately called `Brain.load()` - which compiles
    a real grammar and therefore imports llama_cpp for real - this test started
    failing while the daemon was, in fact, still clean. A test that reads global
    mutable state and passes by luck is worse than no test, because it will fail
    one day for a reason that has nothing to do with the code under test.
    """
    import subprocess

    src = (
        "import sys; import assistant.daemon; "
        "print('llama_cpp' in sys.modules)"
    )
    out = subprocess.run(
        [sys.executable, "-c", src],
        capture_output=True, text=True, timeout=120,
        cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")},
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False", (
        "importing assistant.daemon pulled in the model runtime; Plane A must "
        f"stay independent of the brain. stdout was: {out.stdout!r}"
    )


def test_ops_and_executors_are_wired_together():
    assert len(D.Daemon._OPS) >= 20, "the daemon lost ops somewhere"
    assert D.Daemon._EXECUTORS, "no executors are wired at all"
    registered = {t.name for t in D.ALL_TOOLS}
    for name in D.Daemon._EXECUTORS:
        assert name in registered, (
            f"{name} has an executor but no registry entry, so it would run "
            "without ever being classified (D5)"
        )


def test_preflight_reports_and_does_not_raise(dg: D.Daemon):
    out = dg.preflight()
    assert out["audit_chain"]["ok"] is True
    assert "allowed_roots" in out
    assert out["no_listening_ports"] is True


# --------------------------------------------------------------------------- #
# fail-closed transport
# --------------------------------------------------------------------------- #


def test_an_unknown_op_is_refused_not_guessed(dg: D.Daemon):
    out = req(dg, "definitely.not.a.real.op")
    assert out["ok"] is False
    assert "refused" in out


def test_handle_never_raises(dg: D.Daemon):
    out = req(dg, "status", nonsense_key=object())
    assert out["ok"] is True


def test_a_handler_bug_becomes_a_failure_not_a_traceback(dg: D.Daemon, monkeypatch):
    """A traceback over a pipe leaks absolute paths to the peer. The message is
    the type and text; the detail goes to the local notes."""
    # _OPS holds direct references captured at import, so patching the class
    # attribute would not affect dispatch. Patch the table itself.
    def boom(self, args):
        raise RuntimeError("boom")

    monkeypatch.setitem(D.Daemon._OPS, "status", boom)
    out = req(dg, "status")
    assert out["ok"] is False
    assert "RuntimeError" in out["error"]
    assert "Traceback" not in out["error"]


# --------------------------------------------------------------------------- #
# gating: D13 through the daemon
# --------------------------------------------------------------------------- #


def test_tool_check_predicts_but_never_acts_and_is_not_audited(dg: D.Daemon, workspace: Path):
    req(dg, "workspace_add", name="proj", root=str(workspace), allow_tiers=["T3"])
    before = len(records(dg))
    out = req(dg, "tool_check", tool="file.delete", channel=CHANNEL_PC,
              args={"path": str(workspace / "README.md")})
    assert out["ok"] is True
    res = out["result"]
    assert res["would"] == "confirm", "a T3 delete must never be predicted as allowed"
    # `executable` is not a constant. It tracked the real executor set, and when
    # file.delete gained an executor (D33 quarantine) it flipped to True. Asserting
    # the old value would have been asserting a bug.
    assert res["executable"] is True, "file.delete has an executor now - D33"
    assert "prediction" in res["note"]
    assert len(records(dg)) == before, "a check that only asks a question is not an event"

    # And the same flag must be False for a tool that genuinely has no handler,
    # which is the case that matters: a check that reports a T3 as executable when
    # it could not run would let the user plan around a capability that is absent.
    #
    # The example is *constructed*, not looked up. It was named twice - then
    # `package.install`, then `file.empty_recycle_bin`, then `email.delete` - and
    # each became false the day the tool it named gained an executor, so the
    # assertion died three times for a reason that had nothing to do with what it
    # tests. A lookup is a coincidence that happens to be true today; registering
    # a spec the executor table has never heard of is the condition itself, and it
    # keeps reproducing as the registry grows.
    from assistant.policy import ToolSpec, Tier
    from assistant.tools import ALL_TOOLS

    unrunnable = "test.only.unrunnable_destructive"
    assert unrunnable not in ALL_TOOLS, "a real tool took the test's name"
    assert unrunnable not in D.Daemon._EXECUTORS
    dg.gate.register(
        ToolSpec(unrunnable, Tier.IRREVERSIBLE, "no executor", destructive=True,
                 parameters={"path": {"type": "str"}})
    )
    out2 = req(dg, "tool_check", tool=unrunnable, channel=CHANNEL_PC)
    # The tier verdict still comes back, and it is still right: a tool that
    # cannot run is not thereby safe. Only `executable` flips.
    assert out2["result"]["would"] == "confirm"
    assert out2["result"]["executable"] is False
    assert len(records(dg)) == before


def test_a_denied_tool_call_is_audited_as_a_denial(dg: D.Daemon, workspace: Path):
    out = req(dg, "tool_call", tool="file.read", channel=CHANNEL_PC,
              args={"path": str(workspace / "README.md")})
    assert out["ok"] is False, "reading outside every approved root must be denied"
    denied = [r for r in records(dg) if r["decision"] == "deny"]
    assert denied, "a refusal that is not recorded is indistinguishable from a bug"


def test_confirm_does_not_execute(dg: D.Daemon, workspace: Path):
    """The load-bearing one. A CONFIRM verdict must never fall through to the
    executor, because the thing being confirmed is a human looking at a specific
    action - which the requester cannot fake."""
    req(dg, "workspace_add", name="proj", root=str(workspace), allow_tiers=["T3"])
    out = req(dg, "tool_call", tool="file.delete", channel=CHANNEL_PC,
              args={"path": str(workspace / "README.md")})
    assert out["ok"] is True
    res = out["result"]
    assert res["needs_confirmation"] is True
    assert (workspace / "README.md").exists(), "a T3 delete executed without confirmation"
    assert "traceback" not in res


def test_a_confirm_payload_redacts_secrets(dg: D.Daemon, workspace: Path):
    req(dg, "workspace_add", name="proj", root=str(workspace), allow_tiers=["T3"])
    out = req(dg, "tool_call", tool="file.delete", channel=CHANNEL_PC,
              args={"path": str(workspace / "README.md"), "api_token": "hunter2"})
    args = out["result"]["args"]
    assert "hunter2" not in str(args), "a secret leaked into a confirm payload"
    assert "api_token" in args, "the key should be marked, not merely dropped"


def test_unattended_queues_a_t2_call_instead_of_running_it(dg: D.Daemon, workspace: Path):
    req(dg, "workspace_add", name="proj", root=str(workspace), allow_tiers=["T2"])
    req(dg, "session_set_attended", attended=False, channel=CHANNEL_PC)
    out = req(dg, "tool_call", tool="file.move", channel=CHANNEL_SCHEDULED,
              args={"src": str(workspace / "README.md"), "dest": str(workspace / "sub")})
    assert out["result"]["queued"] is True
    assert out["result"]["depth"] == 1
    assert (workspace / "README.md").exists(), "an unattended T2 moved a file"
    assert len(dg.queue) == 1


def test_t0_and_t1_still_run_when_unattended(dg: D.Daemon, workspace: Path):
    req(dg, "workspace_add", name="proj", root=str(workspace), allow_tiers=["T1"])
    req(dg, "session_set_attended", attended=False, channel=CHANNEL_PC)
    out = req(dg, "tool_call", tool="file.create", channel=CHANNEL_SCHEDULED,
              args={"path": str(workspace / "new.txt"), "content": "hi"})
    assert out["ok"] is True
    assert (workspace / "new.txt").read_text(encoding="utf-8") == "hi"


def test_a_registered_tool_with_no_executor_says_so_loudly(dg: D.Daemon, workspace: Path):
    """The gap must be visible in `status`, not only when a call trips over it.

    Every registered tool now has an executor, so the original comparison
    (`tools_registered > len(tools_executable)`) has stopped meaning anything — it
    passes while the registry grows and fails the day the last gap is closed, which
    is the wrong way round. The property is "`executable` is derived from the
    executor table, not hardcoded", so a tool the daemon has never heard of is
    registered and reports itself as unexecutable.
    """
    from assistant.policy import ToolSpec, Tier

    req(dg, "workspace_add", name="proj", root=str(workspace), allow_tiers=["T3"])
    # T3 always needs confirmation regardless of channel, so confirmation wins
    # over the missing executor. The gap must still be visible in status.
    status = req(dg, "status")["result"]
    assert "file.list" in status["tools_executable"]
    # `tools_registered` counts the registry and `tools_executable` lists the
    # executor table, so the two are only comparable while a gap exists. Assert the
    # shapes rather than a difference that will eventually be zero.
    assert isinstance(status["tools_registered"], int)
    assert isinstance(status["tools_executable"], list)

    # Now the property itself: a registered tool with no executor must not be
    # advertised as executable. Registered on the gate, absent from _EXECUTORS.
    ghost = "test.only.ghost_tool"
    assert ghost not in D.Daemon._EXECUTORS
    dg.gate.register(ToolSpec(ghost, Tier.READ, "registered but not implemented"))
    out = req(dg, "tool_check", tool=ghost, channel=CHANNEL_PC)
    assert out["ok"], "tool_check refused a registered tool outright"
    assert out["result"]["executable"] is False, (
        "a tool the daemon has no executor for was reported as executable, which "
        "lets the user plan around a capability that is absent"
    )
    # And it is absent from the executable list, so a caller reading `status`
    # cannot be told it can be called.
    assert ghost not in req(dg, "status")["result"]["tools_executable"]


# --------------------------------------------------------------------------- #
# attended state is not a per-request claim
# --------------------------------------------------------------------------- #


def test_telegram_cannot_assert_the_user_is_present(dg: D.Daemon):
    out = req(dg, "session_set_attended", attended=True, channel=CHANNEL_TELEGRAM)
    assert out["ok"] is False
    assert dg.attended is not False, "a remote channel granted itself presence"


def test_going_unattended_is_allowed_and_narrowing_only(dg: D.Daemon):
    out = req(dg, "session_set_attended", attended=False, channel=CHANNEL_PC)
    assert out["result"]["attended"] is False
    back = req(dg, "session_set_attended", attended=True, channel=CHANNEL_PC)
    assert back["result"]["attended"] is True


def test_a_channel_cannot_be_invented(dg: D.Daemon):
    out = req(dg, "status", channel="telepathy")
    assert out["ok"] is False


# --------------------------------------------------------------------------- #
# the session ceiling narrows freely and widens only while watched (D97)


def test_lowering_the_ceiling_is_allowed_from_anywhere(dg: D.Daemon):
    out = req(dg, "session_set_ceiling", ceiling="T1", channel=CHANNEL_TELEGRAM)
    assert out["ok"] is True, out
    assert dg.session_ceiling == Tier.REVERSIBLE_WRITE
    assert out["result"]["changed"] is True


def test_raising_the_ceiling_needs_a_watched_human(dg: D.Daemon):
    req(dg, "session_set_ceiling", ceiling="T1", channel=CHANNEL_PC)
    out = req(dg, "session_set_ceiling", ceiling="T3", channel=CHANNEL_PC)
    assert out["ok"] is True, out
    assert dg.session_ceiling == Tier.IRREVERSIBLE

    req(dg, "session_set_attended", attended=False, channel=CHANNEL_PC)
    narrowed = req(dg, "session_set_ceiling", ceiling="T1", channel=CHANNEL_PC)
    assert narrowed["ok"] is True, narrowed
    refused = req(dg, "session_set_ceiling", ceiling="T3", channel=CHANNEL_PC)
    assert refused["ok"] is False
    assert dg.session_ceiling == Tier.REVERSIBLE_WRITE

    remote = req(dg, "session_set_ceiling", ceiling="T3", channel=CHANNEL_TELEGRAM)
    assert remote["ok"] is False
    assert dg.session_ceiling == Tier.REVERSIBLE_WRITE


def test_an_unknown_ceiling_is_refused_loudly(dg: D.Daemon):
    out = req(dg, "session_set_ceiling", ceiling="T9", channel=CHANNEL_PC)
    assert out["ok"] is False
    assert "T0" in out["error"] and "T3" in out["error"], (
        f"the refusal does not name the valid ceilings: {out}"
    )
    assert dg.session_ceiling == Tier.IRREVERSIBLE


def test_setting_the_same_ceiling_writes_nothing(dg: D.Daemon):
    before = len(records(dg))
    out = req(dg, "session_set_ceiling", ceiling="T3", channel=CHANNEL_PC)
    assert out["ok"] is True
    assert out["result"]["changed"] is False
    assert len(records(dg)) == before, "a no-op wrote to the audit chain"


def test_a_ceiling_change_is_audited(dg: D.Daemon):
    req(dg, "session_set_ceiling", ceiling="T2", channel=CHANNEL_PC)
    mine = [r for r in records(dg) if r.get("tool") == "session.set_ceiling"]
    assert mine, "the ceiling moved with no audit record"
    assert mine[-1]["tier"] == "T1"
    assert "T3" in mine[-1]["reason"] and "T2" in mine[-1]["reason"]


def test_narrowing_the_ceiling_kills_live_grants(dg: D.Daemon):
    import time as _time

    dg._grants["file.delete|test"] = _time.monotonic() + 500.0
    req(dg, "session_set_ceiling", ceiling="T1", channel=CHANNEL_PC)
    assert dg._grants == {}, "a T3 grant outlived T3 eligibility"
    assert any(r.get("tool") == "confirm.grant_clear" for r in records(dg))


# --------------------------------------------------------------------------- #
# manifests are the only source of authority (D10)
# --------------------------------------------------------------------------- #


def test_a_fresh_daemon_authorises_nothing(dg: D.Daemon, workspace: Path):
    out = req(dg, "tool_call", tool="file.read", channel=CHANNEL_PC,
              args={"path": str(workspace / "README.md")})
    assert out["ok"] is False
    assert req(dg, "workspace_list")["result"] == []


def test_adding_a_workspace_takes_effect_immediately(dg: D.Daemon, workspace: Path):
    """allowed_roots is recomputed per call, not cached at construction. If it
    were cached, a workspace granted now would not be usable until restart - and
    worse, one removed would stay usable."""
    req(dg, "workspace_add", name="proj", root=str(workspace), allow_tiers=["T0"])
    out = req(dg, "tool_call", tool="file.read", channel=CHANNEL_PC,
              args={"path": str(workspace / "README.md")})
    assert out["ok"] is True
    assert out["result"]["text"] == "hello"


def test_removing_a_workspace_revokes_access_immediately(dg: D.Daemon, workspace: Path):
    req(dg, "workspace_add", name="proj", root=str(workspace), allow_tiers=["T0"])
    req(dg, "workspace_remove", name="proj")
    out = req(dg, "tool_call", tool="file.read", channel=CHANNEL_PC,
              args={"path": str(workspace / "README.md")})
    assert out["ok"] is False, "a revoked root was still reachable"


def test_workspace_add_audits_the_grant(dg: D.Daemon, workspace: Path):
    req(dg, "workspace_add", name="proj", root=str(workspace), allow_tiers=["T1"])
    grants = [r for r in records(dg) if r["tool"] == "workspace.add"]
    assert grants and "granted access" in grants[-1]["reason"]


def test_an_invalid_tier_name_is_refused(dg: D.Daemon, workspace: Path):
    out = req(dg, "workspace_add", name="proj", root=str(workspace), allow_tiers=["T9"])
    assert out["ok"] is False
    assert req(dg, "workspace_list")["result"] == [], "a bad tier still created a workspace"


# --------------------------------------------------------------------------- #
# the three executors
# --------------------------------------------------------------------------- #


def test_file_list(dg: D.Daemon, workspace: Path):
    req(dg, "workspace_add", name="proj", root=str(workspace), allow_tiers=["T1"])
    out = req(dg, "tool_call", tool="file.list", channel=CHANNEL_PC,
              args={"path": str(workspace)})
    names = {e["name"] for e in out["result"]["entries"]}
    assert {"README.md", "sub"} <= names


def test_file_read_reports_truncation_rather_than_lying(dg: D.Daemon, workspace: Path):
    req(dg, "workspace_add", name="proj", root=str(workspace), allow_tiers=["T1"])
    out = req(dg, "tool_call", tool="file.read", channel=CHANNEL_PC,
              args={"path": str(workspace / "README.md"), "max_bytes": 2})
    assert out["result"]["truncated"] is True
    assert out["result"]["bytes"] == 5


def test_file_create_is_atomic_and_leaves_no_partial(dg: D.Daemon, workspace: Path):
    req(dg, "workspace_add", name="proj", root=str(workspace), allow_tiers=["T1"])
    out = req(dg, "tool_call", tool="file.create", channel=CHANNEL_PC,
              args={"path": str(workspace / "made.txt"), "content": "body"})
    assert out["ok"] is True
    assert (workspace / "made.txt").read_text(encoding="utf-8") == "body"
    assert not (workspace / "made.txt.partial").exists(), "a .partial was left behind"


def test_the_assistant_cannot_write_its_own_source_through_a_workspace(dg: D.Daemon, tmp_path: Path):
    """Even with a manifest pointing at the repo, the self deny-path holds."""
    req(dg, "workspace_add", name="repo", root=str(D.ROOT), allow_tiers=["T3"])
    out = req(dg, "tool_call", tool="file.create", channel=CHANNEL_PC,
              args={"path": str(D.ROOT / "src" / "assistant" / "policy.py"), "content": "x"})
    assert out["ok"] is False


# --------------------------------------------------------------------------- #
# the arbiter, through the daemon
# --------------------------------------------------------------------------- #


def test_arbiter_acquire_and_release_round_trip(dg: D.Daemon):
    from assistant.arbiter import Priority

    out = req(dg, "arbiter_acquire", holder="fast_tier", need_bytes=1_000_000_000,
              priority=Priority.FAST_TIER.name)
    assert out["ok"] is True
    # handle() json-normalises every result, so a Lease arrives as a dict. That
    # is the wire shape a peer actually receives, so assert on that.
    assert out["result"]["holder"] == "fast_tier"
    assert req(dg, "arbiter_release", holder="fast_tier")["result"]["released"] is True


def test_an_impossible_acquire_is_refused_and_audited(dg: D.Daemon):
    out = req(dg, "arbiter_acquire", holder="greedy", need_bytes=999_000_000_000)
    assert out["ok"] is False
    denials = [r for r in records(dg) if r["tool"] == "arbiter.acquire" and r["decision"] == "deny"]
    assert denials, "a VRAM denial must be recorded; it is the log of who was crowded out"


def test_arbiter_summary_is_available(dg: D.Daemon):
    text = req(dg, "arbiter_summary")["result"]
    assert "reserve" in text.lower()


# --------------------------------------------------------------------------- #
# audit
# --------------------------------------------------------------------------- #


def test_a_broken_chain_stops_the_daemon(tmp_path: Path, vault: Path):
    """A chain that does not verify means the record of what was done is
    worthless. A daemon that logs on anyway is worse than one that refuses to
    start, because it looks authoritative."""
    state = tmp_path / "state"
    d = D.Daemon(state=state, vault=vault)
    for i in range(4):
        d._audit(tool=f"test.event{i}", decision="allow", tier="T0", channel=CHANNEL_PC)
    path = state / "audit.jsonl"
    raw = path.read_text(encoding="utf-8").splitlines()
    assert len(raw) == 4
    # Rewrite record 2's payload but keep its hash, so the chain is intact in
    # shape and broken in substance - the case a daemon must refuse.
    import json as _json

    tampered = _json.loads(raw[2])
    tampered["reason"] = "quietly rewritten"
    raw[2] = _json.dumps(tampered, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(raw) + "\n", encoding="utf-8")

    again = D.Daemon(state=state, vault=vault)
    with pytest.raises(D.DaemonError, match="audit chain does not verify"):
        again.preflight()


def test_the_audit_chain_verifies_after_a_burst(dg: D.Daemon, workspace: Path):
    # workspace_covers is a read-only lookup and is deliberately not audited, so
    # the burst is driven by real events: denied reads outside the approved root.
    for i in range(20):
        req(dg, "tool_call", tool="file.read", channel=CHANNEL_PC,
            args={"path": str(workspace / f"f{i}")})
    out = req(dg, "audit_verify")["result"]
    assert out["ok"] is True
    assert out["checked"] >= 20


def test_status_exposes_the_whole_surface(dg: D.Daemon):
    s = req(dg, "status")["result"]
    for key in ("version", "uptime_s", "requests", "attended", "tools_registered",
                "tools_executable", "workspaces"):
        assert key in s, f"status is missing {key}"


# --------------------------------------------------------------------------- #
# the vault (D14)
# --------------------------------------------------------------------------- #


def test_vault_status_reports_the_exclusion_boundary(dg: D.Daemon):
    res = req(dg, "vault_status")["result"]
    assert res["available"] is True
    assert "datasets" in res["describe"]


def test_vault_index_never_returns_an_excluded_note(dg: D.Daemon):
    index = req(dg, "vault_index")["result"]
    rels = [n["rel"] for n in index["notes"]]
    assert index["total"] == 2, "the fixture has exactly two readable notes"
    assert all(r.startswith("evernote") for r in rels)
    assert not any("datasets" in r or ".smart-env" in r for r in rels), (
        "the excluded corpus reached the index; everything downstream trusts it"
    )


def test_a_missing_vault_does_not_stop_the_daemon(tmp_path: Path):
    # Point at a drive that does not exist. A merely absent *directory* is not a
    # test of this: the bridge creates its own layout on construction, so a
    # wrong-but-writable path is created rather than reported missing, which is
    # itself the behaviour worth knowing about.
    d = D.Daemon(state=tmp_path / "state", vault="Z:\\no-such-drive\\vault")
    assert d.vault is None, "an unusable vault must not be silently accepted"
    assert d.vault_error
    assert req(d, "status")["ok"] is True, "the live tier must not depend on the notes"
    assert req(d, "vault_status")["result"]["available"] is False


# --------------------------------------------------------------------------- #
# the daemon is auditable by eye
# --------------------------------------------------------------------------- #


def test_every_decision_is_recorded_with_a_reason(dg: D.Daemon, workspace: Path):
    req(dg, "workspace_add", name="proj", root=str(workspace), allow_tiers=["T0"])
    req(dg, "tool_call", tool="file.read", channel=CHANNEL_PC,
        args={"path": str(workspace / "README.md")})
    for r in records(dg):
        if r["tool"].startswith("file.") or r["tool"] == "workspace.add":
            assert r["reason"], f"a {r['tool']} decision was recorded with no reason"
