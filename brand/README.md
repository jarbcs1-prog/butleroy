# ButlerOY — brand kit

**ButlerOY — LocalAIzed Concierge for Windows.**

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
| Product | **ButlerOY** |
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

- **Corners. NOT IMPLEMENTED, and the spec was wrong to ask.** `tk.Button`,
  `tk.Frame` and `tk.Label` are rectangles with no radius option. Rounded corners in
  tkinter mean a `Canvas` with hand-drawn shapes, which loses native focus handling,
  keyboard navigation and screen-reader semantics — a bad trade for 6 px. Every
  control is a sharp rectangle. `Meter` *is* a `Canvas`, because a filled bar is a
  shape rather than a control and has no behaviour to lose. The 22.5% app plate is
  still real: it is in the `.ico`, which is a raster the OS draws.
- **Borders.** 1 px `slate-line`, via `highlightthickness` — `bd` draws a 3D bevel.
  A focused control is a 2 px `brass` highlight. The spec's "1 px plus a 2 px glow" is
  two borders and Tk has one, so it is the single-border version.
- **Height.** 32 px rows, 36 px buttons, 40 px inputs. All multiples of 4. Real, and
  asserted.
- **Icons in the UI. NOT IMPLEMENTED, and the spec was wrong to ask.** tkinter cannot
  load SVG and this project has no rasteriser (D66: pystray and pillow, and pillow
  cannot render SVG). `ListRow`'s icon slot takes a short *text* mark. The 27 glyphs
  remain the brand kit's, the documentation's, and any future non-tk surface's.

### What the interface is, and is not

Measured, not remembered: **32 of the daemon's 46 ops have a way in; 14 do not.**
Reached by `overlay.py` (7), `tray.py` (6, four of which were broken until D77) and
`settings.py` (26), counting the op names that actually appear as string literals in
those files - and, since D77, asserting that every one of them exists.

The figure in this paragraph has been 21, then 20, then 21, then 26, then 28,
then 29, then 30, and is 32 now that `workspace_resolve` and
`confirm_grant_revoke` have windows and the palette asks `ping`
(D98/D100/D101). Each earlier time it was written from the change just made
rather than from the measurement - the same mistake in miniature that the
whole section is about, so it is left visible rather than tidied.

| Section | State |
|---|---|
| Models | **operable** (D92) - one `Select` per tier offering the models in `models/`, staged on change, written via `Save` (`need_bytes` preserved). No add, remove, or path box — the daemon has no op for them. |
| Voices | **operable** (D93) - installed/known voices and the default shown; `Use voice`, `Save`; download, import and remove wired (`voice_install`, `voice_import`, `voice_remove`). |
| Speech | **not built** - push-to-talk, mute, wake word |
| Workspaces | **operable** - a list with `Approve folder…`, `Remove`, `Reload`, danger and allowed-tier pickers, edit-in-place (a remove+add cycle preserving the fields the pickers do not show), and open-in-Explorer. |
| Vault | **built** (D100) - the vault path displayed, `Change…` staging `state/vault.json`; the bridge is built at daemon init so a restart applies it. |
| Authority | **operable** (D78/D97/D101) - the tier ladder (four `Badge.for_tier` rows), attended state, `Step back (unattended)`, the "Waiting on you" pending list, live class grants with `Revoke grant`, and the "What would happen if…" `tool_check` predictor. **The session ceiling is staged in a dropdown and applied with a button** — the daemon's `session_set_ceiling` narrows any time and widens only while attended from a present channel. |
| Advanced | **rebuilt** (D95/D96) - grouped diagnostics with three read-only affordances: VRAM holders (`arbiter_summary`), what-the-model-was-given (`brain_describe`), is-this-path-approved (`workspace_covers`), plus a substring filter over the audit tail. Scrolls; the audit tail was already below the fold before the panels. |
| Appearance | **built** (D100) - family/size/alignment `Select`s writing `state/fonts.json`; the overlay applies it on every show. Transcript and entry only — the recorded brand exception. |

#### The control inventory, measured 2026-10-13

Counting `W.Button(` call sites in `settings.py` — the whole of the settings
window's interactivity:

| Section | Buttons | `Field` / `Select` / `Toggle` / `Badge` / `Meter` / `ListRow` / `Panel` / `Dialog` |
|---|---|---|
| Models | `Reload`, `Save` | `Select` ×1 |
| Voices | `Use voice`, `Save` | **none** |
| Workspaces | `Approve a folder…`, `Edit`, `Open folder`, `Remove`, `Reload` | **none** |
| Vault | `Change…`, `Reload` | **none** |
| Authority | `Step back (unattended)`, `Apply ceiling`, `Revoke grant`, `Reload` | `Select` ×1, `Badge` ×1 site |
| Advanced | `Reload`, `What the model was given`, `Check path`, `Filter` | `Field` ×2, `Panel` ×3 |
| Appearance | `Apply`, `Reload` | `Select` ×3 |

So the window instantiates **five of the nine widgets in `widgets.py`**: `Button`
everywhere (16 call sites, including the seven section tabs), `Select` in Models,
Authority and Appearance, `Badge` in Authority, `Panel` ×3 and `Field` ×2 in Advanced. `Toggle`, `Meter`,
`ListRow` and `Dialog` are built, tested, and still **never instantiated by the
settings window** — which is why this file describes the kit as load-bearing for
a surface that only partly uses it.

