"""The Appearance tab: overlay typography, chosen not coded. D100.

Transcript and entry only — the chrome stays Segoe UI by brand rule, with
this tab recorded as the deliberate exception. A choice applies the next
time the prompt window opens (the overlay re-reads `state/fonts.json` on
every show). Each test fails before the tab exists.
"""

from __future__ import annotations

import json

import pytest

from assistant import settings as S


@pytest.fixture
def win(tmp_path):
    made: list[S.Settings] = []

    def build(daemon, state_path=None):
        w = S.Settings(daemon, title="ButlerOY Settings",
                       state_path=state_path or tmp_path / "state")
        made.append(w)
        w.show()
        return w

    yield build
    for w in made:
        try:
            w.destroy()
        except Exception:  # noqa: BLE001
            pass


class QuietDaemon:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, op: str, **kwargs):
        self.calls.append((op, dict(kwargs)))
        return {"ok": True, "result": {}}


def _section(window: S.Settings) -> object:
    section = window._sections.get("appearance")  # noqa: SLF001
    assert section is not None, (
        f"no Appearance tab; tabs are {sorted(window._sections)}"  # noqa: SLF001
    )
    return section


def _selects(section: object) -> dict[str, object]:
    """Labelled choice controls, keyed by their label."""
    found: dict[str, object] = {}

    def visit(widget) -> None:
        if type(widget).__name__ == "Select":
            found[widget.label.cget("text")] = widget
        for child in widget.winfo_children():
            visit(child)

    for top in getattr(section, "widgets", []):
        visit(top)
    return found


def _button(section: object, text: str):
    found = []

    def visit(widget) -> None:
        if type(widget).__name__ == "Button":
            found.append(widget)
        for child in widget.winfo_children():
            visit(child)

    for top in getattr(section, "widgets", []):
        visit(top)
    for b in found:
        if b.cget("text") == text:
            return b
    raise AssertionError(
        f"no {text!r} button; present: {[b.cget('text') for b in found]}")


def _labels(section: object) -> list[str]:
    out: list = []

    def visit(widget) -> None:
        if type(widget).__name__ == "Label":
            out.append(widget.cget("text"))
        for child in widget.winfo_children():
            visit(child)

    for top in getattr(section, "widgets", []):
        visit(top)
    return out


def test_the_tab_shows_what_you_have(win) -> None:
    """The starting point is the live faces, never a guess (D100)."""
    import tkinter.font as tkfont

    window = win(QuietDaemon())
    window._select("appearance")  # noqa: SLF001
    selects = _selects(_section(window))
    assert set(selects) >= {"Family", "Size", "Alignment"}
    live = tkfont.nametofont("TkFixedFont").actual("family")
    assert selects["Family"].get() == live, (
        f"the family choice does not show the live face: {selects['Family'].get()!r}"
    )


def test_apply_writes_fonts_json_and_says_when_it_takes_effect(win, tmp_path) -> None:
    window = win(QuietDaemon(), state_path=tmp_path / "state")
    window._select("appearance")  # noqa: SLF001
    section = _section(window)
    selects = _selects(section)

    selects["Size"].set("14")
    selects["Alignment"].set("center")
    _button(section, "Apply").invoke()
    selects["Size"].widget.winfo_toplevel().update_idletasks()

    saved = json.loads((tmp_path / "state" / "fonts.json").read_text(
        encoding="utf-8"))
    assert saved["size"] == 14 and saved["align"] == "center"
    assert saved["family"] == selects["Family"].get()
    assert any("prompt window" in text for text in _labels(section)), (
        "the user was not told when the choice takes effect"
    )
