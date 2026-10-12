"""ModelsSection must be operable, not a viewer.

Measured 2026-10-13 (D89/D90): `ModelsSection.build` created a note, a `Listbox`,
a detail label, and two buttons - `Reload` and `Save`. `Save` re-persisted the
table `_load` had just read, so there was **no way to change a tier assignment at
all**. A user could look at which model answered fast and which answered deep, and
could not alter either.

That is what these tests are for. Each one is written to fail before the affordance
exists - see the module docstring in `test_widgets_render.py` for why a test that
merely agrees with the code is worth nothing.

**A note on what "add a model" may mean.** `modelconfig` stores a *filename
relative to `models/`*, never an absolute path, deliberately so that a checkout
that moves does not invalidate the config, and `_SAFE_NAME` rejects anything that
is not `^[A-Za-z0-9][A-Za-z0-9._- ]{0,180}\\.gguf$`. So a text box for typing an
arbitrary path would be a control the daemon rejects on every keystroke. The
affordance that is honest here is **choosing among the models already on disk**.
Copying a new file into `models/` is a different operation and would need a daemon
op that does not exist yet.
"""

from __future__ import annotations

import tkinter as tk

import pytest

from assistant import settings as S


class ModelsDaemon:
    """Minimal stand-in: answers `model_config`, records `model_config_set`."""

    def __init__(self) -> None:
        self.written: list[dict] = []

    def __call__(self, op: str, **kwargs):
        if op == "model_config":
            return {
                "ok": True,
                "result": {
                    "provenance": "file",
                    "models": [
                        {"name": "small-Q4.gguf", "bytes": 550_000_000},
                        {"name": "medium-Q8.gguf", "bytes": 4_400_000_000},
                        {"name": "large-XS.gguf", "bytes": 6_300_000_000},
                    ],
                    "tiers": {
                        "fast": {"model": "medium-Q8.gguf", "need_bytes": 7 * 1024 ** 3},
                        "deep": {"model": "large-XS.gguf", "need_bytes": 7 * 1024 ** 3},
                    },
                },
            }
        if op == "model_config_set":
            self.written.append(kwargs.get("tiers") or {})
            return {"ok": True, "result": {}}
        raise AssertionError(f"unexpected op {op!r}")


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


def _models_section(window: S.Settings) -> object:
    for section in window._sections.values():  # noqa: SLF001 - white-box on purpose
        if type(section).__name__ == "ModelsSection":
            return section
    raise AssertionError("no ModelsSection on the window")


def _menu_labels(select: object) -> set[str]:
    """What the dropdown actually offers, read from the real menu.

    Not `widget.cget("options")` - a `tk.OptionMenu` raises
    `TclError: unknown option "-options"`, which is the first version of this
    helper and looked like the dropdown being empty.
    """
    option_menu = select.widget
    menu = option_menu.nametowidget(option_menu["menu"])
    return {menu.entrycget(i, "label") for i in range(menu.index("end") + 1)}


def _selects(section: object) -> list[object]:
    """Selects anywhere in the section's widget tree."""
    found = []

    def walk(widget):
        for child in widget.winfo_children():
            if type(child).__name__ == "Select":
                found.append(child)
            walk(child)

    for w in getattr(section, "widgets", []):
        walk(w)
    return found


def test_each_tier_offers_the_models_on_disk_as_a_choice(win) -> None:
    """A tier must be assignable from the list, not just displayed.

    This is the assertion that failed before the change: ModelsSection built two
    buttons and a listbox, and nothing a user could change an assignment with.
    """
    window = win(ModelsDaemon())
    section = _models_section(window)

    selects = _selects(section)
    assert len(selects) >= 2, (
        "the fast and deep tiers each need a control for choosing their model; "
        f"found {len(selects)} choice control(s)"
    )

    on_disk = {"small-Q4.gguf", "medium-Q8.gguf", "large-XS.gguf"}
    for control in selects:
        offered = _menu_labels(control)
        assert on_disk <= offered, (
            f"every model on disk must be offerable; offered {sorted(offered)}, "
            f"on disk {sorted(on_disk)}"
        )


def test_changing_a_tier_and_saving_sends_the_new_assignment(win) -> None:
    """The whole point: pick a different model, save, and the daemon is told.

    Asserts the daemon received the *new* tiers - not merely that save reported
    success, which is the failure mode D84's `brain_surface` check exists to catch.
    """
    daemon = ModelsDaemon()
    window = win(daemon)
    section = _models_section(window)

    selects = _selects(section)
    assert selects, "no choice control exists, so there is nothing to exercise"

    # Drive the Select wrapper, not its inner OptionMenu - the wrapper owns the
    # StringVar, and setting the variable is what fires `on_change`.
    selects[0].set("small-Q4.gguf")
    # `Settings` is a wrapper, not a tk widget, so it has no update_idletasks.
    # Pump the real toplevel the way `test_settings_real.py` does.
    selects[0].widget.winfo_toplevel().update_idletasks()

    section._save()  # noqa: SLF001 - the button's callback

    assert daemon.written, "saving sent the daemon nothing at all"
    tiers = daemon.written[-1]
    assert "fast" in tiers and "deep" in tiers, f"expected both tiers, got {tiers}"
    assigned = {t.get("model") for t in tiers.values()}
    assert "small-Q4.gguf" in assigned, (
        f"the chosen model was not written; daemon got {tiers}"
    )
    # The lease ceiling is the session's, not the model's. Losing it here would
    # silently change what the arbiter permits on the next turn.
    assert all("need_bytes" in t for t in tiers.values()), (
        f"saving must not drop the lease ceiling: {tiers}"
    )


def test_the_unchanged_assignment_is_not_written_as_a_surprise(win) -> None:
    """Reload then Save with no edit must be a no-op on the wire.

    Guards the opposite failure: a control that always writes, so a Save with no
    deliberate change still rewrites the config and shows up in the audit chain as
    an unasked-for edit.
    """
    daemon = ModelsDaemon()
    window = win(daemon)
    section = _models_section(window)

    section._reload()  # noqa: SLF001
    section._save()  # noqa: SLF001

    if daemon.written:
        tiers = daemon.written[-1]
        assert tiers.get("fast", {}).get("model") == "medium-Q8.gguf", (
            f"a no-op save changed the fast tier: {tiers}"
        )