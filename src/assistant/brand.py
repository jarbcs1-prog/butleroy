"""The ButlerOY mark, as code.

Why this is in the package and not in `brand/tools/`: the tray icon is drawn from
this geometry at runtime, and `pyproject.toml` packages `src/` only. A tray that
imported from `brand/tools/render_icons.py` would work from a checkout and fail
with ImportError for anyone who actually installed the thing - which is the
failure mode INSTALL.md's install-of-record is supposed to make impossible.

So the geometry lives here, `render_icons.py` is a thin CLI over it, and there is
exactly one copy of the shape. `brand/` holds the specification and the generated
artefacts; this holds the numbers the product actually draws.

The SVG in `brand/mark.svg` is a third copy, deliberately: a designer needs a file
they can open in a vector editor, and parsing SVG would mean depending on a
rasteriser this project deliberately does not have (D66 - core deps are pystray
and pillow). Three copies is only safe because `tests/test_brand.py` asserts all
three agree, by parsing the SVG rather than restating it.

**Import cost: none.** No third-party module is imported at import time - Pillow
is imported inside the drawing functions. This matters: D1 rests on
`import assistant.daemon` pulling in nothing but the standard library, and a
`from PIL import ...` at the top of a module that anything else imports would
spend that for a tray icon.
"""

from __future__ import annotations

# --- the geometry, in mark units ----------------------------------------------
# Named rather than packed into a tuple: the first version indexed a positional
# tuple and read the wrong control point, so the dome came out as a filled
# rectangle. tests/test_brand.py compares every number here against mark.svg,
# which is what keeps the three copies from drifting apart.

KNOB_CX, KNOB_CY, KNOB_R = 32.0, 12.4, 4.3

# The dome is one path in the SVG, `M 17 40 C ... Z`, split here into the two
# cubics it is made of so both assets describe the same curve.
DOME_BASE_Y = 40.0
DOME_LEFT_X, DOME_RIGHT_X = 17.0, 47.0
DOME_START_L = (DOME_LEFT_X, DOME_BASE_Y)
DOME_CTRL_L1 = (17.0, 25.6)
DOME_CTRL_L2 = (23.6, 17.6)
DOME_APEX = (32.0, 17.6)
DOME_CTRL_R1 = (40.4, 17.6)
DOME_CTRL_R2 = (47.0, 25.6)
DOME_END_R = (DOME_RIGHT_X, DOME_BASE_Y)

RIM = (10.5, 38.2, 43.0, 8.4, 4.2)      # x, y, w, h, radius
PLINTH = (15.0, 48.6, 34.0, 6.0, 3.0)   # x, y, w, h, radius
HIGHLIGHT = ((21.5, 35.5), (21.5, 26.5), (25.8, 21.5), (30.0, 20.6), 2.6)

# The share of the plate the mark occupies. A design constant like RIM and
# PLINTH, and the test suite reads it to work out where the mark landed in the
# rendered icon.
MARK_FRACTION = 0.60

# The plate's corner radius, as a fraction of its side. 22.6% is the Windows 11
# "rounded rect" proportion and matches what the OS does to an .ico at medium
# sizes.
PLATE_RADIUS = 0.226

GRID = 64          # the mark's design grid
SS = 8             # supersample factor

# The palette, as ramps rather than as the single hex values the SVGs use: the
# bell is lit from above, and a flat fill reads as a sticker.
BRASS = ((0.00, (232, 206, 134)), (0.42, (200, 169, 81)),
         (0.78, (156, 127, 53)), (1.00, (126, 101, 40)))
BRASS_RIM = ((0.00, (217, 188, 108)), (0.55, (176, 143, 60)),
             (1.00, (111, 88, 34)))
OBSIDIAN = ((0.00, (27, 32, 25)), (1.00, (12, 15, 11)))
BONE = (237, 232, 220)


# --- pure geometry, no Pillow --------------------------------------------------