**Why this table was wrong.** The version above this one marked four sections
**built** and listed affordances — download, import, remove; revoke, danger level,
allowed tiers; the tier ladder, the ceiling, the pending list, the checker — that
have never existed. It was written from the intent rather than from the
measurement, which is the same mistake this file criticises elsewhere in its own
"27 ops with no way in" section. The specification is kept above and is real; the
state column now distinguishes what is built from what is specified.

#### Why the tests did not catch it

`tests/test_settings_real.py` has 17 cases and they are good ones: lifecycle, daemon
round-trips, save semantics, degraded states, widget release on disposal. **Not one
asserts that a section contains an affordance for the thing it is named after.**
A settings window that faithfully renders a read-only configuration satisfies
every one of them. The same shape as D87, where the suite proved a mechanism worked
while the thing a user actually needed went unasserted.

#### The redesign of Advanced

Advanced was rebuilt rather than extended (D95): 487 lines producing one `Reload`
button and no input widgets was the wrong shape for a settings tab, read-only or
not. Its diagnostics are kept — they are the only place a user can see what the
daemon believes — grouped by subject with read-only affordances for three of the
ops that had no way in (see the 16 below). The tab scrolls: facts, three panels
and the tail never fit 600 px, and the tail was already below the fold before
the panels.

#### The 14 ops with no way in, grouped

Counted 2026-10-14 against `Daemon._OPS`: 32 of 46 reached, 14 not.
`workspace_covers`, `arbiter_summary` and `brain_describe` gained a way in via
Advanced (D95); `session_set_ceiling` via Authority (D97); `ping` via the
overlay palette (D98); `workspace_resolve` via Workspaces and Advanced, and
`confirm_grant_revoke` via Authority (D100/D101); `mail_status`,
`reflect_overlay` and `tool_check` were already reached.

| Group | Ops | What a window would do |
|---|---|---|
| `vault_*` (5) | `append`, `index`, `read_journal`, `role_templates`, `write_note` | the Obsidian bridge; reads and writes for a surface of their own. `status` is reached from Advanced |
| `brain_*` (3) | `load`, `stream`, `unload` | process state (`load`/`unload`) and answering (`stream`); not settings. `status` and `describe` are reached from Advanced |
| `arbiter_*` (4) | `acquire`, `release`, `renew`, `sweep` | leases are the daemon's to manage; the sweep kills expired holders and already runs on a timer — refused for Advanced in D95. `summary` is reached from the tray and from Advanced |
| `audit_note` (1) | | a write, needing its own decision |
| `voice_watch` (1) | | start/stop wake-word listening; belongs to Voices |

#### Asked for, and does not exist in the code at all

- **Provider editing.** There is no provider to edit, and that is the design rather
  than an omission: reasoning is local only, and cloud providers are permitted in
  exactly one place - delegated workers (Plane D). A "providers" screen would be a
  screen for a feature the project forbids. Worth saying plainly because it was asked
  for, and the honest answer is not "not built yet".
- **"Modes."** No such concept exists. The nearest things are three *independent*
  settings that have never been presented together: attended/unattended, the session
  ceiling (T0-T3), and the fast/deep tier. If "modes" means naming a combination of
  those, that is a real feature and it is unbuilt.
- **A configurable hotkey.** `Ctrl+Alt+Space` is a constant in `tray.py`
  (`HOTKEY_LABEL`). The Win32 `RegisterHotKey` path exists (D30) but nothing can
  change the chord.
- **The wake word.** `hey_jarvis` is a constant in `audio.py`. A different one means
  retraining - `train_wakeword.py` exists, but there is no path from the window to it.
- **Transcript history.** The overlay's transcript is in-memory and dies with the
  window. Nothing writes a transcript anywhere, so there is nothing to search.
- **Per-tool grants, viewed and revoked.** D32's 600-second class grants are
  listed with remaining time in Authority and revoked there (D101); they are
  still *given* only from the confirmation dialog.
- **Import/export of settings.** `models.json` and `voices.json` are hand-editable and
  nothing more. No backup, no restore, no "export my configuration".

### The widgets, as built (D80)

`src/assistant/widgets.py`. The sections use it — seven ad-hoc button blocks and the
tier rows collapsed into it — so the kit is load bearing rather than a parallel
implementation.

| Widget | State |
|---|---|
| `Button` | built — primary, secondary, ghost, destructive, plus `set_variant` |
| `Field` | built — label above, hint below, `set_error` replaces the hint |
| `Select` | built — native `OptionMenu`, traced so a programmatic set notifies too |
| `Toggle` | built — a flat checkbox, label always shown |
| `Badge` | built — `for_tier()` is the single tier→colour mapping |
| `Meter` | built — a Canvas, stretches, clamps, always reports the figure as text |
| `Panel` | built — 1 px `slate-line` via `highlightthickness` |
| `ListRow` | built — 32 px, label + value + optional mark and chevron |
| `Dialog` | built — transient, topmost, `grab_set`, idempotent `close()` |

Whether it *looks* right cannot be asserted, so it was rendered and photographed. Two
defects were visible only that way: the meter did not stretch (it drew once at a fixed
width and never noticed being resized), and `ImageGrab` works in physical pixels while
Tk reports logical ones, so a bbox from `winfo_rootx()` lands on the wrong window at
any DPI scaling above 100%.

### Widgets still to build, in priority order

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

ButlerOY speaks when spoken to, and it does not perform.

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
- **Not a claim of trademark.** `ButlerOY` and `LocalAIzed` are used here as
  working names for this project. If either is intended for public distribution,
  clear both before shipping — the earlier `Zed` idea was dropped for exactly this
  reason (a well-known open-source editor), and the same check applies to any name.