"""Which voice speaks, and which voices exist. D76.

`speech.py` had three hardcoded voices in `VOICE_PATHS` and one `DEFAULT_VOICE`
constant. That is the same shape the models had before D74: nothing could be
*chosen*, and nothing could be added. This module is the catalogue.

Three sources of truth, deliberately, because a voice can arrive three ways and each
needs to be visible:

  1. **Known** - the piper voices in `speech.VOICE_PATHS`, downloadable on demand.
     `speech.py` remains the authority on where each one lives in the HF repo, because
     the nesting is not uniform and that knowledge is hard-won (its comment says so).
  2. **Installed** - a `.onnx` and a `.onnx.json` pair in `models/voices/`, discovered
     by reading the directory. That is how an *imported* voice appears, and reading
     the directory rather than a manifest means a file the user dropped in by hand
     shows up without being registered anywhere.
  3. **Chosen** - the default, in `state/voices.json`.

Discovery rather than a registry, for the same reason as D74: a manifest is a second
place for the truth to live, and the one that goes stale is the manifest.

**The one op that reads a user-supplied path.** `import_voice` takes a source path,
which is genuinely different from anything the model ops do - they only ever name
files inside `models/`. See the note on that function for why it is acceptable and
what actually protects it.

Stdlib only, and no model runtime at import: this is read by the daemon (D1).
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent
VOICES_DIR = ROOT / "models" / "voices"
DEFAULT_CONFIG_PATH = ROOT / "state" / "voices.json"

#: A piper voice is two files: the model and its config. Both are required.
#:
#: The config is not optional and `speech.ensure_voice` already says why in its own
#: comment: piper reads the sample rate and phoneme id map from it, and a voice
#: without one loads and then speaks at the wrong speed. Fast, chipmunk speech that
#: is *almost* intelligible is a far worse failure than an error, so a pair is the
#: unit here and a lone `.onnx` is not a voice.
MODEL_SUFFIX = ".onnx"
CONFIG_SUFFIX = ".onnx.json"

#: A voice name has to be usable as a filename, so it is checked the same way a model
#: filename is (D74). Refused rather than sanitised: normalising first is how a
#: traversal filter gets bypassed.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\- ]{0,120}$")

#: Whisper models the transcription surface may select. Stated here rather than read
#: from faster-whisper because that import is what D1 keeps out of the daemon.
#:
#: The sizes are the published parameter counts, and `small.en` is the default per
#: D18's measurement (real-time factor 1.30 on CPU, WER 0.000, 0.27 GiB resident).
WHISPER_MODELS: tuple[dict[str, Any], ...] = (
    {"name": "tiny.en", "params_m": 39, "note": "fastest, least accurate"},
    {"name": "base.en", "params_m": 74, "note": "the usual first step up"},
    {"name": "small.en", "params_m": 244, "note": "default; measured RTF 1.30, WER 0.000"},
    {"name": "medium.en", "params_m": 769, "note": "noticeably slower on CPU"},
    {"name": "large-v3", "params_m": 1550, "note": "multilingual; not CPU-viable here"},
)

DEFAULT_WHISPER = "small.en"


class VoiceConfigError(ValueError):
    """Something about a voice or the catalogue was wrong. Fit for a user to read."""


def is_safe_name(name: str) -> bool:
    return bool(isinstance(name, str) and _SAFE_NAME.match(name))


def _pair(name: str, directory: Path | None = None) -> tuple[Path, Path]:
    base = (directory or VOICES_DIR) / name
    return base.with_suffix(MODEL_SUFFIX), Path(str(base) + CONFIG_SUFFIX)


@dataclass
class Voice:
    """One voice, from whichever source it came."""

    name: str
    installed: bool
    source: str            # "known" | "imported" | "known+installed"
    model_bytes: int = 0
    config_bytes: int = 0
    note: str = ""

    @property
    def complete(self) -> bool:
        """Both halves present and non-empty.

        The size check is not paranoia: `speech.ensure_voice` writes to a `.part`
        file and renames only on success, so a zero-byte `.onnx` means something
        went wrong in a way that would otherwise look installed.
        """
        return self.model_bytes > 0 and self.config_bytes > 0


@dataclass
class VoiceConfig:
    default: str
    voices: dict[str, Voice] = field(default_factory=dict)
    path: Path | None = None
    provenance: str = "defaults"
    whisper: str = DEFAULT_WHISPER


def known_voices() -> dict[str, str]:
    """The downloadable set, from `speech.py`, which owns that knowledge.

    Imported rather than restated: the HF path for each voice is a fact about the
    upstream repository that `speech.py` already documents at length, and a second
    copy here would be a second thing to update when a voice is added.
    """
    from . import speech

    return dict(speech.VOICE_PATHS)


def installed_voices(directory: Path | None = None) -> dict[str, Voice]:
    """Every complete voice pair on disk, discovered rather than registered."""
    base = directory or VOICES_DIR
    if not base.is_dir():
        return {}

    found: dict[str, Voice] = {}
    for model in sorted(base.glob(f"*{MODEL_SUFFIX}")):
        if not model.is_file():
            continue
        # `x.onnx.json` also matches `*.onnx`? No - it ends in `.json`. But a voice
        # named `foo.onnx` would produce a config `foo.onnx.onnx.json`, and
        # `glob("*.onnx")` must not pick up `foo.onnx.json`'s stem. Stripping the
        # suffix rather than the stem is what keeps the two files paired correctly.
        name = model.name[: -len(MODEL_SUFFIX)]
        config = Path(str(base / name) + CONFIG_SUFFIX)
        try:
            model_bytes = model.stat().st_size
            config_bytes = config.stat().st_size if config.is_file() else 0
        except OSError:
            continue
        found[name] = Voice(
            name=name,
            installed=True,
            source="imported" if name not in known_voices() else "known+installed",
            model_bytes=model_bytes,
            config_bytes=config_bytes,
            note="" if config_bytes else f"missing {name}{CONFIG_SUFFIX}",
        )
    return found


def catalogue(directory: Path | None = None) -> dict[str, Voice]:
    """Known and installed, merged. Installed wins, because it is the fact."""
    out: dict[str, Voice] = {}
    for name in known_voices():
        out[name] = Voice(name=name, installed=False, source="known",
                          note="not downloaded yet")
    out.update(installed_voices(directory))
    return out


def load(path: Path | str | None = None,
         directory: Path | None = None) -> VoiceConfig:
    """Read the chosen default, degrading rather than failing.

    Same contract as `modelconfig.load`, and for the same reason: the daemon calls
    this, and a daemon that will not start because a settings file has a stray comma
    is a daemon the user cannot fix without a text editor.
    """
    from . import speech

    target = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    voices = catalogue(directory)
    fallback = speech.DEFAULT_VOICE

    if not target.is_file():
        return VoiceConfig(default=fallback, voices=voices, path=target,
                           provenance="defaults")

    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return VoiceConfig(
            default=fallback, voices=voices, path=target,
            provenance=f"defaults+unreadable: {type(exc).__name__}: {exc}",
        )

    if not isinstance(raw, dict):
        return VoiceConfig(default=fallback, voices=voices, path=target,
                           provenance="defaults+unreadable: top level is not an object")

    default = raw.get("default") or fallback
    problems: list[str] = []
    if not is_safe_name(str(default)):
        problems.append(f"default {default!r} is not a usable voice name")
        default = fallback
    else:
        # `installed`, not `in voices`. Every *known* voice is in the catalogue
        # whether or not it has been downloaded, so membership is always true for
        # the three built-ins - and the first version checked membership, so a chosen
        # voice that was never downloaded reported a clean "file" and the window had
        # nothing to warn about. The question is whether it is *here*, not whether it
        # is known.
        entry = voices.get(str(default))
        if entry is None or not entry.complete:
            problems.append(
                f"default {default!r} is not downloaded yet"
            )

    whisper = str(raw.get("whisper") or DEFAULT_WHISPER)
    known = {m["name"] for m in WHISPER_MODELS}
    if whisper not in known:
        problems.append(f"whisper model {whisper!r} is not one of {sorted(known)}")
        whisper = DEFAULT_WHISPER

    provenance = "file" if not problems else "file+partial: " + "; ".join(problems)
    return VoiceConfig(default=str(default), voices=voices, path=target,
                       provenance=provenance, whisper=whisper)


def save(config: VoiceConfig, path: Path | str | None = None) -> Path:
    """Write the choice atomically. Does not require the voice to be installed.

    Unlike the model config, a *missing* voice is not refused. Choosing a voice you
    have not downloaded yet is a legitimate act - it is how you say "this is the one
    I want" before spending 63 MB - and the window offers to fetch it immediately
    afterwards. Refusing here would make the download button unreachable.
    """
    target = Path(path) if path is not None else (config.path or DEFAULT_CONFIG_PATH)

    if not is_safe_name(config.default):
        raise VoiceConfigError(
            f"{config.default!r} is not a usable voice name"
        )
    known = {m["name"] for m in WHISPER_MODELS}
    if config.whisper not in known:
        raise VoiceConfigError(
            f"whisper model {config.whisper!r} is not one of {sorted(known)}"
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "_comment": (
            "Butleroy voice configuration. 'default' is the voice that speaks; "
            "'whisper' is the transcription model. Voices live in models/voices/ as "
            "a .onnx and a .onnx.json pair, and are discovered from disk rather than "
            "listed here. Safe to hand-edit; an unreadable file falls back to the "
            "built-in default rather than stopping the daemon."
        ),
        "default": config.default,
        "whisper": config.whisper,
    }
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(target)
    return target


def import_voice(source: Path | str, name: str | None = None,
                 directory: Path | None = None) -> Voice:
    """Copy a `.onnx` + `.onnx.json` pair into `models/voices/`.

    **This is the one op that reads a path the user supplied**, and it is worth
    being explicit about what protects it, because "it is only an op" is not by
    itself an answer.

    What it does *not* do is let the assistant read anything. Ops are not tools, so
    `brain.turn` cannot propose one - a model that could call `voice_import` would
    have an arbitrary-file-read with no gate in front of it, and that is the D59
    second-door defect exactly. The reachability is the safeguard: only a process
    holding the authkey can call this, which is the local user.

    What it *does* do, on top of that:

      * the source must exist, be a file, and end in `.onnx`;
      * the matching `.onnx.json` must exist beside it, because a lone model is a
        chipmunk-voiced half-voice (see `MODEL_SUFFIX`'s note);
      * the *destination* name is checked against `_SAFE_NAME`, so the copy cannot
        escape `models/voices/` however the caller spells it;
      * the whole thing is audited with the source path recorded, so "where did this
        voice come from" has an answer later.

    The alternative considered and rejected: routing the copy through the gated
    `file.copy` tool. That would put a T3 confirmation dialog in front of "add my
    voice" - a dialog asking the user to confirm the thing they just asked for, on
    their own machine, in their own settings window. D60 already records that a
    confirmation which cannot be meaningfully refused trains people to click through
    confirmations.
    """
    src = Path(source)
    if not src.is_file():
        raise VoiceConfigError(f"{src} is not a file")
    if not src.name.endswith(MODEL_SUFFIX):
        raise VoiceConfigError(
            f"{src.name!r} is not a {MODEL_SUFFIX} voice model"
        )

    src_config = Path(str(src)[: -len(MODEL_SUFFIX)] + CONFIG_SUFFIX)
    if not src_config.is_file():
        raise VoiceConfigError(
            f"{src.name!r} has no {CONFIG_SUFFIX} beside it. A piper voice is two "
            f"files, and without the config the voice loads and then speaks at the "
            f"wrong speed - which is worse than not loading at all."
        )

    chosen = name or src.name[: -len(MODEL_SUFFIX)]
    if not is_safe_name(chosen):
        raise VoiceConfigError(
            f"{chosen!r} is not a usable voice name; it must be a plain filename "
            f"with no path separators"
        )

    base = directory or VOICES_DIR
    base.mkdir(parents=True, exist_ok=True)
    dst_model, dst_config = _pair(chosen, base)

    # Copied to a temporary name and renamed, so a failure part-way cannot leave a
    # truncated voice that looks installed. Same reasoning as `speech.ensure_voice`'s
    # `.part` file, and the same reasoning as the config writes in D74.
    for src_file, dst_file in ((src, dst_model), (src_config, dst_config)):
        tmp = dst_file.with_suffix(dst_file.suffix + ".part")
        shutil.copy2(src_file, tmp)
        tmp.replace(dst_file)

    return installed_voices(base)[chosen]


def remove_voice(name: str, directory: Path | None = None) -> list[str]:
    """Delete an installed voice's files. Returns what was removed.

    Refuses the *default* voice, and refuses the built-in fallback even when it is
    not the default: removing the voice the assistant speaks with leaves it unable to
    say anything, and the failure would appear on the next turn rather than here.
    """
    from . import speech

    if not is_safe_name(name):
        raise VoiceConfigError(f"{name!r} is not a usable voice name")
    if name == speech.DEFAULT_VOICE:
        raise VoiceConfigError(
            f"{name!r} is the built-in fallback voice and cannot be removed; the "
            f"assistant would have nothing to speak with"
        )

    base = directory or VOICES_DIR
    model, config = _pair(name, base)
    if not model.is_file() and not config.is_file():
        raise VoiceConfigError(f"{name!r} is not installed")

    removed: list[str] = []
    for path in (model, config):
        if path.is_file():
            path.unlink()
            removed.append(path.name)
    return removed


__all__ = [
    "CONFIG_SUFFIX",
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_WHISPER",
    "MODEL_SUFFIX",
    "VOICES_DIR",
    "WHISPER_MODELS",
    "Voice",
    "VoiceConfig",
    "VoiceConfigError",
    "catalogue",
    "import_voice",
    "installed_voices",
    "is_safe_name",
    "known_voices",
    "load",
    "remove_voice",
    "save",
]