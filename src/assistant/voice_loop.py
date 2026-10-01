"""The voice loop: wake word, utterance, brain, answer out loud.

Everything else in Phase 5 is a component. This is the thing that has to be
right about all of them at once, and its hard parts are not about signal
processing.

**The assistant must not hear itself.** When it speaks, the microphone is
hearing the speakers, and the wake word detector would hear "yo Archie" in the
assistant's own voice and start a second turn, which would speak again, which
would be heard again. The fix is not a cleverer threshold - no threshold
reliably separates the owner's voice from a speaker pointed at the same
microphone. The fix is to stop listening while talking. Capture is muted for the
duration of the output, which cannot be wrong, and which is the pattern borrowed
from `F:\\openchamber-2.0.2\\packages\\web\\server\\lib\\voice\\browserVoiceService.ts`
and is recorded here as D51.

**One turn at a time.** The loop does not queue. A second wake word during a turn
is dropped rather than buffered, because "you said that while I was answering" is
not something anyone wants replayed, and because an unbounded queue under a
wake word is a memory leak with a microphone attached.

**Calibration happens once, before the loop, never inside it.** Calibrating takes
1.5 s of audio. Done per iteration it would eat the start of every utterance -
which is the first consonant, i.e. the word. Done once at start-up it costs one
and a half seconds of the user's life and is amortised over every turn that
follows.

**The handler is a callable, not a socket.** This loop knows nothing about pipes,
authkeys or the daemon. That is what makes it testable against a fake microphone
and a fake handler, which is the only way to test a loop that runs forever.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import audio as audio_mod
from . import speech


@dataclass
class Turn:
    """One completed exchange, kept for the audit log and for tests."""

    said: str = ""
    heard: str = ""
    answered: str = ""
    seconds: float = 0.0
    wake_s: float | None = None
    error: str = ""


@dataclass
class LoopStats:
    """What happened, because a loop that runs forever is otherwise unobservable."""

    turns: int = 0
    wakes: int = 0
    dropped_while_busy: int = 0
    no_speech: int = 0
    errors: int = 0
    idle_stops: int = 0
    last_turn: dict[str, Any] = field(default_factory=dict)


class VoiceLoop:
    """Wake word in, answer out. Nothing above this module knows it exists.

    `handler` takes the transcript and returns what should be said. It returns a
    string, and an empty string means "say nothing" - a handler that decides a
    turn needs no answer is a normal outcome, not an error, and forcing it to
    speak anyway would make the assistant interrupt itself constantly.
    """

    def __init__(
        self,
        handler: Callable[[str], str],
        *,
        device: str | int | None = None,
        voice: str = speech.DEFAULT_VOICE,
        seconds: float = 30.0,
        wake_name: str = audio_mod.WAKE_WORD,
        out_dir: str | Path | None = None,
        speak: bool = True,
        mic: Any = None,
        wake: Any = None,
    ) -> None:
        self.handler = handler
        self.voice = voice
        self.seconds = float(seconds)
        self.wake_name = wake_name
        self.speak = speak
        self.stats = LoopStats()

        # Injected for tests. Constructed here otherwise. The injection is why
        # every test of this class runs in milliseconds: a real Microphone opens
        # a device and a real WakeWord loads two ONNX models.
        self._mic = mic
        self._wake = wake
        self._device = device
        self.out_dir = Path(out_dir) if out_dir else None

        self.busy = False          # true while speaking; the mute condition
        self._cal: audio_mod.Calibrated | None = None
        self._transcriber: Any = None   # built on first use, then kept (D50)
        self._exhausted = False          # the stream ended; the device is gone
        self.last: Turn | None = None

    # -- setup ------------------------------------------------------------

    def open(self) -> None:
        """Open the microphone, calibrate it, and arm the wake word.

        Separate from `run` because the first call is slow and is the one a user
        is most likely to interrupt. A caller that wants to report "getting the
        microphone ready" can do so between these two.
        """
        if self._mic is None:
            self._mic = audio_mod.Microphone(self._device)
        if self._wake is None:
            self._wake = audio_mod.WakeWord(self.wake_name)
        self._cal = audio_mod.calibrate(self._mic)

    # -- the loop ---------------------------------------------------------

    def run(self, *, turns: int | None = None, max_idle_s: float | None = None
            ) -> LoopStats:
        """Listen until `turns` have completed, or forever if `turns` is None.

        `turns` and `max_idle_s` both exist for the same reason: a loop that only
        stops on a turn it can never have is a loop that hangs. The first version
        of this ran `while turns is None or self.stats.turns < turns` and nothing
        else, so a microphone where the phrase is never detected spun at full
        speed producing no output and no error - found by a test that timed out,
        and it is a defect rather than a test artefact. `max_idle_s` stops it; and
        a stream that ends also stops it, because a device that has gone away is
        not going to come back on its own.

        Production passes `turns=None` and a generous `max_idle_s`; a probe
        measuring the model against a real microphone passes both.
        """
        if self._mic is None:
            self.open()
        mic, wake = self._mic, self._wake
        cal = self._cal or audio_mod.calibrate(mic)
        self._cal = cal

        last_wake = time.monotonic()
        while turns is None or self.stats.turns < turns:
            if self.busy:
                # Muted: drain without looking. See the module docstring for why
                # this is a mute and not a filter.
                self._drain(mic)
                self.stats.dropped_while_busy += 1
                continue
            if not wake.ok:
                # No wake word, no loop. Reported rather than spun on: an
                # assistant that silently listens for a phrase it cannot detect
                # is an assistant that never answers and never says why.
                self.stats.errors += 1
                break
            if max_idle_s is not None and time.monotonic() - last_wake > max_idle_s:
                # Not an error. Nothing was said for a long time, which is a
                # completely ordinary thing for a room to do.
                self.stats.idle_stops += 1
                break
            if not wake.fired(self._frame(mic)):
                # An exhausted stream means the device is gone. Reported, and the
                # loop stops rather than polling a stream that will never yield.
                if self._exhausted:
                    self.stats.idle_stops += 1
                    break
                continue
            self.stats.wakes += 1
            last_wake = time.monotonic()

            heard = audio_mod.segment(
                mic,
                open_rms=cal.open,
                release_rms=cal.release,
                total_s=self.seconds,
            )
            if heard is None:
                # The wake word fired on a noise. Not an error - the model will
                # do this occasionally and pretending otherwise would make the
                # reported false-fire rate meaningless.
                self.stats.no_speech += 1
                continue

            turn = self._exchange(heard, wake)
            self.stats.turns += 1
            self.last = turn
            self.stats.last_turn = _describe_turn(turn)

        return self.stats

    # -- one turn ---------------------------------------------------------

    def _exchange(self, heard: audio_mod.Utterance, wake: Any) -> Turn:
        t0 = time.time()
        turn = Turn(seconds=round(heard.seconds, 2))

        transcript = self._transcribe(heard)
        turn.heard = transcript
        if transcript.startswith("[transcription failed"):
            # Reported as its own outcome rather than handed to the brain. A
            # handler given "[transcription failed: ...]" will answer it - the
            # model is extremely willing to discuss a string it was handed - and
            # the user gets a confident response to an error message.
            turn.error = transcript
            self.stats.errors += 1
            return turn

        try:
            answer = str(self.handler(transcript))
        except Exception as why:  # noqa: BLE001 - the loop must survive the handler
            turn.error = f"{type(why).__name__}: {why}"
            self.stats.errors += 1
            return turn

        turn.answered = answer
        if answer.strip() and self.speak:
            turn.error = self._say(answer)
            if turn.error:
                self.stats.errors += 1
        turn.seconds = round(time.time() - t0, 2)
        return turn

    def _transcribe(self, heard: audio_mod.Utterance) -> str:
        """Wav to words, then delete the wav.

        Deleting is not tidiness. `voice.listen` is T3 because a microphone is
        irreversible, and leaving raw audio of the user's room in the state
        directory would quietly undo that reasoning.

        The transcriber is built once and kept (D50): a cold load is 34-150 s,
        which is the difference between "slow" and "broken".
        """
        out_dir = self.out_dir or (ROOT / "state" / "voice")
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / "turn.wav"
        try:
            heard.save(path)
            if self._transcriber is None:
                self._transcriber = speech.Transcriber()
            return str(self._transcriber.transcribe(path))
        except Exception as why:  # noqa: BLE001
            return f"[transcription failed: {type(why).__name__}: {why}]"
        finally:
            try:
                path.unlink()
            except OSError:
                pass

    def _say(self, text: str) -> str:
        """Speak, with the microphone muted for the duration."""
        self.busy = True
        try:
            out_dir = self.out_dir or (ROOT / "state" / "voice")
            # `synthesise` takes sentences rather than prose, because a wav of
            # forty words is one long breath with nowhere to put a comma. Splitting
            # here rather than inside it keeps that a policy of this module and
            # not a surprise to anything else calling it.
            parts = speech.split_sentences(text)
            rendered = speech.synthesise(parts, out_dir, self.voice)
            if not rendered:
                return "synthesis produced no audio"
            for _sentence, wav in rendered:
                self._play(wav)
            return ""
        except Exception as why:  # noqa: BLE001
            return f"{type(why).__name__}: {why}"
        finally:
            self.busy = False

    @staticmethod
    def _play(path: Path) -> None:
        """Hand the wav to the default output device.

        A missing player is not a failure of speech - the text answer has
        already reached the user through the overlay and the log, and refusing to
        treat it as an error is what keeps a missing dependency from reading as a
        broken assistant.
        """
        import shutil
        import subprocess
        import sys

        if not path or not Path(path).exists():
            return
        if sys.platform == "win32":
            try:
                import winsound

                winsound.PlaySound(str(path), winsound.SND_FILENAME)
                return
            except Exception:  # noqa: BLE001 - fall through to a player
                pass
        player = shutil.which("ffplay") or shutil.which("aplay")
        if player:
            subprocess.run([player, "-nodisp", "-autoexit", str(path)], check=False)
        else:
            import os

            os.startfile(str(path))  # type: ignore[attr-defined]


    # -- plumbing ---------------------------------------------------------

    def _frame(self, mic: Any) -> Any:
        """One block of audio, or zeros when the stream has ended.

        Zeros rather than a sentinel because the wake word's answer to "no audio"
        and its answer to "silence" are the same thing, and a loop that exits on
        an empty stream stops working the moment a driver hiccups.

        Sets `_exhausted` when the generator runs out. That is the difference
        between a device that paused and one that is gone, and the loop needs to
        tell them apart or it spins against an empty stream forever.
        """
        import numpy as np

        self._exhausted = False
        for block in mic.frames():
            if isinstance(block, audio_mod._Overflow):
                continue
            return block
        self._exhausted = True
        return np.zeros(mic._block, dtype=np.int16)

    @staticmethod
    def _drain(mic: Any) -> None:
        """Discard one block, unconditionally."""
        for _ in mic.frames():
            return


def _describe_turn(turn: Turn) -> dict[str, Any]:
    return {
        "said": turn.said,
        "heard": turn.heard,
        "answered": turn.answered[:200],
        "seconds": turn.seconds,
        "error": turn.error,
    }


ROOT = Path(__file__).resolve().parent.parent.parent


def describe() -> dict[str, Any]:
    """What the loop would do here, and what it would cost. Loads nothing."""
    out = dict(audio_mod.describe())
    out["loop"] = {
        "default_voice": speech.DEFAULT_VOICE,
        "wake_word": audio_mod.WAKE_WORD,
        "muted_while_speaking": True,
        "cost": "no VRAM (D50); whisper ~0.27 GiB RAM, held while voice is on",
    }
    return out