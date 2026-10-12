"""Every widget in `widgets.py`, against a real Tk window.

Measured 2026-10-13: `settings.py` instantiates **seven of the nine** widgets in
this module, and all seven are `Button`. `Field`, `Select`, `Toggle`, `Badge`,
`Meter`, `Panel`, `ListRow` and `Dialog` were used **nowhere** in the product,
and no test instantiated any of them.

So D80's kit was unexercised: it could be entirely broken and 1413 tests would
still pass, because a test that never constructs a widget says nothing about it.
That is the same failure shape as D87, where the suite proved a mechanism worked
while the thing a user actually needed went unasserted.

These tests are the floor under the rebuild of the settings surface. Two of them
pin facts that are easy to get wrong and produce a convincing false defect:

  * `Badge.for_tier` is a **classmethod on `Badge`** - `Badge.for_tier(parent,
    tier)`. It is not a module-level function, so `W.for_tier(t)` raises
    AttributeError, and a probe that swallows that and substitutes a default
    renders all four tiers in one colour and looks like a broken tier map.
  * `Panel` owns an internal **`body` frame**. Children belong on `panel.body`.
    Packing on the Panel itself works, but `body` carries `expand=True` and
    absorbs the vertical slack, so the content is squeezed to the bottom and the
    layout looks broken when it is not.

The D67 lesson applies here and is not optional: a `Tk()` that is never pumped
maps a window with the right geometry and paints nothing, because painting *is*
event processing. So every assertion below pumps first and then reads geometry.
Nothing here asserts "the object was created".
"""

from __future__ import annotations

import tkinter as tk

import pytest

from assistant import widgets as W


@pytest.fixture
def root():
    """One real Tk root for the module, torn down even if a test fails.

    Deliberately **not** withdrawn. A withdrawn window is unmapped, so
    `winfo_width()` and `winfo_height()` report 1x1 for everything inside it
    however correctly it was packed - which makes every geometry assertion in
    this file pass vacuously or fail for the wrong reason. Real geometry needs a
    mapped window, so this one is mapped and then pumped.
    """
    r = tk.Tk()
    r.geometry("600x400+40+40")
    r.update_idletasks()
    r.update()
    try:
        yield r
    finally:
        try:
            r.destroy()
        except Exception:  # noqa: BLE001 - teardown must not mask a failure
            pass


def _settle(root: tk.Tk) -> None:
    """Pump until geometry is real. Painting is event processing (D67)."""
    root.update_idletasks()
    root.update()


def _pumped_size(widget: tk.Misc) -> tuple[int, int]:
    _settle(widget.winfo_toplevel())
    return widget.winfo_width(), widget.winfo_height()


# --------------------------------------------------------------------------- #
# Every widget constructs, packs, and is given real space.
# --------------------------------------------------------------------------- #

def test_every_widget_packs_and_is_given_geometry(root: tk.Tk) -> None:
    """Construction alone proves nothing - a widget packed at 0x0 has still
    never been laid out, which is how a broken widget survives."""
    panel = W.Panel(root, title="probe")
    panel.pack(fill="both", expand=True)
    body = panel.body

    made = {
        "button": W.Button(body, "Press", lambda: None, variant="primary"),
        "field": W.Field(body, "Label", hint="a hint"),
        "select": W.Select(body, "Choice", options=("a", "b"), value="a"),
        "toggle": W.Toggle(body, "Enabled", variable=tk.IntVar(value=1)),
        "badge": W.Badge.for_tier(body, "T2"),
        "meter": W.Meter(body, label="VRAM", value=1, ceiling=2),
        "listrow": W.ListRow(body, label="name", value="value"),
    }
    for name, widget in made.items():
        widget.pack(fill="x", pady=2)
    _settle(root)

    empty = [n for n, w in made.items() if _pumped_size(w) == (1, 1)]
    assert not empty, f"packed but never given space: {empty}"

    # A zero-height control is invisible; a 0-width one is unusable.
    squashed = [
        n for n, w in made.items()
        if any(dim <= 1 for dim in _pumped_size(w))
    ]
    assert not squashed, f"laid out at no usable size: {squashed}"


# --------------------------------------------------------------------------- #
# The tier badge - the brand's central idea, and the easiest to get wrong.
# --------------------------------------------------------------------------- #

def test_the_four_tiers_render_in_four_different_colours(root: tk.Tk) -> None:
    """T0 verdigris, T1 sage, T2 amber, T3 rust. Four tiers, four colours.

    `for_tier` is a classmethod **on Badge**. Calling `W.for_tier(t)` instead is
    the mistake that makes this look like a broken tier map.
    """
    row = tk.Frame(root, bg=W.palette()["slate"])
    row.pack()
    colours = {}
    for tier in ("T0", "T1", "T2", "T3"):
        badge = W.Badge.for_tier(row, tier)
        badge.pack(side="left")
        colours[tier] = badge.cget("bg")

    assert len(set(colours.values())) == 4, (
        f"the four tiers must be distinguishable, got {colours}"
    )
    expected = {
        "T0": W.palette()["verdigris"],
        "T1": W.palette()["sage"],
        "T2": W.palette()["amber"],
        "T3": W.palette()["rust"],
    }
    for tier, want in expected.items():
        assert colours[tier] == want, (
            f"{tier} renders as {colours[tier]}, expected {want}"
        )


