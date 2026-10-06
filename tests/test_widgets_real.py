"""The widget set, on a real Tk.

Run by `test_settings_isolated.py` in a subprocess, for the same reason the settings
window is: a worker-thread Tk root in this build can leave Tcl's async handler to be
deleted at interpreter shutdown, and the process then exits `-2147483645` *after every
test has passed*.

The assertions here are deliberately about **meaning** rather than appearance. Whether
the kit looks right cannot be asserted - that was checked by screenshotting it and
looking (`ImageGrab`, since Tk cannot screenshot itself), and a test that pretended
otherwise would be the D36 shape. What *can* be asserted:

  * the tokens the kit uses exist, and agree with `brand/tokens.json`;
  * a tier badge maps a tier to the documented colour;
  * a meter clamps and formats;
  * a dialog closes exactly once;
  * every widget is a real Tk widget that a section can dispose of.

That last one matters more than it sounds: the whole point of the set is that a section
can hand its widgets to `dispose()` and have nothing leak.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from assistant import widgets as W

tk = pytest.importorskip("tkinter")

ROOT = Path(__file__).resolve().parent.parent


def _display_available() -> bool:
    """Whether a display is present, WITHOUT creating a root.

    This probe used to create and destroy a `tk.Tk()` at import time, and that turned
    out to break every later main-thread root under pytest in this build:

        _tkinter.TclError: Can't find a usable init.tcl

    Measured, not guessed: with the probe, 26 errors; without it, all pass. And it is
    almost certainly the same mechanism behind the `-2142143645` aborts this project
    has been chasing - a Tcl interpreter created and dropped on the main thread leaves
    the main thread's Tcl state unusable, and the interpreter is then torn down at
    shutdown by the wrong thread.

    The check is now a cheap capability test instead. On the target platform tkinter
    implies a display; where it does not, `tk.Tk()` raises in the fixture and the test
    errors, which is honest.
    """
    try:
        return tk.Tcl is not None
    except Exception:  # noqa: BLE001
        return False


requires_display = pytest.mark.skipif(
    not _display_available(), reason="no interactive session for Tk"
)


@pytest.fixture(scope="module")
def _root():
    """One Tk root for the whole module, built once.

    Not one per test, and the reason is measured rather than stylistic: creating a
    root in the display probe at import and then another in a test raises

        _tkinter.TclError: Can't find a usable init.tcl

    under pytest, in this Python 3.11.14 / uv build. Three roots in sequence are fine
    in a plain script, so it is the pytest context rather than Tk. One root avoids the
    question entirely, and it is closer to how the product works anyway - the settings
    window has one root and rebuilds sections inside it.
    """
    r = tk.Tk()
    # Off-screen rather than withdrawn. A withdrawn root reports width 1 for its
    # children, so a test that asserts a widget stretches cannot see it stretch - the
    # first version withdrew and the meter measured 60 px at every window size.
    r.geometry("900x300+4000+4000")
    r.update_idletasks()
    yield r
    try:
        r.destroy()
    except Exception:  # noqa: BLE001
        pass
    # Collect on the thread that created the interpreter. D75's lesson, and the same
    # mechanism: the widgets leave reference cycles, and an interpreter whose last
    # reference dies at *process* shutdown is destroyed by the main thread while its
    # Tcl async handler belongs to another - `Tcl_AsyncDelete`, exit `-2147483645`.
    import gc
    gc.collect()


@pytest.fixture
def root(_root):
    """The shared root, emptied between tests.

    Children are destroyed rather than the root, so a test cannot inherit the previous
    one's widgets - and a test that forgets to pack something still starts clean.
    """
    for child in _root.winfo_children():
        child.destroy()
    _root.geometry("")
    return lambda: _root


# --------------------------------------------------------------------------- #
# the tokens
# --------------------------------------------------------------------------- #


def test_the_fallback_palette_agrees_with_the_brand_kit() -> None:
    """Two copies of the palette, and this is what makes two safe.

    `widgets.FALLBACK` exists so an installed copy with no `brand/` directory still
    renders. The cost of that is a second copy of every hex value, and the cost is only
    acceptable because this test compares them - D72's lesson, applied to colour.
    """
    tokens = json.loads(
        (ROOT / "brand" / "tokens.json").read_text(encoding="utf-8"))["colour"]
    tokens = {k: v for k, v in tokens.items() if not k.startswith("$")}

    for name, value in W.FALLBACK.items():
        assert name in tokens, (
            f"widgets.FALLBACK has {name!r}, which brand/tokens.json does not"
        )
        assert tokens[name].upper() == value.upper(), (
            f"{name}: widgets says {value}, tokens.json says {tokens[name]}"
        )


def test_the_palette_reads_the_kit_and_falls_back_cleanly() -> None:
    """In a checkout it reads the file; the fallback is the same values either way."""
    live = W.palette()
    assert "brass" in live and "obsidian" in live
    assert live["brass"].upper() == W.FALLBACK["brass"].upper()


def test_every_token_the_widgets_name_is_a_real_token() -> None:
    """A typo'd token name renders as Tk's default grey, silently.

    D69 was a seven-hex-digit colour reaching a real widget and raising
    `invalid color name`. A *misspelled* name is the quieter version of the same bug:
    `palette().get("brsss", ...)` returns the fallback and the widget renders in the
    wrong colour with nothing anywhere saying so.
    """
    live = W.palette()
    for group in (W.VARIANTS, W.TIER_COLOUR):
        for key, spec in group.items():
            names = spec.values() if isinstance(spec, dict) else [spec]
            for name in names:
                assert name in live, f"{key} names {name!r}, which is not a token"
    for tier, token in W.TIER_COLOUR.items():
        assert token in live, f"{tier} maps to {token!r}, which is not a token"


def test_the_tier_badge_colours_match_the_spec() -> None:
    """The mapping the authority model is made visible through.

    Asserted as an exact mapping rather than "four different colours", because the
    point is *which* colour: `verdigris`/`sage`/`amber`/`rust` escalate, and a set that
    happened to be four other colours would pass a distinctness check while destroying
    the meaning.
    """
    assert W.TIER_COLOUR == {"T0": "verdigris", "T1": "sage",
                             "T2": "amber", "T3": "rust"}
    assert len(set(W.TIER_COLOUR.values())) == 4


# --------------------------------------------------------------------------- #
# Button
# --------------------------------------------------------------------------- #


@requires_display
def test_every_button_variant_builds(root) -> None:
    r = root()
    for variant in W.VARIANTS:
        button = W.Button(r, variant.title(), lambda: None, variant=variant)
        assert button.variant == variant
        assert str(button.cget("relief")) == "flat", (
            "a raised border is the single thing that most makes a tkinter window "
            "look like a tkinter window"
        )
        assert str(button.cget("bd")) == "0"


@requires_display
def test_an_unknown_button_variant_is_refused(root) -> None:
    """Refused rather than defaulted: a silently-secondary primary button is a bug
    that ships, and the message names the alternatives."""
    r = root()
    with pytest.raises(ValueError, match="unknown button variant"):
        W.Button(r, "x", None, variant="primray")


@requires_display
def test_a_button_can_change_variant_in_place(root) -> None:
    """For a button whose meaning changes - a Save that becomes destructive."""
    r = root()
    button = W.Button(r, "Save", None, variant="secondary")
    before = str(button.cget("bg"))

    button.set_variant("destructive")

    assert str(button.cget("bg")) != before
    assert str(button.cget("bg")).upper() == W.FALLBACK["rust"].upper()


@requires_display
def test_a_ghost_button_has_the_surface_colour_not_transparency(root) -> None:
    """Tk has no transparent fill, so a ghost button is the surface colour.

    Worth asserting because the alternative - `bg=""` or a magic value - raises at
    configure time in Tk, and a kit where one variant does not construct is a kit
    nobody uses.
    """
    r = root()
    ghost = W.Button(r, "Ghost", None, variant="ghost")
    assert str(ghost.cget("bg")).upper() == W.FALLBACK["slate"].upper()


# --------------------------------------------------------------------------- #
# Badge
# --------------------------------------------------------------------------- #


@requires_display
def test_a_tier_badge_uses_the_ladder_colour(root) -> None:
    r = root()
    for tier, token in W.TIER_COLOUR.items():
        badge = W.Badge.for_tier(r, tier)
        assert str(badge.cget("bg")).upper() == W.FALLBACK[token].upper(), tier


@requires_display
def test_a_lowercase_tier_still_maps(root) -> None:
    """The daemon spells tiers `T0`; a user reading `status` sees `T0`; but a caller
    that lowercased one must not silently get the unknown-tier colour."""
    r = root()
    badge = W.Badge.for_tier(r, "t3")
    assert str(badge.cget("bg")).upper() == W.FALLBACK["rust"].upper()


@requires_display
def test_an_unknown_tier_badge_does_not_raise(root) -> None:
    """A badge that cannot render is a *missing fact on screen*.

    The daemon is the thing that decides whether a tier is valid; a widget that raised
    would turn a bad tier into a window that will not open.
    """
    r = root()
    badge = W.Badge.for_tier(r, "T9")
    assert str(badge.cget("bg")).upper() == W.FALLBACK["sage-dim"].upper()


# --------------------------------------------------------------------------- #
# Meter
# --------------------------------------------------------------------------- #


@requires_display
def test_a_meter_reports_the_figure_as_text_not_only_as_a_bar(root) -> None:
    """A bar with no figure is a bar you cannot quote.

    "The card is 78% full" is not an answer to "can I fit 7 GiB".
    """
    r = root()
    meter = W.Meter(r, label="VRAM", value=int(6 * 1024 ** 3),
                    ceiling=int(12 * 1024 ** 3))
    meter.pack(fill="x")
    r.update_idletasks()
    r.update()

    text = meter.itemcget(meter._text_id, "text")
    assert "6.0 GiB" in text and "12.0 GiB" in text, text
    assert "50%" in text, text


@requires_display
def test_a_meter_clamps_rather_than_overflowing(root) -> None:
    """Over the ceiling clamps to full and says so in the figure.

    A bar that overflows its track makes a different and more alarming claim than the
    one being made; the exact number is what carries the truth.
    """
    r = root()
    meter = W.Meter(r, label="Lease", value=int(20 * 1024 ** 3),
                    ceiling=int(7 * 1024 ** 3))
    meter.pack(fill="x")
    r.update_idletasks()
    r.update()

    fill = meter.coords(meter._fill_id)
    track = meter.coords(meter._track_id)
    assert fill[2] <= track[2] + 1, (
        f"the fill ({fill}) extends past the track ({track})"
    )
    assert "286%" in meter.itemcget(meter._text_id, "text"), (
        "the figure does not report the true overage"
    )


@requires_display
def test_a_meter_with_no_ceiling_does_not_divide_by_zero(root) -> None:
    """`ceiling=0` is the state before anything is loaded, which is the common case."""
    r = root()
    meter = W.Meter(r, label="VRAM", value=0, ceiling=0)
    meter.pack(fill="x")
    r.update_idletasks()
    r.update()

    assert meter._fill_id is None, "an empty meter drew a fill"
    assert meter.itemcget(meter._text_id, "text")


@requires_display
def test_a_meter_stretches_with_its_parent(root) -> None:
    """The defect the first version shipped with.

    It drew once at a fixed 220 px and never noticed being resized, so a VRAM meter
    occupied a sixth of its panel and read as a truncated widget rather than a gauge.
    A bar is read by comparing its length to something, and a bar whose length is
    arbitrary cannot be compared to anything.
    """
    r = root()
    meter = W.Meter(r, label="VRAM", value=int(6 * 1024 ** 3),
                    ceiling=int(12 * 1024 ** 3))
    meter.pack(fill="x")
    r.geometry("800x200")
    r.update_idletasks()
    r.update()

    wide = meter.coords(meter._track_id)[2]

    r.geometry("300x200")
    r.update_idletasks()
    r.update()

    narrow = meter.coords(meter._track_id)[2]
    assert wide > narrow, (
        f"the track did not shrink with the window ({wide} then {narrow}); it is "
        "drawn at a fixed width"
    )


# --------------------------------------------------------------------------- #
# Field, Select, Toggle
# --------------------------------------------------------------------------- #


@requires_display
def test_a_field_always_has_a_label_and_never_a_placeholder(root) -> None:
    """The spec calls this out because a placeholder disappears the moment you type,
    and takes the question with it."""
    r = root()
    field = W.Field(r, "State directory", hint="where audit.jsonl lives")
    assert field.label.cget("text") == "State directory"
    assert field.hint.cget("text") == "where audit.jsonl lives"


@requires_display
def test_a_field_error_replaces_the_hint_and_can_be_cleared(root) -> None:
    """The explanation appears next to the thing it is about, and goes away again."""
    r = root()
    field = W.Field(r, "State directory", hint="the hint")

    field.set_error("not writable")
    assert field.hint.cget("text") == "not writable"
    assert str(field.hint.cget("fg")).upper() == W.FALLBACK["rust"].upper()

    field.set_error("")
    assert field.hint.cget("text") == "the hint", (
        "clearing the error lost the hint it replaced"
    )


@requires_display
def test_a_select_notifies_on_change_including_when_set_in_code(root) -> None:
    """Traced rather than wrapped around the widget's command.

    A caller that sets the value in code must behave the same as a user who clicked -
    otherwise a form restored from a file does not fire the change handler, and the
    next Save writes back the value that was there before.
    """
    r = root()
    seen: list[str] = []
    select = W.Select(r, "Danger", ["low", "medium", "high"],
                      value="low", on_change=seen.append)

    select.set("high")
    r.update_idletasks()

    assert seen and seen[-1] == "high", f"no notification for a programmatic set: {seen}"


@requires_display
def test_a_select_keeps_a_value_that_is_still_offered(root) -> None:
    r = root()
    select = W.Select(r, "Danger", ["low", "medium", "high"], value="medium")

    select.set_options(["low", "medium", "high", "extreme"])

    assert select.get() == "medium", "re-listing the options reset the selection"


@requires_display
def test_a_select_falls_back_when_its_value_disappears(root) -> None:
    r = root()
    select = W.Select(r, "Danger", ["low", "medium"], value="medium")

    select.set_options(["low", "high"])

    assert select.get() == "low", (
        "the selection stayed on a value that is no longer offered"
    )


@requires_display
def test_a_toggle_carries_a_variable_and_a_label(root) -> None:
    r = root()
    var = tk.BooleanVar(value=False)
    toggle = W.Toggle(r, "Listen for the wake word", variable=var)

    assert toggle.cget("text") == "Listen for the wake word"
    var.set(True)
    assert var.get() is True


# --------------------------------------------------------------------------- #
# Panel and ListRow
# --------------------------------------------------------------------------- #


@requires_display
def test_a_panel_has_a_hairline_border_and_a_body(root) -> None:
    """The border is `highlightthickness`, not `bd` - `bd` draws a 3D bevel."""
    r = root()
    panel = W.Panel(r, "Group")
    assert str(panel.cget("highlightthickness")) == str(W.HAIRLINE)
    assert str(panel.cget("bd")) == "0"
    assert panel.title is not None and panel.title.cget("text") == "Group"
    assert panel.body is not None


@requires_display
def test_a_panel_without_a_title_still_has_a_body(root) -> None:
    r = root()
    panel = W.Panel(r)
    assert panel.title is None
    assert panel.body is not None


@requires_display
def test_a_list_row_is_the_documented_height(root) -> None:
    r = root()
    row = W.ListRow(r, "Fast tier", "4B Q8_0")
    assert row.cget("height") == W.ROW_H, (
        f"the row is {row.cget('height')} px, not the {W.ROW_H} the spec's 4 px grid "
        "calls for"
    )


@requires_display
def test_a_list_row_value_can_be_updated(root) -> None:
    """Rows are the workhorse and most of them change: a lease, a count, a size."""
    r = root()
    row = W.ListRow(r, "Voices", "3")
    row.set_value("4")
    assert row.value.cget("text") == "4"


# --------------------------------------------------------------------------- #
# Dialog
# --------------------------------------------------------------------------- #


@requires_display
def test_a_dialog_is_built_hidden_and_shown_by_open(root) -> None:
    """Built hidden so it can be positioned before it is seen.

    A dialog that maps at 0,0 and then jumps to the centre is a dialog the user
    watches move.
    """
    r = root()
    r.deiconify()
    dlg = W.Dialog(r, "Remove voice", width=400, height=160)
    assert dlg.winfo_ismapped() == 0, "the dialog was visible before open()"

    dlg.open()
    r.update_idletasks()
    r.update()
    assert dlg.winfo_ismapped() == 1
    dlg.close()


@requires_display
def test_a_dialog_close_is_idempotent(root) -> None:
    """D69's lesson: a dialog can be dismissed twice - by the user and by a timeout -
    and the second dismissal must not raise."""
    r = root()
    r.deiconify()
    dlg = W.Dialog(r, "Confirm", width=380, height=140)
    dlg.open()
    dlg.close()
    dlg.close()
    assert dlg._closed is True


@requires_display
def test_a_dialog_takes_the_grab_and_gives_it_back(root) -> None:
    """Modal to its owner, and not modal afterwards.

    A grab that is not released leaves the whole application unclickable, which is a
    worse failure than a non-modal dialog.
    """
    r = root()
    r.deiconify()
    dlg = W.Dialog(r, "Confirm", width=380, height=140)
    dlg.open()
    r.update_idletasks()
    assert dlg.grab_current() == dlg

    dlg.close()
    r.update_idletasks()
    assert r.grab_current() is None, "the grab was not released"


@requires_display
def test_a_dialog_button_carries_its_variant(root) -> None:
    """So a destructive dialog action is the destructive colour without the caller
    remembering which token that is."""
    r = root()
    dlg = W.Dialog(r, "Remove", width=380, height=140)
    button = dlg.add_button("Remove", dlg.close, variant="destructive")
    assert button.variant == "destructive"
    assert str(button.cget("bg")).upper() == W.FALLBACK["rust"].upper()
    dlg.close()


# --------------------------------------------------------------------------- #
# the property the whole set exists for
# --------------------------------------------------------------------------- #


@requires_display
def test_every_widget_is_a_real_tk_widget_that_dispose_can_release(root) -> None:
    """The kit's actual purpose: a section can hold these and hand them to `dispose()`.

    `Section.dispose` nulls attributes that have a `.tk`; anything in the kit that is
    *not* a Tk widget would be held forever and would not be a widget at all. So each
    one is asserted to be a `tk.Misc` and to expose `.tk`.
    """
    r = root()
    built = [
        W.Button(r, "b", None),
        W.Badge(r, "T0"),
        W.Meter(r, label="x"),
        W.Field(r, "label"),
        W.Select(r, "label", ["a", "b"]),
        W.Toggle(r, "label"),
        W.Panel(r, "title"),
        W.ListRow(r, "label", "value"),
        W.Dialog(r, "title"),
    ]
    for widget in built:
        assert isinstance(widget, tk.Misc), f"{widget!r} is not a Tk widget"
        assert hasattr(widget, "tk"), f"{widget!r} has no interpreter handle"
    built[-1].close()