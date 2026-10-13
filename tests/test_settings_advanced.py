"""Advanced is a diagnostics surface with affordances, not a viewer.

Measured 2026-10-14: `AdvancedSection.build` renders the daemon's beliefs as text
(status, brain, mail, vault, reflection, paths, audit tail) behind a single
`Reload` button. Every figure is read-only, which is correct for diagnostics —
but three read-only questions a user can reasonably ask from this surface have
no way in:

* "is this path approved, and what may happen in it" (`workspace_covers`),
* "what holds VRAM and for how long" (`arbiter_summary`),
* "see what the model was given" (`brain_describe`).

All three ops exist in `daemon.py` and all three only read. Each test below
fails before the affordance exists: the op is never called, so the failure is
the absence, not the rendering.

Deliberately out of scope, with reasons in D95: `brain_load`/`brain_unload`
(change process state; the read-only test in `test_settings_real.py` forbids
them here), `arbiter_sweep`/`acquire`/`release`/`renew` (the sweep kills
expired-lease holders; leases are the daemon's to manage), and `audit_note`
plus the vault writes (writes need their own decisions, not a diagnostics tab).
"""

from __future__ import annotations

import tkinter as tk

from assistant import settings as S


class AdvDaemon:
    """Answers the six existing diagnostics ops plus the three new reads."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.approved = "G:/approved/notes/file.txt"

    def __call__(self, op: str, **kwargs):
        self.calls.append((op, dict(kwargs)))
        if op == "status":
            return {"ok": True, "result": {
                "version": "0.3.0", "uptime_s": 12.5, "attended": True,
                "session_ceiling": "T3", "requests": 3, "tools_registered": 61,
                "audit_records": 2}}
        if op == "brain_status":
            return {"ok": True, "result": {
                "running": False, "tier": None, "model_loaded": False,
                "idle_s": None, "ttl_s": 90.0, "note": "no child is running"}}
        if op == "mail_status":
            return {"ok": True, "result": {"configured": False,
                                           "note": "no state/email.json"}}
        if op == "vault_status":
            return {"ok": True, "result": {"available": False,
                                           "error": "vault not configured"}}
        if op == "reflect_overlay":
            return {"ok": True, "result": {"chars": 0}}
        if op == "audit_tail":
            return {"ok": True, "result": [
                {"timestamp": "2026-10-07T12:00:00.000000+00:00",
                 "actor": "tray", "tool": "voice_config.set",
                 "tier": "T1", "decision": "allow"},
                {"timestamp": "2026-10-07T12:01:00.000000+00:00",
                 "actor": "user", "tool": "workspace.add",
                 "tier": "T1", "decision": "allow"},
            ]}
        if op == "workspace_covers":
            if kwargs.get("path", "").startswith("G:/approved"):
                return {"ok": True, "result": {
                    "name": "notes", "root": "G:/approved",
                    "danger_level": "low", "allow_tiers": ["T0", "T1"]}}
            return {"ok": True, "result": None}
        if op == "arbiter_summary":
            return {"ok": True, "result":
                    "VRAM 3.2/8.0 GiB used, 4.8 free, 1.0 reserved | "
                    "leases: brain@FAST_TIER | preemptions 0, expiries 2, "
                    "denials 0"}
        if op == "brain_describe":
            return {"ok": True, "result": {
                "tools": 61, "tiers": ["T0", "T1"],
                "note": "the model sees tools, tiers and workspaces"}}
        raise AssertionError(f"unexpected op {op!r}")


import pytest  # noqa: E402


@pytest.fixture
def win():
    made: list[S.Settings] = []

    def build(daemon):
        w = S.Settings(daemon, title="Butleroy Settings")
        made.append(w)
        w.show()
        return w

    yield build
    for w in made:
        try:
            w.destroy()
        except Exception:  # noqa: BLE001
            pass


def _section(window: S.Settings) -> object:
    for section in window._sections.values():  # noqa: SLF001 - white-box
        if type(section).__name__ == "AdvancedSection":
            return section
    raise AssertionError("no AdvancedSection on the window")


def _pump(widget) -> None:
    widget.winfo_toplevel().update_idletasks()


def _walk(section: object) -> list:
    """Every widget under the section's tops, structurally, not by address."""
    found: list = []

    def visit(widget) -> None:
        found.append(widget)
        for child in widget.winfo_children():
            visit(child)

    for top in getattr(section, "widgets", []):
        visit(top)
    return found


def _labels(section: object) -> list[str]:
    return [w.cget("text") for w in _walk(section)
            if type(w).__name__ == "Label"]


def _button(section: object, text: str):
    for w in _walk(section):
        if type(w).__name__ == "Button" and w.cget("text") == text:
            return w
    raise AssertionError(
        f"no {text!r} button on the Advanced tab; "
        f"buttons present: {[w.cget('text') for w in _walk(section) if type(w).__name__ == 'Button']}"
    )


def _field(section: object):
    for w in _walk(section):
        if type(w).__name__ == "Field":
            return w
    raise AssertionError("no path field on the Advanced tab")


def test_advanced_checks_whether_a_path_is_approved(win) -> None:
    """The path checker: a field, a button, and the daemon's verdict on screen."""
    daemon = AdvDaemon()
    window = win(daemon)
    window._select("advanced")  # noqa: SLF001
    section = _section(window)

    field = _field(section)
    check = _button(section, "Check path")

    field.set(daemon.approved)
    check.invoke()
    _pump(check)
    assert ("workspace_covers", {"path": daemon.approved}) in daemon.calls, (
        "checking never asked the daemon; "
        f"ops called: {[op for op, _ in daemon.calls]}"
    )
    assert any("notes" in text for text in _labels(section)), (
        f"the approved workspace was not shown: {_labels(section)}"
    )

    field.set("G:/elsewhere/file.txt")
    check.invoke()
    _pump(check)
    assert any("outside every approved workspace" in text
               for text in _labels(section)), (
        f"an unapproved path was not refused on screen: {_labels(section)}"
    )


