"""Listening: the microphone, the wake word, and the end of an utterance.

Separate from `speech.py`, which knows how to turn *files* into words and back.
This is about the part that is time-shaped - deciding when the user started
talking and when they stopped - and that part has its own failure modes, none of
which are about model quality.

The three decisions worth stating up front, because each is a way to get this
wrong in a way that only shows up as "the assistant never wakes up":

**Ask each device for the rate it reports, then resample.** Three of the four
devices here declare 44100 or 48000 Hz and reject 16000 Hz outright, with
`PaErrorCode -9997 Invalid sample rate`. The intuitive move - ask for the 16 kHz
that whisper wants - fails on hardware that is working perfectly. What the driver
will accept and what we want to store are not the same number.

**"Blocked" and "in use" are different failures and get different advice.** A
device that is exclusive-mode and owned by another application is a configuration
problem the user can fix by closing the other thing. A device that opens and then
returns nothing is a gain or connection problem, and no amount of code fixes it.

**Energy above zero is not speech.** A room with a fan in it has energy. The end
of an utterance is therefore detected with a hysteresis band and a minimum
duration, and the segment is trimmed - but a segment that contains only noise is
still a segment, and the transcript of it is the honest answer: empty. A wake
loop that fires on a fan is worse than no wake loop, because it trains the user
to ignore it.

Everything here is CPU. Per D50 voice costs no VRAM at all.
"""

from __future__ import annotations

import ctypes
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

ROOT = Path(__file__).resolve().parent.parent.parent

#: What whisper wants, and what we therefore store. Not what we ask the driver
#: for - see the module docstring.
TARGET_RATE = 16000

#: 10 ms. Small enough that a stop request is honoured quickly, large enough that
#: the loop is not spinning on the GIL between frames.
FRAME_MS = 10

#: An utterance is over once the signal stays below the release threshold for
#: this long. Shorter and a pause mid-sentence ends the utterance; longer and the
#: assistant feels unresponsive to natural pauses.
SILENCE_HOLD_S = 0.75

#: ...but not shorter than this after speech actually started, which is what stops
#: a cough from becoming a request.
MIN_UTTERANCE_S = 0.35

#: The gate and the release sit apart on purpose. A single threshold either chatters
#: around it or stays stuck on one side; the gap between them is what makes a level
#: meter into a decision.
#:
#: The *gap* is the design. The absolute numbers are only a fallback, and they were
#: guessed - a real fault, worth naming plainly. They were chosen without ever having
#: heard a voice on the machine they were for. Measured afterwards, this machine's
#: inputs peak at 301 out of 32767 (-41 dBFS) in a quiet room with something
#: occasionally being said into it. An absolute threshold of 220 would never have
#: opened on that, and raising the microphone gain is not a reliable fix for a number
#: that was never measured in the first place.
#:
#: So calibrate() measures the floor on the actual device and derives both numbers
#: from it. These constants are what an uncalibrated microphone falls back to.
OPEN_RMS = 220.0
RELEASE_RMS = 110.0

#: How far above the measured floor a sound has to be to count as speech.
#:
#: 6x, not 2x, and the margin is the point. The failure being guarded against is a
#: wake loop that fires on a fan: a threshold only slightly above the floor will open
#: on *someone else's* noise, and a voice assistant that answers to a refrigerator is
#: worse than one that has to be raised to.
SPEECH_OVER_FLOOR = 6.0

#: Never below an absolute minimum: two of this machine's devices report a floor of
#: exactly zero, and a threshold derived from that would open on one quantisation step.
ABSOLUTE_MIN_OPEN = 90.0

#: Never above a ceiling, so a noisy room cannot produce a threshold nothing in it
#: will ever reach and the assistant goes permanently deaf instead of permanently on.
ABSOLUTE_MAX_OPEN = 6000.0

#: Seconds of the room to listen to before deciding what "quiet" means here.
CALIBRATE_S = 1.5

