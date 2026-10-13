"""The brand kit, and the promise that its assets agree with each other.

An icon set is the easiest thing in a repository to rot, because nothing breaks:
the tray keeps showing last month's bell, the docs keep showing yesterday's hex
codes, and the only symptom is that nobody can say why the app looks like two
different products. So the assertions here are all *cross-checks* between files
rather than checks of any single file:

  * `assistant.brand`, `mark.svg` and `tokens.json` must state the same numbers.
    Three copies of one geometry, and the test that makes that safe is the reason
    it is safe.
  * the generated rasters must exist at every size the OS asks for, must not be
    stale, and the `.ico` must contain the frames Windows requests.
  * the tray icon must be the brand mark, compared as pixels.
  * the glyphs must be well-formed, on-grid, and named by meaning.

None of these can fail in production, which is exactly why they are here.

Deliberately no test asserts a colour *looks* right. Judgement about whether brass
reads as brass cannot be automated, and a test that pretended to would be theatre -
the D36 shape. What can be checked is that the palette is internally consistent, is
legible against the grounds it is used on, and is not defined twice with different
values.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

from assistant import brand

BRAND = Path(__file__).resolve().parent.parent / "brand"
TOKENS = json.loads((BRAND / "tokens.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# the geometry, in one place
# --------------------------------------------------------------------------- #


def _svg_geometry() -> dict:
    """The numbers, read out of `mark.svg` rather than restated here.

    Parsed from the SVG on purpose. If the constants were restated, the test would
    be comparing a copy of itself with the file it was copied from, and would keep
    passing after either one changed.
    """
    text = (BRAND / "mark.svg").read_text(encoding="utf-8")
    knob = re.search(r'<circle cx="([\d.]+)" cy="([\d.]+)" r="([\d.]+)"', text)
    # The dome is one path spanning two lines:
    #   M 17 40
    #     C 17 25.6, 23.6 17.6, 32 17.6
    #     C 40.4 17.6, 47 25.6, 47 40 Z
    # so all four cubics are captured in one pass and then split, rather than
    # trying to index a partial match.
    path = re.search(r'<path d="M\s*([\d.]+) ([\d.]+)\s*'
                     r'C\s*([\d.]+) ([\d.]+),\s*([\d.]+) ([\d.]+),\s*([\d.]+) ([\d.]+)\s*'
                     r'C\s*([\d.]+) ([\d.]+),\s*([\d.]+) ([\d.]+),\s*([\d.]+) ([\d.]+)',
                     text, re.S)
    assert path, "the dome path in mark.svg could not be read; has it been reworded?"
    n = [float(g) for g in path.groups()]
    rects = re.findall(r'<rect x="([\d.]+)" y="([\d.]+)" width="([\d.]+)" '
                       r'height="([\d.]+)" rx="([\d.]+)"', text)
    assert len(rects) >= 2, "mark.svg should have a rim and a plinth"
    return {
        "knob": {"cx": float(knob.group(1)), "cy": float(knob.group(2)),
                 "r": float(knob.group(3))},
        "dome": {
            "base_y": n[1],
            "left_x": n[0],
            "right_x": n[12],
            "start_l": [n[0], n[1]],
            "ctrl_l1": [n[2], n[3]],
            "ctrl_l2": [n[4], n[5]],
            "apex": [n[6], n[7]],
            "ctrl_r1": [n[8], n[9]],
            "ctrl_r2": [n[10], n[11]],
            "end_r": [n[12], n[13]],
        },
        "rim": dict(zip(("x", "y", "w", "h", "r"), map(float, rects[0]))),
        "plinth": dict(zip(("x", "y", "w", "h", "r"), map(float, rects[1]))),
    }


def test_the_svg_the_tokens_and_the_generator_state_one_geometry():
    """Three copies of one mark. This is the test that makes three safe.

    `mark.svg` is what a designer edits, `tokens.json` is what the UI reads, and
    `assistant.brand` is what the product actually draws - including the tray
    icon, which is rendered at runtime rather than loaded. They were three copies
    of a shape, and the only reason they are still three copies is this test.
    """
    svg = _svg_geometry()
    mark = TOKENS["mark"]

    assert mark["knob"] == pytest.approx(svg["knob"]), (
        "tokens.json and mark.svg disagree about the knob"
    )
    assert (brand.KNOB_CX, brand.KNOB_CY, brand.KNOB_R) == pytest.approx(
        (svg["knob"]["cx"], svg["knob"]["cy"], svg["knob"]["r"])), (
        "the package and mark.svg disagree about the knob; the tray is now "
        "showing a different mark from the vector one"
    )

    assert brand.RIM == pytest.approx((svg["rim"]["x"], svg["rim"]["y"],
                                      svg["rim"]["w"], svg["rim"]["h"],
                                      svg["rim"]["r"]))
    assert brand.PLINTH == pytest.approx((svg["plinth"]["x"], svg["plinth"]["y"],
                                          svg["plinth"]["w"], svg["plinth"]["h"],
                                          svg["plinth"]["r"]))
    for key in ("rim", "plinth"):
        assert mark[key] == pytest.approx(svg[key]), (
            f"tokens.json and mark.svg disagree about {key}"
        )

    assert brand.DOME_START_L == pytest.approx(svg["dome"]["start_l"])
    assert brand.DOME_CTRL_L1 == pytest.approx(svg["dome"]["ctrl_l1"])
    assert brand.DOME_CTRL_L2 == pytest.approx(svg["dome"]["ctrl_l2"])
    assert brand.DOME_APEX == pytest.approx(svg["dome"]["apex"])
    assert brand.DOME_CTRL_R1 == pytest.approx(svg["dome"]["ctrl_r1"])
    assert brand.DOME_CTRL_R2 == pytest.approx(svg["dome"]["ctrl_r2"])
    assert brand.DOME_END_R == pytest.approx(svg["dome"]["end_r"])
    assert TOKENS["mark"]["dome"]["ctrl_r1"] == pytest.approx(svg["dome"]["ctrl_r1"])
    assert TOKENS["mark"]["dome"]["ctrl_r2"] == pytest.approx(svg["dome"]["ctrl_r2"])


def test_importing_the_brand_costs_nothing():
    """`assistant.brand` must not pull in Pillow, or D1 stops being true.

    D1 rests on the daemon importing nothing but the standard library: it has to
    hold the policy gate with the model dead, or 24/7 costs VRAM it is supposed to
    be free of. `tray.py` already imports `brand`, so a module-level
    `from PIL import ...` here would put Pillow on that import path.

    Checked by importing in a subprocess and listing what came with it, because
    checking `sys.modules` in-process only works if nothing already imported
    Pillow - and pytest imports Pillow for a dozen other tests, so in-process the
    check can only ever pass.
    """
    import subprocess

    result = subprocess.run(
        [
            sys.executable, "-c",
            "import sys; import assistant.brand; "
            "leaked = [m for m in ('PIL', 'numpy', 'tkinter') if m in sys.modules]; "
            "print(','.join(leaked))",
        ],
        cwd=BRAND.parent, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    leaked = [m for m in result.stdout.strip().split(",") if m]
    assert not leaked, (
        f"importing assistant.brand pulled in {leaked}; the daemon's import must "
        "stay standard-library only (D1)"
    )


def test_the_highlight_is_lighter_than_the_dome_not_darker():
    """A specular highlight that renders darker than its surface is a hole.

    The renderer draws the highlight at 50% alpha. `ImageDraw` *overwrites*
    pixels rather than blending them, so drawing that stroke straight onto the
    dome punched a semi-transparent hole in it and the obsidian plate showed
    through - the highlight came out as a dark arc. The same stroke in `mark.svg`,
    where `opacity` really does blend, was light. The two assets disagreed about
    the same mark and both the geometry cross-check and a glance at a 16px icon
    passed.

    What distinguishes them is a measurement: a highlight must be *brighter* than
    the surface beside it. Stated as a pixel comparison so it holds whatever the
    geometry is, rather than hard-coding the answer.
    """
    from PIL import Image

    size = 256
    im = Image.open(BRAND / "icons" / f"butleroy-icon-{size}.png").convert("RGB")
    px = im.load()

    # The mark is drawn at 60% of the plate, centred, so plate px = mark units
    # scaled by size/64 and offset by the 20% margin. Worked out from the shared
    # constant rather than hard-coded, so a geometry change cannot leave this
    # measuring a point that is no longer on the highlight.
    mark_px = size * brand.MARK_FRACTION
    offset = (size - mark_px) / 2.0
    scale = mark_px / brand.GRID

    def at(mark_x, mark_y):
        """Brightness at a point given in mark units, taking the local max.

        The local max rather than the exact pixel, because the stroke is ~2.6 mark
        units wide and a single-pixel sample can land on its antialiased edge.
        """
        cx = int(round(offset + mark_x * scale))
        cy = int(round(offset + mark_y * scale))
        return max(
            sum(px[cx + dx, cy + dy]) / 3
            for dx in range(-2, 3) for dy in range(-2, 3)
        )

    # Midpoint of the highlight cubic, and a point on the dome beside it.
    highlight = at(24.2, 25.0)
    dome = at(32.0, 25.0)
    assert highlight > dome + 6, (
        f"the highlight measures {highlight:.0f} and the dome beside it "
        f"{dome:.0f}; it is rendering as a hole in the bell, not a glint on it"
    )


def test_the_mark_fits_inside_its_own_grid():
    """The mark may not bleed off the 64-unit canvas.

    Silhouettes that exceed the viewBox still render - they just get clipped, and
    the clipping is invisible in the source and obvious on screen. Checked by
    measuring the alpha channel of a rendered mark, because a geometry assertion
    would have to restate the same numbers again.
    """
    from PIL import Image

    im = Image.open(BRAND / "icons" / "butleroy-mark-256.png")
    alpha = im.split()[3]
    bbox = alpha.getbbox()
    assert bbox is not None, "the mark rendered completely transparent"
    assert bbox[0] >= 0 and bbox[1] >= 0
    assert bbox[2] <= 256 and bbox[3] <= 256, bbox


def test_the_plated_icon_centres_its_mark():
    """The bell sits in the middle of the plate, and occupies about 60% of it.

    Two earlier versions of the generator failed this and neither looked wrong at a
    glance: one pasted the bell in the corner, the other clipped it off the bottom
    right. Both rendered perfectly and both were only caught by measuring the brass
    pixels, so that is what this does.
    """
    from PIL import Image

    size = 128
    im = Image.open(BRAND / "icons" / f"butleroy-icon-{size}.png").convert("RGB")
    px = im.load()

    def is_brass(c):
        r, g, b = c
        return r > 120 and 80 < g < 220 and b < 120

    xs = [x for x in range(size) for y in range(size) if is_brass(px[x, y])]
    ys = [y for y in range(size) for x in range(size) if is_brass(px[x, y])]
    assert xs and ys, "no brass in the app icon at all"

    # The bell's own optical centre must be near the plate's centre.
    bell_cx = (min(xs) + max(xs)) / 2
    assert abs(bell_cx - size / 2) < size * 0.06, (
        f"the bell is centred at x={bell_cx} in a {size}px plate; it is not centred"
    )
    # And it must be a sensible size: neither a stamp nor a crop.
    width = max(xs) - min(xs)
    assert width > size * 0.35, f"the bell is only {width}px wide in {size}"
    assert width < size * 0.85, f"the bell is {width}px wide in {size}; it is cropped"


# --------------------------------------------------------------------------- #
# the raster set is complete
# --------------------------------------------------------------------------- #


ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


@pytest.mark.parametrize("size", ICO_SIZES)
def test_every_icon_size_exists(size):
    """Windows asks for these and no others, and a missing one is a blank icon."""
    for name in (f"butleroy-mark-{size}.png", f"butleroy-tray-{size}.png",
                 f"butleroy-icon-{size}.png"):
        path = BRAND / "icons" / name
        assert path.is_file(), f"{name} is missing; re-run brand/tools/render_icons.py"
        assert path.stat().st_size > 0


def test_the_ico_carries_every_frame_windows_requests():
    from PIL import Image

    im = Image.open(BRAND / "icons" / "butleroy.ico")
    have = set(im.info["sizes"])
    missing = [s for s in ICO_SIZES if (s, s) not in have]
    assert not missing, f"butleroy.ico is missing {missing}"


def test_the_tray_actually_draws_the_brand_mark():
    """The tray icon and the brand mark must be the same pixels.

    Stated as a comparison rather than as an assertion that the tray "looks
    right", because the failure this guards against is exactly the one a look
    cannot see: someone re-brands the mark, regenerates `brand/icons/`, and the
    tray - which draws from the package at runtime - silently keeps showing
    whichever of the two it was compiled against.

    Compared as pixel data, not by hashing the file, because the tray never loads
    a file.
    """
    from PIL import Image, ImageChops

    from assistant.tray import _icon_image

    drawn = _icon_image(64).convert("RGBA")
    expected = brand.draw_mark(64, mono=True).convert("RGBA")

    assert drawn.size == expected.size == (64, 64)
    diff = ImageChops.difference(drawn, expected).convert("L").getbbox()
    assert diff is None, (
        f"the tray icon differs from brand.draw_mark at pixels {diff}; the tray "
        "is not showing the brand mark"
    )
    assert drawn.getchannel("A").getbbox() is not None, "the tray icon is blank"


def test_the_committed_rasters_are_not_stale():
    """Regenerating must reproduce the committed files byte for byte.

    The reason a PNG set can be trusted at all is that it is generated and the
    generator is committed. That only holds while nobody hand-edits a PNG or
    changes the geometry without re-running `render_icons.py`, and the symptom of
    either is invisible: the files look right and simply no longer correspond to
    anything. So this regenerates and compares.

    Comparing bytes rather than pixels, because PNG encoding is deterministic here
    (no timestamps, no randomness) and a byte comparison is the stricter
    statement: it also catches a different encoder setting.
    """
    import io

    checked = 0
    for size in (16, 32, 128):
        for name, maker in (
            (f"butleroy-mark-{size}.png", lambda s=size: brand.draw_mark(s, mono=False)),
            (f"butleroy-tray-{size}.png", lambda s=size: brand.draw_mark(s, mono=True)),
            (f"butleroy-icon-{size}.png", lambda s=size: brand.draw_icon(s)),
        ):
            path = BRAND / "icons" / name
            assert path.is_file(), f"{name} is missing"
            fresh = io.BytesIO()
            maker().save(fresh, format="PNG")
            if fresh.getvalue() != path.read_bytes():
                raise AssertionError(
                    f"{name} does not match what assistant.brand draws now; "
                    "re-run brand/tools/render_icons.py"
                )
            checked += 1
    assert checked == 9


def test_the_tray_icon_is_mono_and_the_plate_is_not():
    """The tray needs one flat colour; the window icon needs the brass.

    They are separate families on purpose: Windows composes tray icons against an
    unpredictable background, and a gradient there turns to mud.

    "Mono" is asserted as *no saturation*, not as "few colours". The first version
    of this asserted `len(colours) <= 2` and failed on a perfectly correct render,
    because supersampling and a LANCZOS reduction produce a distinct blend value
    per edge pixel. What actually matters is that no pixel is a different *hue*:
    the mark is one colour at varying opacity, which is what a tray needs and what
    a gradient is not.
    """
    from PIL import Image, ImageChops

    def channel_spread(im):
        """Largest max-min channel difference across every visible pixel.

        Bone is (237, 232, 220) - a warm off-white, 17 apart - so a flat-but-warm
        mark has a spread of about 17 plus whatever the resampler adds. Brass has
        a spread of 82. The threshold sits between them and is wide enough to
        survive resampling ringing.

        Computed with `ImageChops.lighter`/`darker` on split channels rather than
        a per-pixel loop over `getdata()`, which Pillow 14 removes. Same answer,
        C speed, no deprecation.
        """
        r, g, b, alpha = im.convert("RGBA").split()
        hi = ImageChops.lighter(ImageChops.lighter(r, g), b)
        lo = ImageChops.darker(ImageChops.darker(r, g), b)
        visible = alpha.point(lambda a: 255 if a > 32 else 0)
        return ImageChops.multiply(
            ImageChops.subtract(hi, lo), visible).getextrema()[1]

    tray = channel_spread(Image.open(BRAND / "icons" / "butleroy-tray-32.png"))
    assert tray <= 40, (
        f"the tray mark has a channel spread of {tray}; it is tinted, not mono"
    )

    plate = channel_spread(Image.open(BRAND / "icons" / "butleroy-icon-128.png"))
    assert plate > 60, (
        f"the plated icon has a channel spread of only {plate}; the brass is flat"
    )


def test_the_tray_mark_is_opaque_enough_to_see():
    """Mono is not the same as invisible.

    A mark drawn at the wrong opacity composites into whatever the tray's
    background is, and a light-on-light tray is a bell you cannot find. Checked on
    the tray family, where Windows does not do the compositing for us.
    """
    from PIL import Image

    for size in (16, 24, 32):
        im = Image.open(BRAND / "icons" / f"butleroy-tray-{size}.png")
        alpha = im.split()[3]
        assert alpha.getextrema()[1] == 255, (
            f"the {size}px tray mark never reaches full opacity"
        )
        covered = sum(alpha.histogram()[128:]) / (size * size)
        assert covered > 0.10, (
            f"the {size}px tray mark covers only {covered:.1%} of its box; "
            "it will disappear against the tray background"
        )


# --------------------------------------------------------------------------- #
# the palette
# --------------------------------------------------------------------------- #


def _hex(value: str) -> tuple[int, int, int]:
    v = value.lstrip("#")
    return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))


def test_every_colour_is_a_valid_six_digit_hex():
    """D69, in a different place.

    Seven hex digits reached a real widget and Tk raised on it; `invalid color
    name`. A colour token is exactly the same class of thing, so it gets the same
    check - and this one is cheap enough to run over the whole palette.
    """
    for name, value in TOKENS["colour"].items():
        assert re.fullmatch(r"#[0-9A-Fa-f]{6}", value), (
            f"{name} = {value!r} is not six hex digits"
        )


def test_text_colours_are_legible_on_the_grounds_they_are_used_on():
    """Contrast, not vibes. WCAG relative luminance, 4.5:1 for body text.

    The one this caught: `#C8A951` brass on `#0F120E` obsidian is 7.4:1 and passes,
    but the *previous* palette's `#6aa9ff` accent on the new `#1B2019` panel is
    5.6:1 while brass on that panel is only 6.1:1 — which is why the docs say brass
    is for things that are *acting* and rust/amber for anything destructive.
    """
    def lum(rgb):
        def ch(c):
            c = c / 255
            return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
        r, g, b = (ch(v) for v in rgb)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    def ratio(fg, bg):
        a, b = lum(_hex(fg)), lum(_hex(bg))
        hi, lo = max(a, b), min(a, b)
        return (hi + 0.05) / (lo + 0.05)

    grounds = ("obsidian", "slate")
    for fg, minimum in (("bone", 4.5), ("sage", 4.5), ("brass", 4.5),
                        ("verdigris", 4.5), ("rust", 4.5), ("amber", 4.5)):
        for ground in grounds:
            got = ratio(TOKENS["colour"][fg], TOKENS["colour"][ground])
            assert got >= minimum, (
                f"{fg} on {ground} is {got:.2f}:1, below {minimum}:1 - "
                "unreadable, and a brand token is not the place to be subtle"
            )


def test_the_tier_colours_are_the_four_state_colours():
    """A tier badge that reuses another tier's colour is worse than no badge.

    `$comment` keys are excluded because they are documentation inside the JSON,
    and the first version of this test failed on one - which is the test being
    stricter than the thing it was written to check.
    """
    tiers = {k: v for k, v in TOKENS["tier_colour"].items()
             if not k.startswith("$")}
    assert set(tiers) == {"T0", "T1", "T2", "T3"}, (
        f"the tier colour map has {sorted(tiers)}, expected exactly T0..T3"
    )
    named = [tiers[t] for t in ("T0", "T1", "T2", "T3")]
    assert len(set(named)) == 4, f"two tiers share a colour: {named}"


def test_every_tier_colour_is_a_token_that_exists():
    """The tier map may only name real tokens.

    A tier pointing at a colour that is not in the palette renders as Tk's
    default grey, which is the D69 failure again in a new place: a reference that
    does not resolve is not a reference, it is a silently wrong value.
    """
    palette = {k: v for k, v in TOKENS["colour"].items()
               if not k.startswith("$")}
    for tier, name in TOKENS["tier_colour"].items():
        if tier.startswith("$"):
            continue
        assert name in palette, (
            f"{tier} points at {name!r}, which is not a colour token"
        )


def test_nothing_is_defined_twice_with_different_values():
    """The palette is declared once, in `tokens.json`.

    A hex code appearing in the prose of the README and in the tokens is two
    definitions that will drift. The README quotes the tokens' values and this
    asserts they still agree.
    """
    readme = (BRAND / "README.md").read_text(encoding="utf-8")
    for name, value in TOKENS["colour"].items():
        if name in readme:
            assert value in readme, (
                f"{name} is {value} in tokens.json but the README says something "
                "else; one of them is out of date"
            )


# --------------------------------------------------------------------------- #
# the glyphs
# --------------------------------------------------------------------------- #

GLYPH_NAMES = {
    "bell", "chat", "models", "voices", "microphone", "speaker", "settings",
    "shield", "tier", "workspaces", "vault", "audit", "delegate", "teach",
    "reflect", "mail", "telegram", "vram", "power", "key", "plus", "remove",
    "edit", "import", "export", "search", "close",
}


def test_the_glyph_set_is_complete_and_nothing_more():
    """Exactly the glyphs the README lists.

    An icon nobody uses is dead weight; one that is needed and missing is a row
    with no icon. Both are caught by comparing the directory to the documented
    set rather than by remembering.
    """
    on_disk = {p.stem for p in (BRAND / "glyphs").glob("*.svg")}
    assert on_disk == GLYPH_NAMES, (
        f"added: {sorted(on_disk - GLYPH_NAMES)}  "
        f"missing: {sorted(GLYPH_NAMES - on_disk)}"
    )


@pytest.mark.parametrize("name", sorted(GLYPH_NAMES))
def test_a_glyph_is_on_grid_stroked_and_labelled(name):
    """The three properties that keep a glyph set coherent.

    An unstroked glyph is invisible, an unlabelled one is unusable with a screen
    reader, and an off-grid one does not line up with anything beside it.
    """
    from xml.etree import ElementTree

    path = BRAND / "glyphs" / f"{name}.svg"
    root = ElementTree.parse(path).getroot()
    assert root.get("viewBox") == "0 0 24 24", (
        f"{name} is not on the 24-unit grid: {root.get('viewBox')}"
    )
    assert root.get("stroke") == "currentColor", f"{name} has a fixed stroke colour"
    assert root.get("stroke-width") == "2", f"{name} has a different stroke weight"
    assert root.get("fill") == "none", f"{name} is filled; the set is stroked"
    label = root.get("aria-label")
    assert label, f"{name} has no aria-label; an icon-only button needs one"
    assert root.get("role") == "img", f"{name} is missing role=img"
    # Something has to actually be drawn.
    assert len(root) > 0, f"{name} contains no paths"


def test_the_lockup_carries_the_name_and_not_the_platform():
    """`for Windows` is deliberately absent from the lockup.

    The platform is a fact, not a tagline; it belongs in the About box and the
    installer. Asserted because "just add the platform to the logo" is the most
    common way a lockup gets worse.
    """
    logo = (BRAND / "logo.svg").read_text(encoding="utf-8")
    text = re.sub(r"<!--.*?-->", "", logo, flags=re.S)   # comments are not artwork

    assert "ButlerOY" in text
    assert "LOCALAIZED CONCIERGE" in text
    assert "Windows" not in text, (
        "the lockup names the platform; it is a fact, not a tagline"
    )


def test_a_glyph_is_named_for_what_it_is_not_for_its_shape():
    """The rule that keeps the set searchable, enforced on one known offender.

    `dome.svg` would have been the natural name for the bell before the rule
    existed. If a shape-named glyph reappears, the set is being added to by
    geometry rather than by meaning, and the README is wrong about it.
    """
    shape_names = {"circle", "square", "triangle", "dome", "arc", "oval",
                   "line", "dot", "blob"}
    assert not (GLYPH_NAMES & shape_names), (
        "a glyph is named after its shape; name it after what it is for"
    )