def _bezier(p0, p1, p2, p3, steps=24):
    """Sample one cubic. Cubics, because the dome is a cubic in the SVG and a
    different curve here would mean the two assets drift apart."""
    out = []
    for i in range(steps + 1):
        t = i / steps
        u = 1 - t
        out.append((
            u**3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t**3 * p3[0],
            u**3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t**3 * p3[1],
        ))
    return out


def _dome_points():
    """The dome outline, left flank then right flank.

    Walks the two cubics of `M 17 40 C 17 25.6, 23.6 17.6, 32 17.6
    C 40.4 17.6, 47 25.6, 47 40 Z`. The closing edge along the base is implied
    by `ImageDraw.polygon`, which is what `Z` means here.
    """
    left = _bezier(DOME_START_L, DOME_CTRL_L1, DOME_CTRL_L2, DOME_APEX)
    right = _bezier(DOME_APEX, DOME_CTRL_R1, DOME_CTRL_R2, DOME_END_R)
    return left + right


def _ramp(stops, t):
    """Colour at t through a stop list. Clamped at both ends rather than
    wrapping: a wrapped ramp puts the highlight at the bottom of the knob."""
    t = max(0.0, min(1.0, t))
    for i in range(len(stops) - 1):
        a, ca = stops[i]
        b, cb = stops[i + 1]
        if a <= t <= b:
            f = 0.0 if b == a else (t - a) / (b - a)
            return tuple(int(round(ca[j] + (cb[j] - ca[j]) * f)) for j in range(3))
    return stops[-1][1]


def _vertical_ramp(stops, y, y0, y1):
    return _ramp(stops, 0.0 if y1 == y0 else (y - y0) / (y1 - y0))


# --- drawing -------------------------------------------------------------------


def draw_mark(size: int, *, mono: bool | None = None):
    """The bare mark at `size` px. Transparent background.

    `mono` forces single-colour (BONE) rather than brass, for tray use. It
    defaults to `size <= 32`, because that is where the brass gradient stops
    being metal and starts being mud.

    Supersampled 8x and reduced. That is the whole trick and it is why this is
    worth existing rather than shipping the SVGs: the dome is a filled cubic, and
    a cubic filled at 16 px directly is a staircase, while filled at 128 px and
    reduced it is a curve.
    """
    from PIL import Image, ImageDraw

    if mono is None:
        mono = size <= 32
    big = size * SS
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = big / GRID

    def P(x, y):
        return (x * s, y * s)

    if mono:
        fill = BONE + (255,)
        d.ellipse([*P(KNOB_CX - KNOB_R, KNOB_CY - KNOB_R),
                   *P(KNOB_CX + KNOB_R, KNOB_CY + KNOB_R)], fill=fill)
        d.polygon([P(*p) for p in _dome_points()], fill=fill)
        d.rounded_rectangle([*P(RIM[0], RIM[1]), *P(RIM[0] + RIM[2], RIM[1] + RIM[3])],
                            radius=RIM[4] * s, fill=fill)
        d.rounded_rectangle([*P(PLINTH[0], PLINTH[1]),
                             *P(PLINTH[0] + PLINTH[2], PLINTH[1] + PLINTH[3])],
                            radius=PLINTH[4] * s, fill=fill)
    else:
        top, bot = KNOB_CY - KNOB_R, KNOB_CY + KNOB_R
        d.ellipse([*P(KNOB_CX - KNOB_R, top), *P(KNOB_CX + KNOB_R, bot)],
                  fill=_vertical_ramp(BRASS, top, top, bot) + (255,))

        dome = _dome_points()
        ys = [p[1] for p in dome]
        d.polygon([P(*p) for p in dome],
                  fill=_vertical_ramp(BRASS, min(ys), min(ys), max(ys)) + (255,))

        d.rounded_rectangle([*P(RIM[0], RIM[1]), *P(RIM[0] + RIM[2], RIM[1] + RIM[3])],
                            radius=RIM[4] * s,
                            fill=_vertical_ramp(BRASS_RIM, RIM[1], RIM[1],
                                                RIM[1] + RIM[3]) + (255,))
        d.rounded_rectangle([*P(PLINTH[0], PLINTH[1]),
                             *P(PLINTH[0] + PLINTH[2], PLINTH[1] + PLINTH[3])],
                            radius=PLINTH[4] * s,
                            fill=_vertical_ramp(BRASS_RIM, PLINTH[1], PLINTH[1],
                                                PLINTH[1] + PLINTH[3]) + (255,))

        # The highlight. Only above 24 px: at 16 it is one grey pixel and costs
        # more legibility than it adds.
        #
        # Drawn on its own layer and composited. NOT drawn straight onto `img`:
        # `ImageDraw` overwrites pixels, it does not blend them, so a
        # semi-transparent `fill` punches a hole in the dome and the obsidian
        # plate shows through. The first version did exactly that and the
        # highlight rendered as a dark arc across the bell - while the identical
        # stroke in mark.svg, where `opacity` genuinely blends, rendered as a
        # light one. Same source, opposite result, which is the drift a geometry
        # cross-check cannot see because opacity is not geometry.
        # tests/test_brand.py measures the rendered pixels and catches it.
        if size >= 48:
            (p0, c1, c2, p3, w) = HIGHLIGHT
            hl = Image.new("RGBA", (big, big), (0, 0, 0, 0))
            ImageDraw.Draw(hl).line([P(*p) for p in _bezier(p0, c1, c2, p3)],
                                    fill=(246, 231, 188, 128),
                                    width=max(1, int(w * s)), joint="curve")
            img.alpha_composite(hl)

    return img.resize((size, size), Image.LANCZOS)