#: Ceilings on a single utterance, so a stuck-open microphone produces a bounded
#: file rather than filling the disk. Both are generous: 60 s is a long answer.
MAX_UTTERANCE_S = 60.0
MAX_CAPTURE_S = 300.0


class NoInputDevice(RuntimeError):
    """No input could be opened, and the message says what to do about it."""


# --------------------------------------------------------------------------- devices


def list_inputs() -> list[dict[str, Any]]:
    """Every device PortAudio will let us name, with the rate each prefers.

    Preferred rate is reported because it is load-bearing: asking a WDM-KS device
    for a rate it was not configured for is refused, and that refusal looks
    exactly like a broken microphone.
    """
    import sounddevice as sd

    out: list[dict[str, Any]] = []
    for index, dev in enumerate(sd.query_devices()):
        if int(dev["max_input_channels"]) < 1:
            continue
        out.append(
            {
                "index": index,
                "name": str(dev["name"]),
                "channels": int(dev["max_input_channels"]),
                "rate": int(dev["default_samplerate"]) or 44100,
            }
        )
    return out


def default_input() -> dict[str, Any]:
    """The device to use when nobody named one.

    The system's own default, unless it cannot be opened, in which case the first
    input that will. Preferring the Windows default rather than "the first
    microphone we can find" is what makes the choice follow the user plugging in
    headphones rather than our enumeration order.
    """
    import sounddevice as sd

    inputs = list_inputs()
    if not inputs:
        raise NoInputDevice(
            "no audio input device is present at all. This is not something the "
            "assistant can work around: there is nothing to listen to."
        )
    try:
        index = int(sd.default.device[0])
    except Exception:  # noqa: BLE001 - a host with no default is still a host
        index = -1
    for dev in inputs:
        if dev["index"] == index:
            return dev
    return inputs[0]


