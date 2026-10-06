"""Write the Butleroy raster set. A CLI over `assistant.brand`; no geometry here.

    .\\.venv\\Scripts\\python.exe brand\\tools\\render_icons.py

All the drawing lives in `src/assistant/brand.py` because the tray draws the mark
at runtime and `src/` is the only thing that gets packaged - see that module's
docstring. This file is only the part that touches the filesystem.

Output is deterministic: no timestamps, no randomness, no dictionary ordering
dependence, so re-running produces byte-identical files and a rebuild shows no
diff. That is what makes the generated set reviewable - the alternative to
committing PNGs nobody can read is not committing them, it is committing the
generator and being able to prove the PNGs still match it.
"""

from __future__ import annotations

import sys
from pathlib import Path

BRAND = Path(__file__).resolve().parent.parent
OUT = BRAND / "icons"

sys.path.insert(0, str(BRAND.parent / "src"))

from assistant.brand import draw_icon, draw_mark  # noqa: E402

# Every size the OS asks for. 20 and 40 are for DPI-scaled taskbars that request
# them directly; a family missing them falls back to the nearest neighbour, which
# is how a tray icon ends up blurry at exactly the DPI it is most often seen at.
SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)

# The frames Windows actually requests from an .ico. Pillow resamples the 256 base
# down to each, which is right here because the base was itself drawn
# supersampled: reducing a supersampled render is still a supersampled render.
ICO_SIZES = ((16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48),
             (64, 64), (128, 128), (256, 256))


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    written = []

    for size in SIZES:
        # Two families, deliberately: `butleroy-tray-*` is what the tray loads,
        # `butleroy-mark-*` is the bare brass mark for documents and splash.
        draw_mark(size, mono=True).save(OUT / f"butleroy-tray-{size}.png")
        written.append(f"butleroy-tray-{size}.png")

        draw_mark(size, mono=False).save(OUT / f"butleroy-mark-{size}.png")
        written.append(f"butleroy-mark-{size}.png")

        draw_icon(size).save(OUT / f"butleroy-icon-{size}.png")
        written.append(f"butleroy-icon-{size}.png")

    draw_icon(256).save(OUT / "butleroy.ico", format="ICO", sizes=ICO_SIZES)
    written.append("butleroy.ico")

    # A tray-ready copy at a predictable name, so anyone wanting the static file
    # does not have to guess which of the ten sizes the tray uses.
    #
    # Written again rather than `Path.replace()`d from `butleroy-tray-32.png`:
    # replace() is a *move*, so the first version of this line consumed the 32px
    # file it was meant to copy and left the set with no 32px tray in it.
    draw_mark(32, mono=True).save(OUT / "tray.png")
    written.append("tray.png")

    print(f"{len(written)} files written to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())