def test_advanced_shows_what_holds_vram(win) -> None:
    """`status`'s vram figure never renders with a real daemon.

    `op_status` returns `vram` as a one-line *string* (`arbiter.summary()`),
    but the tab only renders it `if isinstance(vram, dict)` — so the holders
    line is silently absent exactly when the daemon is real. The tab asks
    `arbiter_summary` itself and shows the answer.
    """
    daemon = AdvDaemon()
    window = win(daemon)
    window._select("advanced")  # noqa: SLF001
    section = _section(window)
    _pump(section.widgets[0])

    assert any(op == "arbiter_summary" for op, _ in daemon.calls), (
        "the tab never asked what holds VRAM"
    )
    assert any("leases: brain@FAST_TIER" in text
               for text in _labels(section)), (
        f"the VRAM holders are not shown: {_labels(section)}"
    )


def test_advanced_shows_what_the_model_was_given(win) -> None:
    daemon = AdvDaemon()
    window = win(daemon)
    window._select("advanced")  # noqa: SLF001
    section = _section(window)

    _button(section, "What the model was given").invoke()
    _pump(section.widgets[0])

    assert any(op == "brain_describe" for op, _ in daemon.calls), (
        "the tab never asked what the model was given"
    )
    assert any("the model sees" in text for text in _labels(section)), (
        f"the description was not shown: {_labels(section)}"
    )


def test_the_new_affordances_change_nothing(win) -> None:
    """Reads, asserted on the ops — a disabled widget would still look right."""
    daemon = AdvDaemon()
    window = win(daemon)
    window._select("advanced")  # noqa: SLF001
    section = _section(window)

    _field(section).set(daemon.approved)
    _button(section, "Check path").invoke()
    _button(section, "What the model was given").invoke()
    field, button = _tail_filter_widgets(section)
    field.set("workspace")
    button.invoke()
    _pump(section.widgets[0])

    writes = {
        "voice_config_set", "voice_install", "voice_import", "voice_remove",
        "model_config_set", "workspace_add", "workspace_remove",
        "session_set_attended", "tool_call", "confirm_approve", "confirm_deny",
        "audit_note", "brain_load", "brain_unload", "brain_turn", "brain_stream",
        "arbiter_acquire", "arbiter_release", "arbiter_renew", "arbiter_sweep",
        "vault_append", "vault_write_note", "vault_index",
    }
    called = {op for op, _ in daemon.calls}
    assert not (called & writes), (
        f"the diagnostics tab called a mutating op: {sorted(called & writes)}"
    )


def test_advanced_content_scrolls_instead_of_clipping(win) -> None:
    """Facts, three panels and the audit tail do not fit 600 px.

    They did not fit before the panels either — the tail was already below
    the fold. So the tab scrolls in a viewport; without it the new
    affordances would need a resize to be seen at all.
    """
    daemon = AdvDaemon()
    window = win(daemon)
    window._select("advanced")  # noqa: SLF001
    section = _section(window)

    canvases = [w for w in _walk(section) if type(w).__name__ == "Canvas"]
    assert canvases, "the Advanced tab has no scrolling viewport"
    view = canvases[0]
    assert view.find_withtag("advanced-inner"), (
        "the viewport carries no content window"
    )
    inner = view.nametowidget(view.itemcget(view.find_withtag("advanced-inner")[0], "window"))

    def ancestry(widget) -> list:
        chain = []
        node = widget
        while node is not None:
            chain.append(node)
            node = node.master
        return chain

    for name in ("record", "facts", "status"):
        widget = getattr(section, name)
        assert inner in ancestry(widget), (
            f"{name} is outside the scrolling viewport and clips at 600 px"
        )
    assert any(type(w).__name__ == "Scrollbar" for w in _walk(section)), (
        "a viewport with no scrollbar is a trap, not a fix"
    )


def _tail_filter_widgets(section: object):
    field = button = None
    for w in _walk(section):
        if type(w).__name__ == "Field" and w.label.cget("text") == "Filter":
            field = w
        if type(w).__name__ == "Button" and w.cget("text") == "Filter":
            button = w
    return field, button


def test_advanced_filters_the_audit_tail(win) -> None:
    """The tail is 40 rows of mono text; without a filter it is looked at,
    not read. A substring filter over the row's own fields, re-rendered
    locally — filtering asks the daemon nothing."""
    daemon = AdvDaemon()
    window = win(daemon)
    window._select("advanced")  # noqa: SLF001
    section = _section(window)

    field, button = _tail_filter_widgets(section)
    assert field is not None, "no Filter field on the Advanced tab"
    assert button is not None, "no Filter button on the Advanced tab"

    before = {op for op, _ in daemon.calls}
    field.set("workspace")
    button.invoke()
    _pump(button)

    rendered = section.record.get("1.0", "end")
    assert "workspace.add" in rendered, f"the match did not survive: {rendered!r}"
    assert "voice_config.set" not in rendered, (
        f"the filter kept a non-match: {rendered!r}"
    )
    assert {op for op, _ in daemon.calls} == before, (
        "filtering asked the daemon; it is a local re-render"
    )

    field.set("")
    button.invoke()
    _pump(button)
    rendered = section.record.get("1.0", "end")
    assert "workspace.add" in rendered and "voice_config.set" in rendered, (
        f"clearing the filter did not restore the tail: {rendered!r}"
    )
