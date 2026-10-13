"""Which model answers, read from a file instead of a Python literal.

D74. Until now the two tiers were literals at `brain_server.py:43-72`:

    FAST_MODEL  = ROOT / "models" / "Qwen3.5-4B-...gguf"
    TIERS = {"fast": {...}, "deep": {...}}

which is fine for a system with one fixed configuration and is the reason there has
never been any way to *edit* a model: the only thing that can change it is a
programmer with a text editor. This module is the file that makes the models
surface possible, and it is deliberately the smallest thing that could work.

**The literals are still here, and that is the point.** `brain_server.TIERS` remains
the default; `load()` reads `state/models.json` over the top of it and falls back
when the file is absent, unreadable, or names a model that is not on disk. So a
machine that has never opened the settings window behaves exactly as it did before,
and a corrupt config cannot stop the assistant from answering — it degrades to the
built-in default and says so in `provenance`.

Four properties this has to have, each of which was a bug somewhere in this
project's history:

1. **A missing file is not an error.** The daemon must start on a fresh machine.
2. **A corrupt file is not an error either, but it is *visible*.** A settings window
   that silently discards what the user typed is worse than one that refuses to
   save, because the user believes it took. `load()` reports what it fell back to.
3. **A model that is not on disk is refused at save time, not at load time.** By the
   time a load fails the user has a 5 GiB lease outstanding and a turn in flight.
4. **No absolute paths escape into the file.** The config stores a *filename
   relative to models/*, not `G:\\localaized\\models\\...`, because the checkout is
   not guaranteed to be at the same path on the next machine — and a config that
   hard-codes it is a config that breaks on a move.

Stdlib only. No model runtime is imported here, which is D1: this is read by the
daemon, and the daemon must hold the gate with the model dead.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Where the models live, and where the config lives. Both derived from the repo
#: root for the same reason as everywhere else: a second hard-coded `G:\\localaized`
#: is a second thing to fix when the checkout moves (D73).
ROOT = Path(__file__).resolve().parent.parent.parent
MODELS_DIR = ROOT / "models"
DEFAULT_CONFIG_PATH = ROOT / "state" / "models.json"

#: The suffix a llama.cpp model must have. Checked at save time so a user cannot
#: point the fast tier at a spreadsheet and get a runtime error six seconds into a
#: lease rather than a red row in the settings window.
MODEL_SUFFIX = ".gguf"

#: A filename safe to store. Deliberately conservative: this string is joined to
#: MODELS_DIR and the result is opened by a model loader, so `..`, a separator or a
#: drive letter must all be refused rather than normalised. Refusing is the only
#: version of this that cannot be bypassed by a clever encoding.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\- ]{0,180}\.gguf$")

#: A lease ceiling. Refused above this rather than clamped, because silently
#: asking for less than the user asked for produces a denial from the arbiter that
#: reads as "the model is too big" when the real answer is "the number was wrong".
#: 12 GiB is the card this was measured on; the ceiling exists to catch a typo, not
#: to encode a policy.
MAX_LEASE_BYTES = 24 * 1024 ** 3


class ModelConfigError(ValueError):
    """Something in a models config was wrong. Carries a message fit for a user."""


@dataclass
class TierSetting:
    """One tier: which model answers it, and how much VRAM it may ask for.

    `name` is the filename relative to `models/`, never an absolute path. Holding
    it that way round is what makes the config portable; `resolved()` is the only
    thing that joins it to the filesystem.
    """

    model: str
    need_bytes: int

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not self.model:
            raise ModelConfigError("a tier needs a model name")
        if not self.model.endswith(MODEL_SUFFIX):
            raise ModelConfigError(
                f"{self.model!r} is not a {MODEL_SUFFIX} file"
            )
        if not _SAFE_NAME.match(self.model):
            raise ModelConfigError(
                f"{self.model!r} is not a plain filename. It must be a single file "
                f"inside models/, with no path separators."
            )
        if not isinstance(self.need_bytes, int) or isinstance(self.need_bytes, bool):
            raise ModelConfigError("need_bytes must be a whole number of bytes")
        if self.need_bytes <= 0:
            raise ModelConfigError("need_bytes must be greater than zero")
        if self.need_bytes > MAX_LEASE_BYTES:
            raise ModelConfigError(
                f"need_bytes is {self.need_bytes:,} which is above the "
                f"{MAX_LEASE_BYTES:,} ceiling; that is a typo, not a lease"
            )

    def resolved(self, models_dir: Path | None = None) -> Path:
        """The absolute path, joined *now* rather than stored."""
        return (models_dir or MODELS_DIR) / self.model


@dataclass
class ModelsConfig:
    """The whole file: named tiers, plus where it came from.

    `provenance` is the field that makes a silent fallback visible. It is a string
    rather than a bool because the useful answer to "why is the fast tier the 4B
    when I set it to the 0.8B?" is a sentence, and a bool forces the caller to
    reconstruct one from state it does not have.
    """

    tiers: dict[str, TierSetting] = field(default_factory=dict)
    path: Path | None = None
    #: "file" - read from models.json. "defaults" - no file, using the literals.
    #: "defaults+unreadable: <why>" - a file exists but was not usable.
    provenance: str = "defaults"

    def tier(self, name: str) -> TierSetting | None:
        return self.tiers.get(name)

    def as_dict(self) -> dict[str, Any]:
        return {
            name: {"model": t.model, "need_bytes": t.need_bytes}
            for name, t in sorted(self.tiers.items())
        }


#: The built-in default, mirroring `brain_server.TIERS`.
#:
#: Held here rather than imported from `brain_server` because importing that
#: module pulls in `llama_cpp` on some paths, and this file is read by the daemon.
#: The duplication is the price of D1, and `test_model_config.py` asserts the two
#: agree — see the note in that file about why an import would be worse.
def defaults() -> dict[str, TierSetting]:
    return {
        "fast": TierSetting(
            model="Qwen3.5-4B-Uncensored-HauhauCS-Aggressive-Q8_0.gguf",
            need_bytes=7 * 1024 ** 3,
        ),
        "deep": TierSetting(
            model="Qwen3.5-9B-The-Defiant-Fable-Uncnr-Heretic-NEO-MAX-IQ4_XS.gguf",
            need_bytes=7 * 1024 ** 3,
        ),
    }


def available_models(models_dir: Path | None = None) -> list[dict[str, Any]]:
    """Every model on disk, with its size, for the settings window to list.

    Reads the directory rather than a manifest, so a model the user copied in by
    hand shows up without being registered anywhere. `mmproj-*` files are excluded:
    they are projector weights for a multimodal model, not a model, and offering
    one as "the fast tier" produces a child that loads and then cannot speak.
    """
    directory = models_dir or MODELS_DIR
    if not directory.is_dir():
        return []

    found: list[dict[str, Any]] = []
    for path in sorted(directory.glob(f"*{MODEL_SUFFIX}")):
        if not path.is_file():
            continue
        if path.name.startswith("mmproj"):
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        found.append({
            "name": path.name,
            "bytes": size,
            # The estimate the codebase already uses elsewhere (D1 measured 4.17 GiB
            # of file occupying 4.92 GiB on the card). Reported so the settings
            # window can show what a lease will actually cost before the user
            # commits to it, rather than after a denial.
            "est_vram_bytes": int(size * 1.18),
        })
    return found


def load(path: Path | str | None = None,
         models_dir: Path | None = None) -> ModelsConfig:
    """Read the config, degrading to the defaults rather than failing.

    Every failure mode here returns `defaults()` with `provenance` saying why. The
    daemon calls this on start-up, and a daemon that will not start because a
    settings file has a stray comma is a daemon the user cannot fix without a text
    editor — which is the thing being removed.

    A tier naming a model that is not on disk is also degraded, per-tier rather than
    wholesale: one bad tier should not cost the user the other one. That is
    recorded in `provenance` too.
    """
    target = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    base = defaults()

    if not target.is_file():
        return ModelsConfig(tiers=base, path=target, provenance="defaults")

    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return ModelsConfig(
            tiers=base, path=target,
            provenance=f"defaults+unreadable: {type(exc).__name__}: {exc}",
        )

    if not isinstance(raw, dict):
        return ModelsConfig(
            tiers=base, path=target,
            provenance="defaults+unreadable: top level is not an object",
        )

    tiers_raw = raw.get("tiers")
    if not isinstance(tiers_raw, dict):
        return ModelsConfig(
            tiers=base, path=target,
            provenance="defaults+unreadable: 'tiers' is missing or not an object",
        )

    directory = models_dir or MODELS_DIR
    resolved: dict[str, TierSetting] = {}
    problems: list[str] = []
    for name, spec in tiers_raw.items():
        if not isinstance(name, str) or not name:
            problems.append(f"tier name {name!r} is not a string")
            continue
        if not isinstance(spec, dict):
            problems.append(f"tier {name!r} is not an object")
            continue
        try:
            tier = TierSetting(
                model=str(spec.get("model", "")),
                need_bytes=int(spec.get("need_bytes", 0)),
            )
        except (ModelConfigError, TypeError, ValueError) as exc:
            problems.append(f"tier {name!r}: {exc}")
            continue
        # Presence is checked here rather than at load-of-model, so a typo is
        # reported by `load()` instead of by a child that has already leased VRAM.
        if not tier.resolved(directory).is_file():
            problems.append(
                f"tier {name!r} names {tier.model!r}, which is not in "
                f"{directory.name}/"
            )
            continue
        resolved[name] = tier

    if not resolved:
        return ModelsConfig(
            tiers=base, path=target,
            provenance="defaults+unreadable: " + ("; ".join(problems) or "no tiers"),
        )

    # Anything the file did not mention keeps its default, so adding a tier later
    # does not require every existing config to be edited.
    merged = {**base, **resolved}
    provenance = "file" if not problems else "file+partial: " + "; ".join(problems)
    return ModelsConfig(tiers=merged, path=target, provenance=provenance)


def save(config: ModelsConfig, path: Path | str | None = None,
         models_dir: Path | None = None) -> Path:
    """Write the config atomically, after checking every model exists.

    Atomic because a half-written `models.json` is one of the unreadable states
    `load()` has to degrade from, and the window to make that impossible is smaller
    than the window to recover from it: write to a sibling temp file and rename,
    which on Windows is atomic within a volume.

    The existence check happens *here*, not in `load()`, so the settings window can
    refuse the save and show the user which row is wrong while they are still
    looking at the form.

    `models_dir` takes the directory to check against, for the same reason `load()`
    does: otherwise a caller that read a config out of somewhere other than the
    repository would have its save refused against a different directory than it
    read from, and the error would name a model that demonstrably exists.
    """
    target = Path(path) if path is not None else (config.path or DEFAULT_CONFIG_PATH)
    directory = models_dir or MODELS_DIR

    for name, tier in sorted(config.tiers.items()):
        if not tier.resolved(directory).is_file():
            raise ModelConfigError(
                f"tier {name!r} names {tier.model!r}, which is not in "
                f"{directory.name}/. Pick a model that exists, or copy the file "
                f"in first."
            )

    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "_comment": (
            "ButlerOY model configuration. Tier -> model filename (relative to "
            "models/) and the VRAM lease ceiling in bytes. Written by the settings "
            "window; safe to hand-edit, and a file that cannot be read falls back "
            "to the built-in defaults rather than stopping the daemon."
        ),
        "tiers": config.as_dict(),
    }

    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    # replace() rather than rename(): on Windows, rename() fails if the destination
    # exists, and this file always exists after the first save.
    tmp.replace(target)
    return target


def to_brain_tiers(config: ModelsConfig,
                    models_dir: Path | None = None) -> dict[str, dict[str, Any]]:
    """The shape `brain_server.TIERS` uses, for the child to consume.

    A copy rather than a mutation of the imported dict: `brain_server.TIERS` is a
    module-level literal that other tests read, and having this change it as a side
    effect would make the result of one call depend on whether another had been
    made. Returns fresh values every time.

    `models_dir` for the same reason `save()` takes it: resolving against the wrong
    directory produces a path the child cannot open, and the error would then name
    a file that exists.
    """
    directory = models_dir or MODELS_DIR
    return {
        name: {"model": tier.resolved(directory), "need_bytes": tier.need_bytes}
        for name, tier in config.tiers.items()
    }


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "MODELS_DIR",
    "ModelConfigError",
    "ModelsConfig",
    "TierSetting",
    "available_models",
    "defaults",
    "load",
    "save",
    "to_brain_tiers",
]