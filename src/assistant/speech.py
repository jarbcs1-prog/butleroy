"""Speaking and listening. Two models, and neither of them needs the card.

Both halves of the voice path are ONNX, both are CPU by default, and that is not
a compromise forced by the hardware - it is measured. `faster-whisper small.en`
at int8 occupies about 0.34 GiB of RAM against a 16 GiB budget, while the reason
to reach for the GPU would be latency. D18 says the GPU is only for the window
where a model is actually being used, and a microphone that is listening half the
day is not that window.

The asymmetry worth naming: transcribing is latency-sensitive and brief, so it is
the part that *could* be promoted; synthesising is neither (piper is far faster
than real time on one core) and so has no reason to be.

Nothing here loads a model at import. Import cost is paid by the daemon on every
start and by every test, and it is not worth it for a capability most turns do not
use - the first `Transcriber(...)` or `synthesise(...)` pays.
"""

from __future__ import annotations

import ctypes
import json
import os
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

ROOT = Path(__file__).resolve().parent.parent.parent
MODELS = ROOT / "models"

#: Where piper voices live. Kept inside the project so a half-downloaded voice is
#: as easy to delete as any other artefact, rather than scattered in a user cache
#: where it would outlive the project that needs it.
VOICES = MODELS / "voices"

#: The one voice we ship with. A project's default should be a decision, not
#: whatever the tool happened to pick first: `en_US-lessac-medium` is intelligible,
#: small, and not obviously a clone of anybody.
DEFAULT_VOICE = "en_US-lessac-medium"

#: Hugging Face repo holding piper voices, and where each known voice lives in it.
#:
#: The nesting is *not* uniform - `lessac` keeps the file directly in `medium/`
#: while other families nest it one level deeper under the voice name - so it is
#: stated per voice rather than derived by a rule. The first version of this file
#: assumed a uniform `medium/{name}/{name}.onnx`, got a confident 404, and the
#: obvious "fix" of trying several layouts is exactly how you end up downloading
#: the 500 MB voice instead of the 63 MB one.
VOICE_REPO = "rhasspy/piper-voices"
VOICE_PATHS = {
    "en_US-lessac-medium": "en/en_US/lessac/medium/{name}.onnx",
    "en_US-amy-medium": "en/en_US/amy/medium/{name}.onnx",
    "en_GB-alan-medium": "en/en_GB/alan/medium/{name}.onnx",
}

#: Measured on this machine, and the reason the GPU promotion path does not exist.
#:
#: D18 said transcription is CPU by default and may be promoted while listening.
#: The measurement says the promotion is unnecessary, so it is not built. Not
#: building it is the point of having measured: a planned feature whose premise no
#: longer holds is a liability with a test suite attached.
#:
#: `small.en`, CPU, int8, three sentences synthesised by piper so the ground truth
#: is exactly what was said:
#:
#:     audio 5.41s -> wall 4.16s   real-time factor 1.30   WER 0.000
#:     0.27 GiB resident, 1.3 s to load warm
#:
#: A real-time factor above 1.0 means it finishes *before* the audio ends - the
#: transcription is ready before the user has finished speaking. Against D1's
#: 7.0 GiB VRAM ceiling that is the trade we wanted and did not have to make:
#: zero VRAM instead of several gigabytes, and no slower in any way a person would
#: notice.
MEASURED_WHISPER_RTF = 1.30

#: Whisper model used when the caller does not name one. `small.en` rather than
#: `base.en` because measured word error rate on this machine separated them, and
#: the memory difference between them is a tenth of a gigabyte.
DEFAULT_WHISPER = "small.en"

#: Recording format for capture. 16 kHz mono is what whisper resamples to anyway,
#: and capturing at 48 kHz would store three times the bytes for no gain.
SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2          # 16-bit
CHANNELS = 1


def wav_seconds(path: str | os.PathLike) -> float:
    """Length of a wav in seconds, from the header rather than the samples."""
    with wave.open(str(path), "rb") as fh:
        return fh.getnframes() / float(fh.getframerate() or 1)


