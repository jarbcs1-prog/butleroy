"""The Butleroy widget set. D80.

`brand/README.md` §5 specifies nine widgets with corner radii, border rules and
heights. Until now that was documentation with no code behind it: the settings
sections used raw `tk.Listbox`, `tk.Label` and `tk.Button` styled ad hoc with the
brand palette, so consistency came from repeating the same `configure(...)` calls
rather than from a shared component. That works exactly until someone adds a section
who did not read the file.

## Two things the spec asks for that tkinter cannot do

Both are corrected in `brand/README.md` rather than quietly ignored, because a spec
that claims something the code does not do is the failure this project keeps hitting.

**Rounded corners are not implemented.** `tk.Button`, `tk.Frame` and `tk.Label` are
rectangles; there is no radius option. A rounded control in tkinter means a `Canvas`
with hand-drawn shapes, which loses native focus handling, keyboard navigation and
screen-reader semantics - a bad trade for 6 px of corner. So every control here is a
sharp rectangle, and the spec now says so. `Meter` *is* a Canvas, because a filled bar
is a shape rather than a control and has no behaviour to lose.

**The SVG glyphs are not used in the UI.** tkinter cannot load SVG and this project has
no rasterizer (D66 keeps the dependency list to pystray and pillow, and pillow cannot
render SVG). So `ListRow`'s icon slot takes a short *text* mark rather than a glyph
file. The 27 SVGs remain what they are for: documentation, the brand kit, and any
future non-tk surface. Claiming otherwise would be a comment that lies.

## What is real

The tokens, the variants, the heights, the tier colours and the focus rule. Everything
below reads its colours from `palette()`, which reads `brand/tokens.json` - so a change
to the kit reaches the window, and a hex code typed into a section is a bug.

Tk is imported at module level here, unlike `settings.py` and `tray.py`. Those import it
lazily because they are imported by the daemon's process on a headless machine; this
module is only ever imported by a window that is about to build widgets, so the lazy
import would be ceremony.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import tkinter as tk

#: The brand values, repeated here so a missing kit still renders. Same values as
#: `brand/tokens.json`; the test suite asserts they agree, which is the only thing that
#: makes a second copy safe (D72's lesson).
FALLBACK = {
    "obsidian": "#0F120E",
    "slate": "#1B2019",
    "slate-line": "#2E342A",
    "brass": "#C8A951",
    "brass-lit": "#E8CE86",
    "brass-deep": "#7E6528",
    "bone": "#EDE8DC",
    "sage": "#7C8A7C",
    "sage-dim": "#59645A",
    "verdigris": "#6AC47F",
    "rust": "#D9694F",
    "amber": "#C79A3C",
}

#: The tier ladder, as the badge renders it. A user must be able to see what a call
#: will cost before approving it, and the badge is where that becomes visible.
TIER_COLOUR = {
    "T0": "verdigris",
    "T1": "sage",
    "T2": "amber",
    "T3": "rust",
}

#: Metrics, all multiples of 4 except the stroke widths. The spec's rule: 32 px rows,
#: 36 px buttons, 40 px inputs.
ROW_H = 32
BUTTON_H = 36
INPUT_H = 40
MIN_BUTTON_W = 88
HAIRLINE = 1
FOCUS_RING = 2

#: The UI face. Segoe UI is guaranteed on the target platform, which is why the kit
#: needs no webfont and no dependency.
UI_FONT = ("Segoe UI", 9)
UI_FONT_BOLD = ("Segoe UI", 9, "bold")
UI_FONT_TITLE = ("Segoe UI", 11, "bold")
MONO_FONT = ("Cascadia Code", 9)


def palette() -> dict[str, str]:
    """The brand colours, from the kit if it is reachable, else the same values.

    Read from `brand/tokens.json` rather than imported, because that file is
    documentation beside the artwork and is not part of the installed package (D72).
    An installed copy falls back to the values above, which is why they are here.
    """
    root = Path(__file__).resolve().parent.parent.parent
    try:
        raw = json.loads(
            (root / "brand" / "tokens.json").read_text(encoding="utf-8"))["colour"]
        return {k: v for k, v in raw.items() if not k.startswith("$")}
    except Exception:  # noqa: BLE001 - a missing kit must not stop the window
        return dict(FALLBACK)


def colour(name: str, fallback: str = "bone") -> str:
    """One token by name. A typo returns the fallback rather than raising.

    Tk raises on an invalid colour name - D69's seven-hex-digit bug - and a settings
    window that will not open because one token is misspelled is worse than one that
    is slightly the wrong shade. The test suite asserts every name used here is real.
    """
    return palette().get(name, palette().get(fallback, FALLBACK[fallback]))


# --------------------------------------------------------------------------- #
# Button
# --------------------------------------------------------------------------- #

#: Fill, text, and the hover/pressed pair. A ghost button has no fill, so its
#: active colour is the surface it sits on.
VARIANTS: dict[str, dict[str, str]] = {
    "primary":     {"bg": "brass",     "fg": "obsidian", "active_bg": "brass-lit"},
    "secondary":   {"bg": "slate",     "fg": "bone",     "active_bg": "slate-line"},
    "ghost":       {"bg": "slate",     "fg": "brass",    "active_bg": "slate-line"},
    "destructive": {"bg": "rust",      "fg": "obsidian", "active_bg": "amber"},
}


class Button(tk.Button):
    """A flat brand button in one of four variants.

    `relief="flat"` and `bd=0` on every variant, because Tk's default 3D border is the
    single thing that most makes a tkinter window look like a tkinter window.

    **No rounded corners** - see the module docstring. The spec said 6 px and the spec
    is now corrected, because the alternative is a Canvas control that loses focus and
    keyboard behaviour for a cosmetic gain.
    """

    def __init__(self, parent: Any, text: str, command: Callable | None = None,
                 *, variant: str = "secondary", width_chars: int | None = None,
                 **kw: Any) -> None:
        if variant not in VARIANTS:
            raise ValueError(
                f"unknown button variant {variant!r}; known are "
                f"{sorted(VARIANTS)}"
            )
        v = VARIANTS[variant]
        c = palette()
        super().__init__(
            parent, text=text, command=command,
            bg=c[v["bg"]], fg=c[v["fg"]],
            activebackground=c[v["active_bg"]],
            activeforeground=c[v["fg"]],
            # The focus ring, as far as Tk can draw one: a 2 px highlight in brass
            # when the button has focus. The spec's "1 px brass plus a 2 px glow" is
            # two borders and Tk has one, so this is the single-border version.
            highlightthickness=FOCUS_RING,
            highlightbackground=c[v["bg"]],
            highlightcolor=c["brass"],
            relief="flat", bd=0, font=UI_FONT,
            padx=12, pady=6, cursor="hand2",
            **({"width": width_chars} if width_chars is not None else {}),
            **kw,
        )
        self._variant = variant

    @property
    def variant(self) -> str:
        return self._variant

    def set_variant(self, variant: str) -> None:
        """Restyle in place, for a button whose meaning changes.

        Used by the Authority section's step-back button, which is disabled rather
        than restyled - but a Save button that becomes destructive is the case this
        exists for, and doing it by hand at the call site is how the palette drifts.
        """
        if variant not in VARIANTS:
            raise ValueError(f"unknown button variant {variant!r}")
        v = VARIANTS[variant]
        c = palette()
        self.configure(bg=c[v["bg"]], fg=c[v["fg"]],
                       activebackground=c[v["active_bg"]],
                       activeforeground=c[v["fg"]],
                       highlightbackground=c[v["bg"]])
        self._variant = variant


# --------------------------------------------------------------------------- #
# Field and Select
# --------------------------------------------------------------------------- #


class Field(tk.Frame):
    """A labelled input: label above, hint below, error in rust.

    The label is **always** present and never replaced by a placeholder, which the
    spec calls out because a placeholder disappears the moment you type and takes the
    question with it. `set_error` shows the message where the hint was, so the
    explanation appears next to the thing it is about.
    """

    def __init__(self, parent: Any, label: str, *, hint: str = "",
                 value: str = "", on_submit: Callable[[str], None] | None = None,
                 **kw: Any) -> None:
        c = palette()
        super().__init__(parent, bg=c["slate"], **kw)
        self._hint = hint

        self.label = tk.Label(self, text=label, fg=c["sage"], bg=c["slate"],
                              font=UI_FONT, anchor="w")
        self.label.pack(fill="x")
        self.entry = tk.Entry(self, fg=c["bone"], bg=c["obsidian"],
                              insertbackground=c["bone"], relief="flat", bd=0,
                              highlightthickness=HAIRLINE,
                              highlightbackground=c["slate-line"],
                              highlightcolor=c["brass"], font=MONO_FONT)
        self.entry.pack(fill="x", ipady=6, ipadx=6, pady=(2, 2))
        self.entry.insert(0, value)
        if on_submit is not None:
            self.entry.bind("<Return>", lambda _e: on_submit(self.entry.get()))

        self.hint = tk.Label(self, text=hint, fg=c["sage-dim"], bg=c["slate"],
                             font=UI_FONT, anchor="w", justify="left")
        self.hint.pack(fill="x")

    def get(self) -> str:
        return self.entry.get()

    def set(self, value: str) -> None:
        self.entry.delete(0, "end")
        self.entry.insert(0, value)

    def set_error(self, message: str) -> None:
        """Show an error where the hint was, or clear it back to the hint."""
        self.hint.configure(
            text=message or self._hint,
            fg=colour("rust") if message else colour("sage-dim"),
        )


class Select(tk.Frame):
    """A labelled dropdown, built on Tk's `OptionMenu`.

    The spec says native, and native it is: a custom dropdown in tkinter is a week of
    work that ends up worse than `OptionMenu`, which already handles keyboard
    navigation, the popup grab and click-outside. Styled as far as the native widget
    allows, which is the menu's own colours and not much else.
    """

    def __init__(self, parent: Any, label: str, options: Sequence[str],
                 *, value: str = "", on_change: Callable[[str], None] | None = None,
                 width: int = 18, **kw: Any) -> None:
        c = palette()
        super().__init__(parent, bg=c["slate"], **kw)

        self.label = tk.Label(self, text=label, fg=c["sage"], bg=c["slate"],
                              font=UI_FONT, anchor="w")
        self.label.pack(side="left")

        self.var = tk.StringVar(value=value or (options[0] if options else ""))
        self._on_change = on_change
        self.widget = tk.OptionMenu(self, self.var, *(options or [""]))
        self.widget.configure(bg=c["obsidian"], fg=c["bone"], relief="flat", bd=0,
                              highlightthickness=HAIRLINE,
                              highlightbackground=c["slate-line"],
                              activebackground=c["slate-line"],
                              font=MONO_FONT, width=width, anchor="w")
        self.widget["menu"].configure(bg=c["obsidian"], fg=c["bone"],
                                      activebackground=c["brass"],
                                      activeforeground=c["obsidian"])
        self.widget.pack(side="left", padx=(10, 0))

        # Tk's OptionMenu calls its own internal command; wrapping it is how a change
        # notification is attached without replacing the widget. Done by tracing the
        # variable, which also fires when the value is set in code - which is what
        # makes `set()` behave the same as a click.
        if on_change is not None:
            self.var.trace_add("write", lambda *_: on_change(self.var.get()))

    def get(self) -> str:
        return self.var.get()

    def set(self, value: str) -> None:
        self.var.set(value)

    def set_options(self, options: Iterable[str]) -> None:
        """Replace the list. Keeps the current value if it is still offered."""
        options = list(options)
        menu = self.widget["menu"]
        menu.delete(0, "end")
        for option in options:
            menu.add_command(label=option, command=lambda v=option: self.var.set(v))
        if self.var.get() not in options and options:
            self.var.set(options[0])


# --------------------------------------------------------------------------- #
# Toggle
# --------------------------------------------------------------------------- #


class Toggle(tk.Checkbutton):
    """A checkbox, styled flat.

    The spec says "a switch is a checkbox with a rounded track; do not invent a pill",
    and this is the checkbox without the rounded track - because Tk cannot round it.
    A pill would mean a Canvas, and a Canvas checkbox has no `variable`, no
    keyboard toggle and no accessible role. The label is always shown, so the control
    reads as a sentence rather than as an unexplained box.
    """

    def __init__(self, parent: Any, text: str, *, variable: Any = None,
                 command: Callable | None = None, **kw: Any) -> None:
        c = palette()
        super().__init__(
            parent, text=text, variable=variable, command=command,
            bg=c["slate"], fg=c["bone"], activebackground=c["slate"],
            activeforeground=c["bone"], selectcolor=c["obsidian"],
            relief="flat", bd=0, highlightthickness=0, anchor="w",
            font=UI_FONT, cursor="hand2", **kw,
        )


# --------------------------------------------------------------------------- #
# Badge
# --------------------------------------------------------------------------- #


class Badge(tk.Label):
    """A small pill showing a tier or a state.

    The one widget with a *background* colour that varies by meaning, and the reason is
    the authority model: "this call is T3" has to be visible at a glance, and colour is
    the only channel a label has. Everything else in the kit uses colour sparingly, per
    §3's rule that brass means "the assistant is acting" and must never mean
    "dangerous" - so the tiers map to `verdigris`/`sage`/`amber`/`rust` and never to
    brass.

    Not rounded, like everything else here. A pill that is actually a rectangle is
    honest; a pill drawn on a Canvas loses the label semantics that make it readable
    to a screen reader.
    """

    def __init__(self, parent: Any, text: str, *, token: str = "sage",
                 on_surface: str = "slate", **kw: Any) -> None:
        c = palette()
        super().__init__(
            parent, text=f" {text} ", bg=c.get(token, c["sage"]),
            fg=c["obsidian"], font=UI_FONT_BOLD, padx=6, pady=1, **kw,
        )
        self._token = token
        self._on_surface = on_surface

    @classmethod
    def for_tier(cls, parent: Any, tier: str, **kw: Any) -> "Badge":
        """A badge for a tier name, coloured by the ladder.

        An unknown tier gets `sage-dim` rather than raising: a badge that cannot render
        is a missing fact on screen, and the daemon is the thing that decides whether a
        tier is valid.
        """
        token = TIER_COLOUR.get(str(tier).strip().upper(), "sage-dim")
        return cls(parent, tier, token=token, **kw)

    def set_token(self, token: str) -> None:
        self.configure(bg=colour(token))
        self._token = token


# --------------------------------------------------------------------------- #
# Meter
# --------------------------------------------------------------------------- #


class Meter(tk.Canvas):
    """A filled bar: VRAM, disk, a lease against its ceiling.

    The one Canvas in the kit, and the reason is that a bar is a *shape* rather than a
    control - it has no focus, no keyboard interaction and nothing to be accessible
    about beyond the number beside it. So the trade the module docstring refuses for
    buttons is the right one here.

    The number is always drawn as text too. A bar with no figure is a bar you cannot
    quote, and "the card is 78% full" is not an answer to "can I fit 7 GiB".
    """

    def __init__(self, parent: Any, *, label: str = "", value: int = 0,
                 ceiling: int = 0, height: int = 12, width: int = 220,
                 unit: str = "GiB", stretch: bool = True, **kw: Any) -> None:
        c = palette()
        super().__init__(parent, height=height, width=width, bg=c["slate"],
                         highlightthickness=0, bd=0, **kw)
        self._label = label
        self._value = int(value or 0)
        self._ceiling = int(ceiling or 0)
        self._unit = unit
        self._height = height
        #: The track's width. In stretch mode this is whatever the widget has been
        #: given, less room for the figure on the right; otherwise the fixed width.
        #:
        #: Stretch is the default because the first version was not, and a VRAM meter
        #: occupying a sixth of its panel looked like a truncated widget rather than a
        #: gauge. A bar is read by comparing its length to something, and a bar whose
        #: length is arbitrary cannot be compared to anything.
        self._stretch = stretch
        self._fixed_width = width
        self._text_id: int | None = None
        self._track_id: int | None = None
        self._fill_id: int | None = None
        self._track_width = width
        self.bind("<Configure>", self._on_configure)
        self._draw()

    def set(self, value: int, ceiling: int | None = None) -> None:
        self._value = int(value or 0)
        if ceiling is not None:
            self._ceiling = int(ceiling or 0)
        self._draw()

    def _on_configure(self, event: Any) -> None:
        """Redraw when the widget is resized, so the bar tracks the panel.

        Bound rather than computed once, because the width a Canvas ends up with is
        decided by its parent's layout - which is not known when the widget is
        constructed. The first version drew once at a fixed width and never noticed.
        """
        if self._stretch and abs(int(event.width) - self._track_width) > 1:
            self._draw()

    def _draw(self) -> None:
        c = palette()
        self.delete("all")
        h = self._height
        text = self._describe()

        # Measure the figure first, then give the track whatever is left. The figure
        # goes on the right, so the bar grows left-to-right into the space beside it.
        probe = self.create_text(0, -1000, anchor="w", text=text, font=UI_FONT)
        box = self.bbox(probe)
        text_w = (box[2] - box[0]) if box else 0
        self.delete(probe)

        if self._stretch:
            available = max(60, self.winfo_width() - text_w - 12)
            self._track_width = available
        else:
            self._track_width = self._fixed_width

        w = self._track_width
        self._track_id = self.create_rectangle(
            0, 0, w, h, fill=c["slate-line"], outline="")
        ratio = 0.0
        if self._ceiling > 0:
            # Clamped at 1.0 and never above: a bar that overflows its track is a bar
            # that says "more than full", which is a different and more alarming claim
            # than the one being made. The number beside it carries the exact figure.
            ratio = max(0.0, min(1.0, self._value / float(self._ceiling)))
        if ratio > 0:
            self._fill_id = self.create_rectangle(
                0, 0, max(1, int(w * ratio)), h, fill=c["brass"], outline="")
        self._text_id = self.create_text(
            w + 8, h // 2, anchor="w", text=text, fill=c["sage"], font=UI_FONT)

    def _describe(self) -> str:
        if self._ceiling <= 0:
            return f"{self._label} {self._human(self._value)}".strip()
        pct = 100.0 * self._value / self._ceiling
        return (f"{self._label} {self._human(self._value)} of "
                f"{self._human(self._ceiling)} ({pct:.0f}%)").strip()

    def _human(self, n: int) -> str:
        if self._unit == "GiB":
            return f"{n / 1024 ** 3:.1f} GiB"
        if self._unit == "MB":
            return f"{n / 1024 ** 2:.0f} MB"
        return f"{n:,}"


# --------------------------------------------------------------------------- #
# Panel and ListRow
# --------------------------------------------------------------------------- #


class Panel(tk.Frame):
    """A titled group: `slate` on `obsidian`, a 1 px `slate-line` border.

    The border is Tk's `highlightthickness` rather than `bd`, because `bd` draws a 3D
    bevel and this needs a flat line. One pixel, one colour, per the spec.
    """

    def __init__(self, parent: Any, title: str = "", **kw: Any) -> None:
        c = palette()
        super().__init__(parent, bg=c["slate"], highlightthickness=HAIRLINE,
                         highlightbackground=c["slate-line"],
                         highlightcolor=c["slate-line"], bd=0, **kw)
        self.title = None
        self.body = tk.Frame(self, bg=c["slate"])
        if title:
            self.title = tk.Label(self, text=title, fg=c["brass"], bg=c["slate"],
                                  font=UI_FONT_BOLD, anchor="w")
            self.title.pack(fill="x", padx=12, pady=(10, 4))
        self.body.pack(fill="both", expand=True, padx=12, pady=(0, 10))


class ListRow(tk.Frame):
    """The workhorse: one line, label left, value right, optional chevron.

    ﻿`ROW_H` tall, per the spec's 32 px grid. The chevron is a text character rather
    than a glyph file - see the module docstring on why SVG cannot be used here.
    """

    def __init__(self, parent: Any, label: str, value: str = "", *,
                 mark: str = "", chevron: bool = False,
                 command: Callable | None = None, **kw: Any) -> None:
        c = palette()
        super().__init__(parent, bg=c["slate"], height=ROW_H, **kw)
        self.pack_propagate(False)

        self.mark = None
        if mark:
            self.mark = tk.Label(self, text=mark, fg=c["brass"], bg=c["slate"],
                                 font=UI_FONT, width=3, anchor="w")
            self.mark.pack(side="left", padx=(2, 0))

        self.chevron = None
        if chevron:
            self.chevron = tk.Label(self, text=">", fg=c["sage"], bg=c["slate"],
                                    font=UI_FONT)
            self.chevron.pack(side="right", padx=(6, 4))

        self.value = tk.Label(self, text=value, fg=c["sage"], bg=c["slate"],
                              font=UI_FONT, anchor="e")
        self.value.pack(side="right", padx=(6, 0))

        self.label = tk.Label(self, text=label, fg=c["bone"], bg=c["slate"],
                              font=UI_FONT, anchor="w")
        self.label.pack(side="left", fill="x", expand=True)

        if command is not None:
            for widget in (self, self.label, self.value):
                widget.bind("<Button-1>", lambda _e: command())
                widget.configure(cursor="hand2")

    def set_value(self, value: str) -> None:
        self.value.configure(text=value)


# --------------------------------------------------------------------------- #
# Dialog
# --------------------------------------------------------------------------- #


class Dialog(tk.Toplevel):
    """A modal dialog on its owner's root, always on top.

    Always on top, unlike the settings window itself. The rule from the spec and from
    D69 is: a *transient* thing that is waiting for an answer is topmost, because it is
    useless behind another window; a persistent window is not, because pinning it
    covers the desktop for as long as it is open.

    `transient()` is what makes it modal *to its owner* rather than to the whole
    desktop, and `grab_set()` is what stops clicks reaching the owner. Both are undone
    by `close()`, and `close()` is idempotent because D69's confirm dialog taught that
    a dialog can be dismissed twice - by the user and by a timeout - and the second
    dismissal must not raise.
    """

    def __init__(self, owner: Any, title: str, *, width: int = 460,
                 height: int = 220, **kw: Any) -> None:
        c = palette()
        super().__init__(owner, **kw)
        self.withdraw()                      # built hidden, shown by `open()`
        self.title(title)
        self.configure(bg=c["obsidian"])
        self.resizable(False, False)
        self.attributes("-topmost", True)
        self._closed = False

        self.body = tk.Frame(self, bg=c["obsidian"])
        self.body.pack(fill="both", expand=True, padx=16, pady=(14, 8))
        self.buttons = tk.Frame(self, bg=c["obsidian"])
        self.buttons.pack(fill="x", padx=16, pady=(0, 14))

        self.heading = tk.Label(self.body, text=title, fg=c["bone"], bg=c["obsidian"],
                                font=UI_FONT_TITLE, anchor="w", justify="left")
        self.heading.pack(fill="x")
        self.message = tk.Label(self.body, text="", fg=c["sage"], bg=c["obsidian"],
                                font=UI_FONT, anchor="w", justify="left",
                                wraplength=width - 40)
        self.message.pack(fill="x", pady=(6, 0))

        self._width = width
        self._height = height
        self.protocol("WM_DELETE_WINDOW", self.close)

    def say(self, message: str) -> None:
        self.message.configure(text=message)

    def add_button(self, text: str, command: Callable[[], None], *,
                   variant: str = "secondary") -> Button:
        button = Button(self.buttons, text, command, variant=variant)
        button.pack(side="right", padx=(6, 0))
        return button

    def open(self) -> None:
        """Centre on the owner, show, and take the grab."""
        owner = self.master
        try:
            owner.update_idletasks()
            x = owner.winfo_rootx() + max(0, (owner.winfo_width() - self._width) // 2)
            y = owner.winfo_rooty() + max(0, (owner.winfo_height() - self._height) // 3)
            self.geometry(f"{self._width}x{self._height}+{x}+{y}")
        except Exception:  # noqa: BLE001 - geometry is a nicety, not a requirement
            self.geometry(f"{self._width}x{self._height}")
        try:
            self.transient(owner)
        except Exception:  # noqa: BLE001 - not every parent supports it
            pass
        self.deiconify()
        self.lift()
        try:
            self.grab_set()
        except Exception:  # noqa: BLE001 - a grab can fail if the owner is not viewable
            pass
        try:
            self.focus_force()
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        """Idempotent. Releases the grab and destroys, once."""
        if self._closed:
            return
        self._closed = True
        try:
            self.grab_release()
        except Exception:  # noqa: BLE001 - releasing a grab that is not held is fine
            pass
        try:
            self.destroy()
        except Exception:  # noqa: BLE001
            pass


__all__ = [
    "BUTTON_H",
    "Badge",
    "Button",
    "Dialog",
    "FALLBACK",
    "Field",
    "HAIRLINE",
    "INPUT_H",
    "ListRow",
    "MIN_BUTTON_W",
    "Meter",
    "Panel",
    "ROW_H",
    "Select",
    "TIER_COLOUR",
    "Toggle",
    "UI_FONT",
    "UI_FONT_BOLD",
    "UI_FONT_TITLE",
    "VARIANTS",
    "colour",
    "palette",
]