# Butleroy — brand kit

**Butleroy — LocalAIzed Concierge for Windows.**

A 24/7 local assistant for one Windows machine. This file is the whole brand, so
that nothing about how it looks has to be re-decided by whoever wires it next.

---

## 1. The idea

The reference term is **butleraized**, and it is a state rather than a service:
everything handled, nothing asked twice. A good butler is invisible — the room is
simply ready, and you only notice if they are absent.

So the brand is about **absence of friction**, not presence of a servant. Every
decision below follows from that:

- The mark is a bell, not a bow tie. A bow tie reads as costume; a bell reads as
  *handled*.
- Nothing in the kit is loud. No gradients with more than two stops, no drop
  shadows, no mascot.
- The one warm colour in the whole system is brass, and it is reserved for things
  that are *acting* — the brand accent is not decoration, it is a state indicator.

---

## 2. Name and typography

| | |
|---|---|
| Product | **Butleroy** |
| Descriptor | LocalAIzed Concierge for Windows |
| Wordmark face | `"Hoefler Text", "Playfair Display", Georgia, "Times New Roman", serif` |
| UI face | `"Inter", "Segoe UI", system-ui, -apple-system, sans-serif` |
| Mono face | `"Cascadia Code", "JetBrains Mono", Consolas, "Courier New", monospace` |

The wordmark is a serif because it is the only place old-world service belongs; the
UI is Segoe UI because it is guaranteed on the target platform and because a serif
at 12 px is a legibility bug. **Segoe UI is not a preference here** — it is the
reason the UI needs no webfont and no dependency.

`LocalAIzed Concierge` is set in caps at `+0.18em` tracking, `BONE` at 60% size,
beneath a 1 px brass rule. `for Windows` is **not** in the lockup: the platform is
a fact, not a tagline, and it belongs in the About box and the installer.

### Production note

The SVGs ship live text. Before anything goes to print, to the installer, or into a
signed binary, the wordmark must be **converted to outlines** against a licensed
cut of the serif — `Hoefler Text` and `Playfair Display` are both commercially
licensed and neither is redistributable. The raster set in `icons/` is already
outline-free and is safe to ship as-is.

---

## 3. Colour

Obsidian ground, brass accent, bone text. A private-members'-club palette: it
reads as service rather than as utility, and it is legible on the dark UI the
project already had.

| Token | Hex | Use |
|---|---|---|
| `obsidian` | `#0F120E` | app ground, window background |
| `slate` | `#1B2019` | raised surfaces, cards, menus |
| `slate-line` | `#2E342A` | hairlines, dividers, input borders |
| `brass` | `#C8A951` | **the** accent: active, focused, in-flight |
| `brass-lit` | `#E8CE86` | brass highlight, hover |
| `brass-deep` | `#7E6528` | pressed, shadowed brass |
| `bone` | `#EDE8DC` | primary text, mono tray mark |
| `sage` | `#7C8A7C` | secondary text, units, timestamps |
| `sage-dim` | `#59645A` | disabled, placeholder |
| `verdigris` | `#6AC47F` | success, allowed, T0 |
| `rust` | `#D9694F` | refusal, destructive, T3 |
| `amber` | `#C79A3C` | warning, confirmation pending |

`rust` was `#C4553F` and was corrected to `#D9694F` by
`test_text_colours_are_legible_on_the_grounds_they_are_used_on`: at 4.24:1 on
`obsidian` it failed WCAG AA for body text, and a colour reserved for *"this is
destructive"* cannot be the one that is hardest to read. Every state colour clears
4.5:1 on both grounds; `sage-dim` is the only token below that, and it is
disabled/placeholder text where the contrast exemption applies.

### Measured contrast

| | obsidian | slate |
|---|---|---|
| `bone` | 15.4:1 | 13.6:1 |
| `brass` | 8.3:1 | 7.3:1 |
| `verdigris` | 8.8:1 | 7.8:1 |
| `amber` | 7.3:1 | 6.4:1 |
| `rust` | 5.5:1 | 4.8:1 |
| `sage` | 5.2:1 | 4.6:1 |
| `sage-dim` | 3.1:1 | 2.7:1 |