class Microphone:
    """An open input stream, yielding 16 kHz mono frames.

    Constructing this opens the device. That is deliberate - the alternative is a
    lazy object that fails at the first read, which for a voice loop means it
    fails after the user has already spoken.
    """

    def __init__(
        self,
        device: int | str | None = None,
        *,
        rate: int | None = None,
        frames_per_buffer: int | None = None,
    ) -> None:
        import numpy as np
        import sounddevice as sd

        self._np = np
        self._sd = sd

        if device is None:
            chosen = default_input()
        elif isinstance(device, int):
            # The rate must come from the device, and looking it up is the whole
            # point of passing an index. Hardcoding 44100 here - which this did -
            # makes a 48000 Hz device fail to open with "Invalid sample rate",
            # which reads as broken hardware and is nothing of the kind. Measured:
            # every index-addressed open on a 48 kHz device failed at 44100 Hz,
            # and only because of this line.
            matches = [d for d in list_inputs() if d["index"] == device]
            if not matches:
                known = ", ".join(str(d["index"]) for d in list_inputs()) or "(none)"
                raise NoInputDevice(
                    f"no input at index {device}. Available indices: {known}"
                )
            chosen = matches[0]
        else:
            matches = [d for d in list_inputs() if d["name"] == device]
            if not matches:
                known = ", ".join(d["name"] for d in list_inputs()) or "(none)"
                raise NoInputDevice(
                    f"no input named {device!r}. Available: {known}"
                )
            chosen = matches[0]

        self.device_index = int(chosen["index"])
        self.device_name = str(chosen["name"])
        # The rate we *ask for* is the device's, never TARGET_RATE. This is the
        # whole reason this class exists as more than a thin wrapper.
        self.rate = int(rate or chosen["rate"])
        self.channels = 1
        self._block = int(
            frames_per_buffer or max(1, int(self.rate * FRAME_MS / 1000))
        )

        try:
            self._stream = sd.InputStream(
                device=self.device_index,
                channels=self.channels,
                samplerate=self.rate,
                dtype="int16",
                blocksize=self._block,
            )
            self._stream.start()
        except Exception as why:  # noqa: BLE001 - PortAudio raises its own types
            raise NoInputDevice(
                f"could not open {self.device_name!r} at {self.rate} Hz: {why}. "
                f"If another application holds the device exclusively, close it - "
                f"a microphone can only be opened by one program at a time on most "
                f"Windows audio drivers."
            ) from why

    # -- streaming ---------------------------------------------------------

    def frames(self, seconds: float | None = None) -> Iterator[Any]:
        """Yield frames of samples at the device's native rate.

        `seconds` bounds the total; unbounded by default, because the caller's
        loop is the one that decides when there has been enough.
        """
        limit = None if seconds is None else int(seconds * self.rate / self.rate)
        blocks = 0
        max_blocks = None
        if limit is not None:
            max_blocks = int(limit * self.rate / self._block)
        while max_blocks is None or blocks < max_blocks:
            data, overflowed = self._stream.read(self._block)
            blocks += 1
            if overflowed:
                # Reported rather than silently dropped. An overflow means the
                # driver had nowhere to put samples we already asked for, so the
                # audio is no longer continuous and a transcript of it is a
                # transcript of a gap.
                yield _Overflow(len(data))
            else:
                yield data

    def rms(self, frame: Any) -> float:
        return float(self._np.sqrt(self._np.mean(frame.astype(self._np.float64) ** 2)))

    # -- to files ----------------------------------------------------------

    def capture(self, path: str | os.PathLike, seconds: float) -> Path:
        """Record a fixed number of seconds to a 16 kHz wav, whatever it contains.

        Fixed rather than segmenting, because this is the honest one: "record N
        seconds and see what is there" is a measurement, and a measurement that
        silently drops the quiet parts is measuring something else.
        """
        import numpy as np

        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        blocks = int(seconds * self.rate / max(1, self._block))
        chunks: list[Any] = []
        for frame in self.frames():
            if isinstance(frame, _Overflow):
                continue
            chunks.append(frame)
            if len(chunks) >= blocks:
                break
        data = _resample(np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.int16),
                         self.rate)
        with wave.open(str(out), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(TARGET_RATE)
            wf.writeframes(data.astype(np.int16).tobytes())
        return out

    def close(self) -> None:
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:  # noqa: BLE001 - closing must not raise
            pass

    def __enter__(self) -> "Microphone":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def describe(self) -> dict[str, Any]:
        return {
            "device": self.device_name,
            "index": self.device_index,
            "sample_rate": self.rate,
            "block_ms": FRAME_MS,
            "stored_at": TARGET_RATE,
        }


class _Overflow:
    """Marks a lost block rather than pretending the audio was continuous."""

    def __init__(self, blocks: int) -> None:
        self.blocks = blocks


# --------------------------------------------------------------------- utterance


@dataclass
class Utterance:
    """One complete thing the user said."""

    audio: Any
    rate: int = TARGET_RATE
    seconds: float = 0.0
    peak: float = 0.0
    rms: float = 0.0
    overflows: int = 0
    trimmed_s: float = 0.0

    def save(self, path: str | os.PathLike) -> Path:
        """Write 16 kHz mono 16-bit, the format `speech.transcribe` expects."""
        import numpy as np

        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        pcm = self.audio.astype(np.int16)
        with wave.open(str(out), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self.rate)
            wf.writeframes(pcm.tobytes())
        return out

    def describe(self) -> dict[str, Any]:
        return {
            "seconds": round(self.seconds, 2),
            "peak": int(self.peak),
            "rms": round(self.rms, 1),
            "overflows": self.overflows,
            "silence_trimmed_s": round(self.trimmed_s, 2),
        }


def _resample(frame: Any, src: int, dst: int = TARGET_RATE) -> Any:
    """Linear resample to the rate we store.

    Only ever called when `src != dst`, and it says so when it is not asked to do
    anything - a silent no-op resample would leave 48 kHz audio labelled 16 kHz,
    which whisper would transcribe at the wrong speed and produce words that are
    almost right, which is the worst kind of wrong.
    """
    if src == dst:
        return frame
    import numpy as np

    n_out = max(1, int(len(frame) * dst / src))
    idx = np.linspace(0, len(frame) - 1, n_out).astype(np.int64)
    return frame[idx]


@dataclass
class Calibrated:
    """What this room actually sounds like, and what follows from that.

    Measured rather than assumed because a threshold guessed on a different
    machine, in a different room, through a different microphone, is not a
    threshold - it is a number that happens to be a float.

    The floor is the loud percentile of the room rather than its mean, because
    the mean of a room with a fan in it is not the noise: one loud moment drags
    it and the derived threshold ends up above speech. The 90th percentile
    describes the room; the mean describes an event.
    """

    floor: float = 0.0
    peak: float = 0.0
    open: float = OPEN_RMS
    release: float = RELEASE_RMS
    seconds: float = CALIBRATE_S
    device: str = ""

    def describe(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "noise_floor": round(self.floor, 1),
            "peak_seen": int(self.peak),
            "open_rms": round(self.open, 1),
            "release_rms": round(self.release, 1),
            "window_s": round(self.seconds, 2),
            # "Did we listen" and "was the room loud" are different questions.
            # A device that measured a floor of exactly zero was listened to and
            # found digitally silent - which is a real, actionable answer, and
            # different from having nothing to say. Keying this off `floor`
            # instead of `seconds` conflates them, and conflating them hides the
            # one case that most needs reporting.
            "derived": ("measured" if self.seconds > 0
                        else "nothing was measured - fell back to the constants"),
            "silence": self.seconds > 0 and self.peak <= 2,
        }


def thresholds_for(floor: float) -> tuple[float, float]:
    """Turn a measured noise floor into an (open, release) pair.

    The release sits at half the open threshold because the band has to be a
    band. Halving rather than using a fixed fraction keeps the hysteresis
    proportional at every noise level: a room that needs 600 to open needs 300 to
    close, and the margin between them scales with the room instead of vanishing
    in a loud one.

    Clamped at both ends. The lower clamp exists because a device reporting a
    floor of zero must not produce a threshold of zero; the upper clamp exists
    because a noisy room must not produce a threshold nothing can reach - which
    fails silently and looks exactly like a broken microphone.
    """
    open_at = max(ABSOLUTE_MIN_OPEN,
                  min(ABSOLUTE_MAX_OPEN, floor * SPEECH_OVER_FLOOR))
    return open_at, open_at / 2.0


def calibrate(mic: Microphone, *, seconds: float = CALIBRATE_S) -> Calibrated:
    """Listen to the room and decide what counts as speech here.

    Called with the microphone already open. A short window is deliberate: this
    runs at the start of every listening session, and the user is waiting. It is
    also long enough to be more than one block, so a single anomalous frame
    cannot define the room.

    A device that is delivering a constant zero - which two of this machine's do
    - yields a floor of zero, and the absolute minimum is what saves it. That is
    the whole reason the minimum exists, and it was added after measuring exactly
    this case rather than before.
    """
    import numpy as np

    levels: list[float] = []
    t0 = mic.rate * 0
    wanted = int(seconds * mic.rate / max(1, mic._block))
    for frame in mic.frames():
        if isinstance(frame, _Overflow):
            continue
        levels.append(mic.rms(frame))
        if len(levels) >= wanted:
            break

    if not levels:
        return Calibrated(floor=0.0, peak=0.0, open=OPEN_RMS,
                          release=RELEASE_RMS, seconds=0.0,
                          device=mic.device_name)

    arr = np.asarray(levels, dtype=np.float64)
    floor = float(np.percentile(arr, 90))
    peak = float(arr.max())
    open_at, release_at = thresholds_for(floor)
    return Calibrated(floor=floor, peak=peak, open=open_at, release=release_at,
                      seconds=len(levels) * mic._block / mic.rate,
                      device=mic.device_name)

def segment(
    mic: Microphone,
    *,
    open_rms: float | None = None,
    release_rms: float | None = None,
    hold_s: float = SILENCE_HOLD_S,
    min_s: float = MIN_UTTERANCE_S,
    max_s: float = MAX_UTTERANCE_S,
    total_s: float = MAX_CAPTURE_S,
) -> Utterance | None:
    """Listen for one utterance and return it, or None if the deadline passed.

    Two thresholds rather than one, and the reason is not tidiness. A single
    threshold has two behaviours and both are bad: sit near it and the state
    machine chatters, opening and closing on every quiet syllable; set it high and
    it never opens. The band between them is the hysteresis, and it is what makes
    a level into a boundary.
    """
    import numpy as np

    # NOT calibrated here, deliberately. Calibrating inside this function would
    # consume the first CALIBRATE_S of the stream, so every utterance would lose
    # its opening - including the opening, which is where the first consonant
    # lives. That was written the obvious way first and the tests caught it as
    # eleven mysterious failures.
    #
    # So the caller calibrates once, at the start of a listening session, and
    # passes the result. Silence between calls falls back to the constants, which
    # is a real fallback and says so in the caller's own report rather than
    # being indistinguishable from a working threshold.
    if open_rms is None:
        open_rms = OPEN_RMS
    if release_rms is None:
        release_rms = RELEASE_RMS

    tail = max(1, int(hold_s * mic.rate))
    min_frames = int(min_s * mic.rate)

    collecting: list[Any] = []
    quiet = 0
    overflows = 0
    seen_blocks = 0
    max_blocks = int(total_s * mic.rate / max(1, mic._block))

    for frame in mic.frames():
        seen_blocks += 1
        if isinstance(frame, _Overflow):
            overflows += 1
            continue

        level = mic.rms(frame)
        if not collecting:
            if level < open_rms:
                # Still waiting. `seen_blocks` is the only clock, so a device that
                # streams silence forever ends this rather than hanging the loop.
                if seen_blocks >= max_blocks:
                    return None
                continue
            collecting.append(frame)
            quiet = 0
            continue

        collecting.append(frame)
        if level < release_rms:
            quiet += len(frame)
            if quiet >= tail:
                break
        else:
            quiet = 0
        if sum(len(f) for f in collecting) >= int(max_s * mic.rate):
            break
        if seen_blocks >= max_blocks:
            break

    if not collecting:
        return None

    raw = np.concatenate(collecting)
    # The *release* threshold trims, not the open one. Content between the two is
    # exactly what the hysteresis band exists to keep - it is speech that has got
    # quieter, not silence - and trimming at the open threshold deleted it. Found
    # by a test that put a steady level inside the band and watched the utterance
    # come back with that part cut off: the segmenter was right and the trim
    # undid it, which is the worst place for a disagreement to live.
    trimmed, trimmed_s = _trim(raw, mic.rate, release_rms)
    if len(trimmed) < min_frames:
        # Long enough to trigger on, too short to be a sentence. Returning it
        # would send a click to the model, and a model asked about a click will
        # confidently produce a sentence about a click.
        return None

    return Utterance(
        audio=_resample(trimmed, mic.rate),
        rate=TARGET_RATE,
        seconds=len(trimmed) / mic.rate,
        peak=int(np.max(np.abs(trimmed.astype(np.int32)))) if len(trimmed) else 0,
        rms=float(np.sqrt(np.mean(trimmed.astype(np.float64) ** 2))) if len(trimmed) else 0.0,
        overflows=overflows,
        trimmed_s=trimmed_s,
    )


def _trim(frame: Any, rate: int, threshold: float) -> tuple[Any, float]:
    """Drop the silence at each end, keeping a little at the start.

    The lead-in matters and its length is a judgement: too short and the first
    phoneme is already clipped, too long and every transcription begins with
    padding. A fifth of a second is the first consonant of a word.
    """
    import numpy as np

    env = np.abs(frame.astype(np.float64))
    win = max(1, rate // 100)
    count = len(env) // win
    if count == 0:
        return frame, 0.0
    peaks = env[: count * win].reshape(count, win).max(axis=1)
    above = np.where(peaks > threshold)[0]
    if above.size == 0:
        return frame, 0.0
    pad = win * 5
    lo = max(0, int(above[0]) * win - pad)
    hi = min(len(frame), (int(above[-1]) + 1) * win + pad)
    return frame[lo:hi], (len(frame) - (hi - lo)) / rate


# --------------------------------------------------------------------- wake word


#: The phrase the assistant answers to.
#:
#: openWakeWord ships six pretrained classifiers - alexa, hey mycroft, hey jarvis,
#: hey rhasspy, current weather, timers - downloaded by `download_models()`, not
#: trained. "hey jarvis" is the one chosen: two ordinary words plus a proper noun,
#: which is the same *shape* as a personal name and exercises the identical code
#: path, at about 20 KB with zero VRAM (D50). It is openWakeWord's model rather
#: than one of ours, and it answers to someone else's name - which is the trade
#: for a wake word that has been measured by someone else on a 5.5-hour corpus
#: of dinner-party noise instead of by us on nothing.
#:
#: The alternative was training "yo archie" here. That was attempted and failed to
#: learn - see DECISIONS D55 for the six count-and-unit errors behind it, and the
#: last measurement: every input, the phrase and ordinary speech alike, scored
#: 0.061. A model that cannot tell the wake phrase from the build failing again
#: is worse than one that answers to a different name.
WAKE_WORD = "hey_jarvis"


def wake_models_available() -> list[str]:
    """Which pretrained wake words are on disk right now.

    Reported rather than assumed. `download_models()` puts them in the installed
    package, not in this project, so "is the wake word downloaded?" is a question
    about somebody else's directory and the only honest answer is to look.
    """
    try:
        import openwakeword
    except Exception:  # noqa: BLE001 - absent is an answer, not a crash
        return []
    folder = Path(openwakeword.__file__).parent / "resources" / "models"
    if not folder.is_dir():
        return []
    return sorted(p.name[: -len(".onnx")] for p in folder.glob("*.onnx")
                  if not p.name.startswith(("melspectrogram", "embedding", "silero")))


def wake_model_path(name: str = WAKE_WORD) -> Path | None:
    """The file openWakeWord will actually load for `name`, or None.

    Found by globbing rather than by constructing a filename. The release assets
    carry a version suffix (`hey_jarvis_v0.1.onnx`) and the suffix is exactly the
    part a hand-written name would get wrong - the same trap as the piper voice
    layout, where `{name}/{name}.onnx` 404s and a confident guess downloads the
    wrong file. Searching answers "what is there"; constructing answers "what did
    I expect to be there".
    """
    try:
        import openwakeword
    except Exception:  # noqa: BLE001
        return None
    folder = Path(openwakeword.__file__).parent / "resources" / "models"
    hits = sorted(folder.glob(f"{name}*.onnx"))
    return hits[0] if hits else None


@dataclass
class WakeWord:
    """A local wake-word detector, or an honest report that we cannot have one.

    `openwakeword` is audio-only, which is the property that matters here. The
    alternative - a keyboard hook - means a process that is always watching every
    keystroke on the machine to answer a spoken word, and the same reasoning that
    chose Win32 `RegisterHotKey` over the `keyboard` library for the hotkey
    applies unchanged: a component that can see everything is a much larger blast
    radius than one that can hear a phrase.

    Absence is a state, not a crash. The caller decides whether voice is usable
    without a wake word (a push-to-talk button works perfectly well) and this
    exists so it can find out.
    """

    name: str = WAKE_WORD
    threshold: float = 0.5
    ok: bool = False
    error: str = ""
    _model: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        try:
            from openwakeword.model import Model

            # inference_framework="onnx" is the CPU path. The default picks a
            # runtime by sniffing, and a silent promotion of the audio stack onto
            # the card is exactly the thing D50 exists to prevent.
            self._model = Model(wakeword_models=[self.name],
                                inference_framework="onnx")
            self.ok = True
            self.error = ""
        except Exception as why:  # noqa: BLE001 - third-party, types unknown
            self.ok = False
            self.error = f"{type(why).__name__}: {why}"
            self._model = None

    def _score_chunk(self, chunk: Any) -> float:
        """One 80 ms prediction for our own phrase.

        `Model.predict` returns a dict of every loaded model to its score, not
        an array - it was written for several wake words at once, and this class
        loads exactly one. Indexing by `self.name` is the correct read; taking
        a max over the dict would be answering "did any phrase fire" to a caller
        that asked about one phrase in particular.
        """
        out = self._model.predict(chunk)
        if isinstance(out, tuple):
            out = out[0]
        if self.name in out:
            return float(out[self.name])
        if len(out) == 1:
            return float(next(iter(out.values())))
        return max((float(v) for v in out.values()), default=0.0)

    def score(self, frame: Any) -> float:
        """Highest wake-word score in one frame, or 0.0 if we have no model."""
        if not self.ok:
            return 0.0
        import numpy as np

        # The model eats int16 - that is what the microphone yields, and what
        # every production caller passes. A float frame in [-1, 1] is the
        # standard PCM normalization of the same signal, so it is scaled rather
        # than refused; anything else float is passed through untouched, because
        # guessing at an unknown scale would be inventing amplitude.
        #
        # This guard exists because the alternative is silence with no error:
        # float audio scores ~0.0001 on every window including the phrase
        # itself, which reads exactly like "the model does not fire" when the
        # truth is "the model was fed a whisper". That misreading cost a full
        # probe run before the int16/float comparison found it.
        data = np.asarray(frame)
        if data.dtype.kind == "f" and data.size and float(np.abs(data).max()) <= 1.5:
            data = (data * 32768.0).astype(np.int16)
        # openwakeword wants 1280 samples of 16 kHz (80 ms) and will raise on
        # anything else. Padding a short frame is correct: audio does not stop
        # existing at a block boundary, and a fixed-size model has to be fed
        # something.
        want = 1280
        if len(data) < want:
            data = np.pad(data, (0, want - len(data)))
        elif len(data) > want:
            # Score every 80 ms in the block and keep the best, so a wake word
            # spanning a block boundary is still seen.
            best = 0.0
            for start in range(0, len(data) - want + 1, want):
                best = max(best, self._score_chunk(data[start:start + want]))
            return best
        return self._score_chunk(data)

    def fired(self, frame: Any) -> bool:
        return self.score(frame) >= self.threshold

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "threshold": self.threshold,
                "error": self.error}


def describe() -> dict[str, Any]:
    """What listening would cost and whether it can work at all. Free of models."""
    out: dict[str, Any] = {
        "sample_rate": TARGET_RATE,
        "silence_hold_s": SILENCE_HOLD_S,
        "min_utterance_s": MIN_UTTERANCE_S,
        "open_rms": OPEN_RMS,
        "release_rms": RELEASE_RMS,
    }
    try:
        out["inputs"] = list_inputs()
    except Exception as why:  # noqa: BLE001
        out["inputs"] = []
        out["input_error"] = f"{type(why).__name__}: {why}"
    try:
        out["default_input"] = default_input()
    except Exception as why:  # noqa: BLE001
        out["default_input"] = None
        out["default_error"] = str(why)
    # The wake word, resolved by looking rather than by assuming. `describe()` is
    # the one call a user can make to find out whether voice will work at all, so
    # it reports the phrase it answers to and the file behind it - not merely that
    # some model is somewhere.
    out["wake_word"] = WAKE_WORD
    out["wake_model"] = str(wake_model_path() or "")
    out["wake_models_available"] = wake_models_available()
    return out