def draw_icon(size: int):
    """The plated app icon: obsidian rounded square, brass bell.

    The tray uses `draw_mark` instead. Windows composes tray icons against an
    unpredictable background, and a filled shape with a hard edge survives that
    where a lit gradient does not.
    """
    from PIL import Image, ImageDraw

    big = size * SS
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = big * PLATE_RADIUS
    top = _ramp(OBSIDIAN, 0.0)
    bot = _ramp(OBSIDIAN, 1.0)
    # Vertical plate gradient, drawn as horizontal bands, because Pillow has no
    # gradient fill and a per-row fill is `size * 8` draws per icon.
    band = Image.new("RGBA", (big, big))
    bd = ImageDraw.Draw(band)
    for y in range(big):
        f = y / max(1, big - 1)
        bd.line([(0, y), (big, y)],
                fill=tuple(int(round(top[j] + (bot[j] - top[j]) * f))
                            for j in range(3)) + (255,))
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, big - 1, big - 1], radius=r, fill=255)
    img.paste(band, (0, 0), mask)

    # The brass hairline that keeps the plate from dissolving into a dark taskbar.
    d.rounded_rectangle([big * 0.004, big * 0.004, big * 0.996, big * 0.996],
                        radius=r, outline=(200, 169, 81, 46),
                        width=max(1, int(big * 0.006)))

    # Everything above works in `big` space, so the mark is sized and inset in
    # `big` too. Two earlier versions got this wrong in opposite directions - a
    # final-size mark pasted into a supersampled canvas (an 11 px bell in the
    # corner) and a full-size mark with a 20% inset (clipped off the
    # bottom-right). Both were found by measuring the brass pixels, not by
    # looking.
    #
    # `MARK_FRACTION` is the single number both derive from, so the mark and its
    # inset cannot disagree.
    mark_px = int(round(big * MARK_FRACTION))
    inset = int(round((big - mark_px) / 2))
    mark = draw_mark(mark_px)
    # `dest=` is named. `alpha_composite(im, (x, y))` passes the tuple as
    # `source`, which silently pastes from an offset instead of to one.
    img.alpha_composite(mark, dest=(inset, inset))
    return img.resize((size, size), Image.LANCZOS)


def tray_image(size: int = 64):
    """The mark as the tray draws it: mono, at `size`.

    Named separately so the tray never has to know that `mono=True` is a policy
    decision rather than a rendering option.
    """
    return draw_mark(size, mono=True)