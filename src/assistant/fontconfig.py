"""Overlay typography: family, size, alignment. All client-side.

The daemon never sees these: which face the transcript wears is not a fact
about the assistant. Storage is `state/fonts.json`, read by the overlay at
build and at every show, so a choice applies the next time the prompt window
opens — and sooner if it is already open, because show reconfigures too.

Absent or broken file means *today's hardcoded faces stay*, not a fallback
constant that could drift from them. There is deliberately no DEFAULTS dict:
the settings tab pre-fills from the live Tk faces, so "what you have" is
always the starting point, never a guess.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

VALID_ALIGN = ("left", "center", "right")
MIN_SIZE = 8
MAX_SIZE = 48


def _repo_state_dir() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "state"


def config_path(state_dir: Path | str | None = None) -> Path:
    return (Path(state_dir) if state_dir else _repo_state_dir()) / "fonts.json"


def load(state_dir: Path | str | None = None) -> dict[str, Any] | None:
    """The saved typography, or None when there is nothing valid to apply.

    Per-field strictness would leave half a face applied (a size with no
    family); anything invalid falls back whole, to the hardcoded faces.
    """
    try:
        raw = json.loads(config_path(state_dir).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - missing or broken file is not an error
        return None
    if not isinstance(raw, dict):
        return None
    family = raw.get("family")
    size = raw.get("size")
    align = raw.get("align")
    if not isinstance(family, str) or not family.strip():
        return None
    if isinstance(size, bool) or not isinstance(size, int):
        return None
    if not (MIN_SIZE <= size <= MAX_SIZE):
        return None
    if align not in VALID_ALIGN:
        return None
    return {"family": family.strip(), "size": size, "align": align}


def save(state_dir: Path | str | None, family: str, size: int,
         align: str) -> dict[str, Any]:
    """Validate strictly, write, and return what was written.

    Strict here because the caller is a settings tab that can show the
    refusal next to the field: a typo near typography must not land
    somewhere near.
    """
    if not isinstance(family, str) or not family.strip():
        raise ValueError("font family must be a non-empty name")
    if isinstance(size, bool) or not isinstance(size, int):
        raise ValueError(f"font size must be a whole number of points, not {size!r}")
    if not (MIN_SIZE <= size <= MAX_SIZE):
        raise ValueError(
            f"font size must be {MIN_SIZE}..{MAX_SIZE} points, not {size}")
    if align not in VALID_ALIGN:
        raise ValueError(
            f"alignment must be one of {', '.join(VALID_ALIGN)}, not {align!r}")
    saved = {"family": family.strip(), "size": size, "align": align}
    target = config_path(state_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(saved, indent=2), encoding="utf-8")
    return saved