### Mapping onto the existing UI

`overlay.py` already carries six colours that predate this kit. They are mapped,
not replaced, so nothing has to change at once:

| existing | token | delta |
|---|---|---|
| `BG = #14161a` | `obsidian #0F120E` | warmer, greener, darker |
| `PANEL = #1e2128` | `slate #1B2019` | warmer |
| `FG = #e6e6e6` | `bone #EDE8DC` | less blue, warmer |
| `MUTED = #8b8b8b` | `sage #7C8A7C` | green-grey instead of neutral |
| `ACCENT = #6aa9ff` | `brass #C8A951` | **deliberate**: a blue accent says "tool", a brass accent says "service" |
| `OK = #6ac47f` | `verdigris` | unchanged in value |
| `DANGER = #ff6a5a` | `rust #D9694F` | deeper, less alarming, and now clears 4.5:1 |

The `ACCENT` change is the one with a real cost: `#6aa9ff` reads clearly against
both grounds, `#C8A951` is warmer and marginally lower in contrast on `obsidian`.
Where a state colour must be unmistakable — the T3 dialog, a destructive
confirmation — use `rust` or `amber`, **never** brass. Brass means "the assistant
is doing something", and it must never mean "dangerous".

---

## 4. The mark

A concierge's bell, low and wide with a heavy rim — the proportion of a hotel desk
bell. Drawn on a 64-unit grid.

```
knob      circle  c(32, 12.4)  r 4.3
dome      M 17 40  C 17 25.6, 23.6 17.6, 32 17.6
                    C 40.4 17.6, 47 25.6, 47 40  Z
rim       x 10.5  y 38.2   w 43   h 8.4   r 4.2
plinth    x 15    y 48.6   w 34   h 6     r 3
highlight stroke from (21.5, 35.5) curving to (30, 20.6), 2.6 wide, 50%
```

The plinth is heavier than the knob, so the mark is nudged **down** one unit for
optical centring. Sound waves were drawn and cut: they push the silhouette
outward, which costs legibility at 16 px, and a confident mark does not need to
announce that it makes a noise.

### Files

| File | Use |
|---|---|
| `mark.svg` | brass, for light-ish or photographic grounds |
| `mark-mono.svg` | single `currentColor`, **the default** |
| `icon.svg` | plated app icon source |
| `logo.svg` | full lockup |
| `icons/butleroy-mark-{16…256}.png` | brass mark, all sizes |
| `icons/butleroy-tray-{16…256}.png` | mono mark |
| `icons/butleroy-icon-{16…256}.png` | plated, all sizes |
| `icons/butleroy.ico` | 9 frames, 16 → 256 |
| `icons/tray.png` | 32 px mono, for anything that wants a file |
| `glyphs/*.svg` | 27 UI glyphs — see `glyphs/README.md` |

### Where the geometry lives — and why there are three copies of it

**`src/assistant/brand.py` holds the numbers the product actually draws.** Not
this directory, and that is load-bearing:

- The **tray icon is rendered at runtime** from `assistant.brand`, not loaded from
  a file. `pyproject.toml` packages `src/` only, so a PNG under `brand/` simply
  does not exist for an installed user — a tray reading it would fall over, or
  worse, silently show nothing.
- `brand/tools/render_icons.py` is a **thin CLI over that module**. It touches the
  filesystem and nothing else.

So there is exactly one copy of the shape in the code, and two more on disk that
exist for other reasons:

| Copy | Exists because | Guarded by |
|---|---|---|
| `src/assistant/brand.py` | the tray draws it at runtime | — (source of truth) |
| `brand/mark.svg` | a designer needs a file they can open in a vector editor | `test_the_svg_the_tokens_and_the_generator_state_one_geometry` |
| `brand/tokens.json` | the UI reads palette and geometry as data, not re-typed hex | same test, plus `test_nothing_is_defined_twice_with_different_values` |