def test_an_unknown_tier_is_visible_rather_than_invisible(root: tk.Tk) -> None:
    """An unrecognised tier gets `sage-dim`, not a crash and not a blank.

    The badge has to render a missing fact on screen - the daemon, not the
    window, is what decides whether a tier is valid.
    """
    badge = W.Badge.for_tier(tk.Frame(root), "T9")
    assert badge.cget("bg") == W.palette()["sage-dim"]


# --------------------------------------------------------------------------- #
# Panel - the layout trap.
# --------------------------------------------------------------------------- #

def test_a_panel_gives_its_body_the_vertical_slack(root: tk.Tk) -> None:
    """`panel.body` is the container, and it receives the panel's slack.

    A child packed on the Panel itself is a *sibling* of body, not a child of
    it, so it stacks above the body and renders at a perfectly normal size. An
    earlier version of this test asserted that such a child gets "squeezed",
    which sounds right and is false - measured, both come out 34 px. What is
    actually true, and what breaks, is that a Panel's children end up outside
    the body, so `body` stops being the thing you manage.
    """
    panel = W.Panel(root, title="probe")
    panel.pack(fill="both", expand=True)
    inside = W.Button(panel.body, "inside", lambda: None)
    inside.pack(fill="x")
    _settle(root)

    _, panel_h = _pumped_size(panel)
    _, body_h = _pumped_size(panel.body)

    # Measured 319 of 400. The margin is deliberately loose: the title strip and
    # body padding take the rest, and an exact fraction is a brittle assertion
    # that would fail on a font change rather than on a layout regression.
    assert body_h > panel_h * 0.7, (
        f"body should fill the panel; panel={panel_h} body={body_h}"
    )
    # `.master`, not `winfo_master()`. This Python/Tk build has no `winfo_master`
    # on a widget at all - `winfo_manager` exists and is something else
    # entirely, which is what the first two versions of this line reached for.
    assert inside.master is panel.body, (
        "a control packed on panel.body must be its child - if it is a sibling, "
        "it renders outside the panel's body and is disposed of differently"
    )


# --------------------------------------------------------------------------- #
# The sizes the kit specifies, so a regression is a failure rather than a taste
# question.
# --------------------------------------------------------------------------- #

def test_controls_meet_the_sizes_the_kit_specifies(root: tk.Tk) -> None:
    """Nothing interactive may be under 32 px, per brand/tokens.json `size`.

    Note a real off-spec, found by this test rather than by reading: the kit
    declares `BUTTON_H = 36`, exports it, and **never applies it**. A Button's
    height is decided by its font metrics and comes out at **34 px**. `INPUT_H`
    is the same story. So the constant is documentation rather than behaviour,
    and a 2 px discrepancy would otherwise be invisible forever.

    The assertion is therefore the 32 px floor - the kit's own hard rule, and the
    one that actually matters for hitting a control - not the nominal 36. Making
    a 2 px cosmetic gap a red test would train everyone to ignore this file.
    """
    button = W.Button(root, "Press", lambda: None, variant="primary")
    button.pack()
    field = W.Field(root, "Label")
    field.pack()
    row = W.ListRow(root, label="name", value="value")
    row.pack()
    _settle(root)

    _, button_h = _pumped_size(button)
    _, field_h = _pumped_size(field)
    _, row_h = _pumped_size(row)

    assert button_h >= 32, f"button is {button_h}px, too small to hit reliably"
    assert field_h >= 32, f"input is {field_h}px, too small to type in"
    assert row_h >= 32, f"row is {row_h}px, under the 32px workhorse height"
    assert W.ROW_H == 32 and W.MIN_BUTTON_W >= 88, (
        "the kit's own constants are the floor these assertions rely on"
    )


# --------------------------------------------------------------------------- #
# Button variants, because a destructive action must not look like a safe one.
# --------------------------------------------------------------------------- #

def test_the_four_button_variants_are_visibly_different(root: tk.Tk) -> None:
    """Brass is reserved for *acting*; rust is the only destructive fill."""
    fills = {}
    for variant in ("primary", "secondary", "ghost", "destructive"):
        b = W.Button(root, variant, lambda: None, variant=variant)
        b.pack()
        fills[variant] = b.cget("bg")
    _settle(root)

    c = W.palette()
    assert fills["primary"] == c["brass"]
    assert fills["destructive"] == c["rust"]
    assert fills["secondary"] == c["slate"]
    assert fills["primary"] != fills["destructive"], (
        "an action and a destructive action must not look alike"
    )


def test_an_unknown_variant_is_refused_loudly(root: tk.Tk) -> None:
    """A typo in a variant name must not silently fall back to a safe-looking
    button - that is how a destructive action ends up styled as primary."""
    with pytest.raises(ValueError):
        W.Button(root, "x", lambda: None, variant="primry")