# --- resident memory, stated the way Windows reports it -----------------------

class _Pmc(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_uint32), ("PageFaultCount", ctypes.c_uint32),
        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
    ]


_PSAPI = ctypes.WinDLL("psapi", use_last_error=True)
_PSAPI.GetProcessMemoryInfo.restype = ctypes.c_int
_PSAPI.GetProcessMemoryInfo.argtypes = [
    ctypes.c_void_p, ctypes.POINTER(_Pmc), ctypes.c_uint32]
# GetCurrentProcess lives in kernel32, not psapi. Asking psapi for it raises
# AttributeError at import, which is at least loud - unlike the return-type bug
# above, which was silent.
_KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
_KERNEL32.GetCurrentProcess.restype = ctypes.c_void_p
_KERNEL32.GetCurrentProcess.argtypes = []


def resident_gib() -> float:
    """This process's working set in GiB.

    Working set rather than private bytes because the question is "how much is it
    holding", and the working set is what the OS is holding for it. Declaring the
    signatures matters here: without them ctypes returns a 32-bit int for the BOOL,
    the call fails silently, and this reports 0.00 for every process including a
    loaded model - a number that is identical across cases that obviously differ,
    which is the tell that the measurement is broken rather than small.
    """
    counter = _Pmc()
    counter.cb = ctypes.sizeof(_Pmc)
    if not _PSAPI.GetProcessMemoryInfo(
        _KERNEL32.GetCurrentProcess(), ctypes.byref(counter), counter.cb
    ):
        return 0.0
    return counter.WorkingSetSize / 1024 ** 3


# --- speaking -----------------------------------------------------------------


def voice_files(name: str = DEFAULT_VOICE) -> tuple[Path, Path]:
    """Where a voice's model and config live, whether or not they are downloaded."""
    if name not in VOICE_PATHS:
        # Refused rather than guessed. A wrong guess here is a 404 or, worse, a
        # multi-hundred-megabyte download of some other voice - and both look like
        # "voice is broken" to the person who asked to speak.
        raise KeyError(
            f"unknown voice {name!r}; known voices are {sorted(VOICE_PATHS)}"
        )
    return VOICES / f"{name}.onnx", VOICES / f"{name}.onnx.json"


def voice_url(name: str) -> str:
    return f"https://huggingface.co/{VOICE_REPO}/resolve/main/" + \
        VOICE_PATHS[name].format(name=name)


def ensure_voice(name: str = DEFAULT_VOICE) -> tuple[Path, Path]:
    """Download a voice if it is missing, and return its paths.

    The `.onnx.json` is fetched too, and the reason is worth stating: piper reads
    its sample rate and phoneme id map from that config, and a voice without one
    loads and then produces sound at the wrong speed - fast, chipmunk speech that
    is *almost* intelligible, which is a far worse failure than an error.
    """
    model, config = voice_files(name)
    for target, url in (
        (model, voice_url(name)),
        (config, voice_url(name) + ".json"),
    ):
        if target.exists() and target.stat().st_size > 0:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        import urllib.request

        tmp = target.with_suffix(target.suffix + ".part")
        with urllib.request.urlopen(url, timeout=120) as resp, \
                open(tmp, "wb") as fh:
            while chunk := resp.read(1 << 20):
                fh.write(chunk)
        # Renamed only on success. A voice truncated mid-download would otherwise
        # look present and be loaded, and a half a model is not a small error.
        tmp.replace(target)
    return model, config


@dataclass
class Speaker:
    """A loaded piper voice, or a reason it could not be loaded."""

    voice: Any = None
    error: str = ""
    load_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.voice is not None