Three copies is only safe because the suite asserts all three agree — and it
**parses `mark.svg` rather than restating it**, because a test comparing a copy of
itself against the file it was copied from keeps passing after either changes.

`icons/` is **generated**. Edit `src/assistant/brand.py`, re-run
`.\.venv\Scripts\python.exe brand\tools\render_icons.py`, and never hand-edit a
PNG. `test_the_committed_rasters_are_not_stale` re-renders and compares bytes, so
a stale or hand-edited file fails the suite instead of quietly surviving.

`import assistant.brand` pulls in **no third-party module** — Pillow is imported
inside the drawing functions. That is checked in a subprocess, because D1 rests on
the daemon's import staying standard-library only, and an in-process `sys.modules`
check could only ever pass once pytest has already imported Pillow for something
else.

---

## 5. Widgets

There are none yet — deliberately. Widgets are specified here so they are built to
this kit rather than to whatever tkinter defaulted to, and so the set is designed
once. What they must obey:

- **Corners.** 6 px on controls, 10 px on cards and menus, 22.5% on the app plate.
  Nothing in between unless there is a reason.
- **Borders.** 1 px `slate-line`. A focused control is 1 px `brass` plus a 2 px
  `brass` at 24% outward — an outer glow, not a thick ring, which is what tkinter
  can actually draw.
- **Height.** 32 px rows, 36 px buttons, 40 px inputs. All multiples of 4.
- **Icons in the UI** come from `glyphs/`: 24-unit grid, 2 px stroke, round caps,
  `currentColor`. Never the brass gradient — a gradient inside a 16 px list row is
  mush.

### What is built, as of D76

| Section | State |
|---|---|
| Models | **built** - tiers, models on disk, leases, import by choosing a file |
| Voices | **built** - installed/known voices, default, whisper model, download, import, remove |
| Speech | not built - push-to-talk, wake word, mute |
| Workspaces | not built - approved roots, manifests |
| Authority | not built - tiers, confirmations, session ceiling |
| Advanced | not built - paths, state dir, hotkeys, logs |

### Widgets to build, in priority order

1. `Button` — primary (brass fill, `obsidian` text), secondary (`slate` fill), ghost
   (no fill), destructive (`rust` fill). Min 88 px wide.
2. `Field` — input with label above, hint below, error in `rust`. Never a
   placeholder in place of a label.
3. `Select` — native `tk` listbox styled, because a custom dropdown in tkinter is a
   week of work for a worse result.
4. `Toggle` — a switch is a checkbox with a rounded track; do not invent a pill.
5. `ListRow` — 32 px, icon + label + value + optional chevron. The workhorse.
6. `Panel` — titled group, `slate` on `obsidian`, 1 px `slate-line`.
7. `Badge` — small caps pill. T0 `verdigris`, T1 `sage`, T2 `amber`, T3 `rust`.
   The tier badge is how the authority model becomes visible.
8. `Meter` — VRAM and disk. `brass` fill on `slate-line` track.
9. `Dialog` — a `Toplevel` on the overlay's own root, `-topmost`, modal.

---

## 6. Voice

Butleroy speaks when spoken to, and it does not perform.

- **Confirm before acting, once.** "I'll remove `notes.txt`." not "Certainly! I'll
  go ahead and remove that file for you right now."
- **Report facts, not effort.** "Done — 4 files, 1.2 MB." not "I've successfully
  completed the task."
- **Never apologise twice.** If something was refused, say so and stop.
- **The refusal is not a failure of the butler.** "That's outside every approved
  workspace" is a complete sentence.
- **No exclamation marks.** One per answer is the ceiling; normally zero.

---

## 7. What this kit is not

- **Not a theme for the existing UI.** Mapping is given; adoption is a separate
  decision with its own tests.
- **Not the settings surface.** It is what that surface is drawn with.
- **Not a claim of trademark.** `Butleroy` and `LocalAIzed` are used here as
  working names for this project. If either is intended for public distribution,
  clear both before shipping — the earlier `Zed` idea was dropped for exactly this
  reason (a well-known open-source editor), and the same check applies to any name.