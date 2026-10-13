"""The Vault tab: where the notes live, and how to move them. D100.

The vault path is startup-only (the bridge is built at daemon init), so this
tab displays and stages but never re-points anything live: Change writes
`state/vault.json`, the launcher honours it on the next start, and the status
line says exactly that. Each test fails before the tab exists.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from assistant import settings as S


class VaultDaemon:
    def __init__(self, path: str = "E:/Obsidian_Vault") -> None:
        self.calls: list[tuple[str, dict]] = []
        self.path = path

    def __call__(self, op: str, **kwargs):
        self.calls.append((op, dict(kwargs)))
        if op == "vault_status":
            return {"ok": True, "result": {
                "available": True, "error": None, "path": self.path,
                "describe": {"path": self.path, "notes": 3}, "stats": {}}}
        raise AssertionError(f"unexpected op {op!r}")


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


def _section(window: S.Settings) -> object:
    section = window._sections.get("vault")  # noqa: SLF001 - white-box
    assert section is not None, (
        f"no Vault tab on the window; tabs are "
        f"{sorted(window._sections)}"  # noqa: SLF001
    )
    return section


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


def test_the_vault_tab_shows_where_the_notes_live(win) -> None:
    window = win(VaultDaemon())
    window._select("vault")  # noqa: SLF001
    assert any("E:/Obsidian_Vault" in text
               for text in _labels(_section(window)))


def test_change_writes_vault_json_and_says_restart(win, tmp_path, monkeypatch) -> None:
    import tkinter.filedialog as filedialog

    picked = tmp_path / "picked"
    picked.mkdir()
    monkeypatch.setattr(filedialog, "askdirectory", lambda **kw: str(picked))

    daemon = VaultDaemon()
    window = win(daemon, state_path=tmp_path / "state")
    window._select("vault")  # noqa: SLF001
    section = _section(window)
    _button(section, "Change…").invoke()
    section.widgets[0].winfo_toplevel().update_idletasks()

    written = json.loads((tmp_path / "state" / "vault.json").read_text(
        encoding="utf-8"))
    assert Path(written["root"]) == picked
    assert any("restart" in text.lower() for text in _labels(section)), (
        "the user was not told a restart is needed"
    )


def test_cancelling_the_browser_writes_nothing(win, tmp_path, monkeypatch) -> None:
    import tkinter.filedialog as filedialog

    monkeypatch.setattr(filedialog, "askdirectory", lambda **kw: "")
    window = win(VaultDaemon(), state_path=tmp_path / "state")
    window._select("vault")  # noqa: SLF001
    _button(_section(window), "Change…").invoke()
    assert not (tmp_path / "state" / "vault.json").exists()


def test_a_non_directory_is_refused_not_written(win, tmp_path, monkeypatch) -> None:
    import tkinter.filedialog as filedialog

    f = tmp_path / "file.txt"
    f.write_text("x", encoding="utf-8")
    monkeypatch.setattr(filedialog, "askdirectory", lambda **kw: str(f))
    window = win(VaultDaemon(), state_path=tmp_path / "state")
    window._select("vault")  # noqa: SLF001
    section = _section(window)
    _button(section, "Change…").invoke()
    assert not (tmp_path / "state" / "vault.json").exists()
    assert any("irector" in text for text in _labels(section)), (
        "the refusal does not say what is wrong"
    )