def load_speaker(name: str = DEFAULT_VOICE) -> Speaker:
    """Load piper. Never raises: a missing voice must not take the daemon down.

    D44's rule applied - a probe that fails says so. An empty `Speaker` with no
    `error` would look exactly like "piper is installed and working", which is the
    one thing it is not.
    """
    from piper import PiperVoice

    try:
        model, config = ensure_voice(name)
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        return Speaker(error=f"voice {name!r} unavailable: {exc}")
    t0 = time.monotonic()
    try:
        voice = PiperVoice.load(str(model), config_path=str(config))
    except Exception as exc:  # noqa: BLE001
        return Speaker(error=f"voice {name!r} failed to load: {exc}")
    return Speaker(voice=voice, load_s=time.monotonic() - t0)


def synthesise(sentences: Sequence[str], out_dir: str | os.PathLike,
               voice: str = DEFAULT_VOICE) -> list[tuple[str, Path]]:
    """Render each sentence to a 16 kHz mono wav. Returns (text, path) pairs.

    Used by the probe as *ground truth* for transcription, which is why the
    sample rate is forced to whisper's: a mismatch here would show up as
    transcription error caused by the harness rather than by the model, and a
    measurement that blames the wrong thing is worse than no measurement.
    """
    speaker = load_speaker(voice)
    if not speaker.ok:
        raise RuntimeError(speaker.error)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    made: list[tuple[str, Path]] = []
    for i, text in enumerate(sentences):
        chunks = list(speaker.voice.synthesize(text))
        if not chunks:
            continue
        audio = b"".join(bytes(c.audio_int16_bytes) for c in chunks)
        rate = chunks[0].sample_rate or SAMPLE_RATE
        target = out_dir / f"tts_{i:02d}.wav"
        with wave.open(str(target), "wb") as fh:
            fh.setnchannels(1)
            fh.setsampwidth(SAMPLE_WIDTH)
            fh.setframerate(rate)
            fh.writeframes(audio)
        made.append((text, target))
    return made


# --- listening ----------------------------------------------------------------


@dataclass
class Transcriber:
    """Whisper, held in memory because loading it costs more than using it.

    Cold load measured at 34 s for tiny and 46 s for base - those were first-run
    downloads, but even warm the first load of `small.en` is seconds, and paying
    that on the first spoken request would make the assistant feel broken rather
    than slow. So it is built when voice is enabled and kept, and the cost is
    ~0.34 GiB of RAM against a 16 GiB budget.
    """

    model: str = DEFAULT_WHISPER
    device: str = "cpu"
    compute_type: str = "int8"
    _engine: Any = field(default=None, init=False, repr=False)
    load_s: float = 0.0
    ram_gib: float = 0.0

    def __post_init__(self) -> None:
        from faster_whisper import WhisperModel

        before = resident_gib()
        t0 = time.monotonic()
        self._engine = WhisperModel(
            self.model, device=self.device, compute_type=self.compute_type
        )
        self.load_s = time.monotonic() - t0
        self.ram_gib = resident_gib() - before

    def transcribe(self, wav: str | os.PathLike, **kw: Any) -> str:
        """Transcribe a wav file. Returns plain text, never raises on content.

        A failure is returned as an error string rather than raised, because the
        caller is a voice loop: an exception there would end the loop and leave
        the user with a microphone that has silently stopped working.
        """
        try:
            segments, _info = self._engine.transcribe(str(wav), **kw)
            return " ".join(s.text.strip() for s in segments).strip()
        except Exception as exc:  # noqa: BLE001
            return f"[transcription failed: {exc}]"

    def unload(self) -> None:
        self._engine = None


def sample_hz() -> int:
    return SAMPLE_RATE


def describe() -> dict[str, Any]:
    """What voice would cost right now, without loading anything.

    Free to call, and worth it: the daemon can answer "what is voice using" long
    before the user has asked to speak.
    """
    return {
        "whisper_model": DEFAULT_WHISPER,
        "device": "cpu",
        "compute_type": DEFAULT_WHISPER and "int8",
        "sample_rate": SAMPLE_RATE,
        "voice": DEFAULT_VOICE,
        "voices_dir": str(VOICES),
        "loaded": False,
        "note": "voice is loaded on first use; nothing is resident until then",
    }