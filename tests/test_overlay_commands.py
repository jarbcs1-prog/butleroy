"""Slash commands: a read-only palette in the prompt box. D98.

Text starting with `/` routes to a closed table of read ops instead of
`brain_turn`. Each test below fails before the routing exists: unknown text
either wakes the brain (wrong) or dies silently (worse).
"""

from __future__ import annotations

import pytest

from assistant import overlay as O


class CmdDaemon:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, op, **args):
        self.calls.append((op, args))
        table = {
            "status": {"version": "0.3.0", "uptime_s": 9.0, "attended": True,
                       "session_ceiling": "T3", "requests": 4,
                       "tools_registered": 61, "audit_records": 1},
            "ping": "pong",
            "arbiter_summary": "VRAM 1.0/8.0 GiB used, leases: none",
            "brain_status": {"running": False, "tier": None,
                             "model_loaded": False},
            "brain_describe": {"tools": 61, "tiers": "T0-T1"},
            "model_config": {"tiers": {"fast": {"model": "s.gguf"}},
                             "models": [{"name": "s.gguf"}],
                             "provenance": "file"},
            "voice_config": {"default": "v", "voices": ["v"]},
            "workspace_list": {"workspaces": ["proj"]},
            "workspace_covers": {"name": "proj", "root": "G:/ok",
                                 "danger_level": "low",
                                 "allow_tiers": ["T0", "T1"]},
            "confirm_list": {"pending": [{"id": "c1", "tool": "file.delete",
                                          "reason": "T3"}],
                             "grants": []},
            "tool_check": {"tool": "file.delete", "would": "confirm",
                           "tier": "T3", "reason": "destructive"},
            "audit_tail": [{"timestamp": f"2026-10-07T12:{i:02d}:00",
                            "actor": "user", "tool": f"tool.{i}",
                            "tier": "T1", "decision": "allow"}
                           for i in range(25)],
            "mail_status": {"configured": False},
            "vault_status": {"available": False},
            "reflect_overlay": {"chars": 10},
            "audit_verify": {"ok": True, "checked": 3},
        }
        if op in table:
            return {"ok": True, "result": table[op]}
        raise AssertionError(f"unexpected op {op!r}")

    def ops(self):
        return [op for op, _ in self.calls]


@pytest.fixture
def win():
    def build(daemon):
        ov = O.Overlay(daemon)
        ov._root = object()  # headless: no Tk is ever constructed
        return ov
    return build


def said(ov):
    out = []
    while True:
        try:
            kind, *rest = ov._events.get_nowait()
        except Exception:
            return out
        if kind == "say":
            out.append(str(rest[0]))


def run_cmd(ov, name, argv=None):
    # Mirrors the turn tests: `_submit` holds `_busy` across the worker, so a
    # direct call sets it too, or a `finally` that resets it proves nothing.
    ov._busy = True
    ov._run_command(name, argv or [])
    return said(ov)


def test_routing_is_a_pure_function_with_no_tk():
    assert O._route("/vram") == ("command", "vram", [])
    # Splitting is the router's whole job; rejoining a path with spaces is
    # `covers`'s, so the argv here is words, not the path.
    assert O._route("/covers G:/some dir/x") == ("command", "covers",
                                                 ["G:/some", "dir/x"])
    assert O._route("how much VRAM?") == ("turn", "how much VRAM?")
    assert O._route("/nope") == ("command", "nope", [])
    assert O._route("/") == ("command", "help", [])


def test_slash_status_shows_facts_and_never_wakes_the_brain(win):
    daemon = CmdDaemon()
    out = run_cmd(win(daemon), "status")
    assert any("0.3.0" in line for line in out), out
    assert "brain_turn" not in daemon.ops()


def test_an_unknown_command_says_so_and_lists_help(win):
    daemon = CmdDaemon()
    out = run_cmd(win(daemon), "nope")
    assert any("unknown command" in line for line in out), out
    assert any("/help" in line or "help" in line for line in out), out
    assert "brain_turn" not in daemon.ops()


def test_covers_without_a_path_asks_for_one_and_calls_nothing(win):
    daemon = CmdDaemon()
    out = run_cmd(win(daemon), "covers", [])
    assert "workspace_covers" not in daemon.ops()
    assert any("path" in line for line in out), out


def test_covers_with_a_path_shows_the_verdict(win):
    daemon = CmdDaemon()
    out = run_cmd(win(daemon), "covers", ["G:/ok/file.txt"])
    op, kw = daemon.calls[-1]
    assert op == "workspace_covers" and kw.get("path") == "G:/ok/file.txt"
    assert any("proj" in line for line in out), out


def test_the_tail_is_capped_and_a_bad_limit_is_refused(win):
    daemon = CmdDaemon()
    out = run_cmd(win(daemon), "tail", ["999"])
    assert len(out) <= 21, f"the transcript flooded: {len(out)} lines"
    assert any("truncat" in line for line in out), out
    before = list(daemon.ops())
    out = run_cmd(win(daemon), "tail", ["abc"])
    assert "audit_tail" not in daemon.ops()[len(before):], (
        "a bad limit still asked the daemon"
    )
    assert any("number" in line for line in out), out


def test_commands_never_call_anything_mutating(win):
    daemon = CmdDaemon()
    ov = win(daemon)
    for name in ("help", "status", "ping", "vram", "brain", "describe",
                 "models", "voices", "workspaces", "pending", "check",
                 "tail", "mail", "vault", "reflect", "verify"):
        argv = {"covers": ["G:/ok/x"], "check": ["file.delete"]}.get(name, [])
        run_cmd(ov, name, argv)
    writes = {
        "brain_turn", "brain_stream", "tool_call", "confirm_approve",
        "confirm_deny", "model_config_set", "voice_config_set",
        "voice_install", "voice_import", "voice_remove", "voice_watch",
        "workspace_add", "workspace_remove", "session_set_attended",
        "session_set_ceiling", "audit_note", "vault_append",
        "vault_write_note", "brain_load", "brain_unload",
    }
    called = set(daemon.ops())
    assert not (called & writes), f"a palette command mutated: {called & writes}"
    assert "brain_turn" not in called
