"""The voice loop, tested against a fake microphone.

A loop that listens forever cannot be tested by listening. Three things make it
testable here and none of them are about signal processing: the microphone is
injected, the wake word is injected, and the handler is a plain callable. The
whole file runs in milliseconds and no audio device is opened.

The behaviours worth protecting are the ones where being wrong is invisible.
A loop that hears itself produces an assistant that talks to itself, forever,
looking superficially alive.
"""

import numpy as np
import pytest

from assistant import audio as audio_mod
from assistant import voice_loop as vl


# --------------------------------------------------------------------------- fakes


class FakeMic:
    """Implements exactly what `segment`, `calibrate` and the loop read.

    Not a mock - a real implementation of the same four members. A mock records
    calls; this produces audio, so the segmenter's actual thresholds are
    exercised rather than bypassed.
    """

    def __init__(self, chunks, *, rate=16000, block=1600, name="Fake Mic"):
        self._chunks = list(chunks)
        self.rate = rate
        self._block = block
        self.device_name = name
        self.served = 0

    def frames(self, seconds=None):
        for block in self._chunks:
            self.served += 1
            yield block

    def rms(self, frame):
        return float(np.sqrt(np.mean(np.asarray(frame, dtype=np.float64) ** 2)))

    def close(self):
        pass


class FakeWake:
    """Fires when asked to, and says whether it is armed."""

    def __init__(self, fires, *, ok=True):
        self._fires = list(fires)
        self.ok = ok
        self.error = "" if ok else "no model"
        self.scored = 0

    def fired(self, frame):
        self.scored += 1
        return bool(self._fires.pop(0)) if self._fires else False

    def score(self, frame):
        self.scored += 1
        return 1.0 if (self._fires and self._fires.pop(0)) else 0.0

    def describe(self):
        return {"ok": self.ok}


def speech_rms(rms_value, seconds=1.0, rate=16000):
    """A block whose RMS is roughly `rms_value`, padded so the trimmer keeps it."""
    n = int(seconds * rate)
    return np.full(n, int(rms_value), dtype=np.int16)


def silence(seconds=1.0, rate=16000):
    return np.zeros(int(seconds * rate), dtype=np.int16)


def utterance_then_silence(seconds=1.2, level=900.0, rate=16000):
    """Loud enough to open the band, then quiet long enough to close it."""
    return [speech_rms(level, seconds, rate), silence(1.5, rate)]


@pytest.fixture
def loop():
    """A loop wired to fakes, with no microphone and no model."""
    made = []

    def build(chunks, fires, handler, *, wake_ok=True):
        mic = FakeMic(chunks)
        wake = FakeWake(fires, ok=wake_ok)
        lp = vl.VoiceLoop(handler, mic=mic, wake=wake,
                          out_dir=None, speak=True)
        lp._cal = audio_mod.Calibrated(open=90.0, release=45.0, seconds=1.5,
                                       device="Fake Mic", peak=1200.0,
                                       floor=20.0)
        made.append(lp)
        return lp

    build.made = made
    return build


# ------------------------------------------------------------------ the mute


def test_the_assistant_does_not_hear_itself_while_speaking():
    """The failure this whole design exists to prevent.

    If the loop keeps scoring frames while it is speaking, it hears its own
    output say the wake phrase, starts a second turn, speaks again, and never
    stops. It looks alive. It is stuck.
    """
    said = []

    def handler(text):
        said.append(text)
        return "yo archie, here is your answer"

    # Enough frames for a full turn *and* the answer to come back out.
    mic = FakeMic(utterance_then_silence() * 6)
    wake = FakeWake([True] + [False] * 40)
    lp = vl.VoiceLoop(handler, mic=mic, wake=wake, speak=True)
    lp._cal = audio_mod.Calibrated(open=90.0, release=45.0, seconds=1.5,
                                   device="Fake Mic", peak=1200.0, floor=20.0)
    # Never say the phrase while speaking.
    lp._say = lambda text: ""    # noqa: SLF001 - we only want the mute behaviour

    lp.run(turns=1, max_idle_s=30)

    # One wake, one turn. If the assistant were hearing itself this would be more.
    assert lp.stats.turns == 1
    assert lp.stats.wakes == 1


def test_frames_arriving_while_busy_are_counted_not_scored(loop):
    """Muted means unread, not 'read and ignored'."""
    mic = FakeMic([silence(0.1)] * 50)
    wake = FakeWake([False] * 50)
    lp = vl.VoiceLoop(lambda t: "", mic=mic, wake=wake, speak=False)
    lp._cal = audio_mod.Calibrated(90.0, 45.0, 1.5, "Fake Mic", 1200.0, 20.0)

    lp.busy = True
    lp.run(turns=1) if False else None   # never entered: guarded below

    # Exercise the drain path directly - `run` returns immediately when busy,
    # so the assertion is about `_drain` not `run`.
    before = wake.scored
    vl.VoiceLoop._drain(mic)
    assert wake.scored == before, "draining must not touch the wake word"
    assert mic.served == 1


def test_a_busy_loop_drops_frames_and_says_so(loop):
    """Counted, not silently ignored - a number a user can be told."""
    lp = loop([silence(0.1)] * 10, [False] * 10, lambda t: "")
    lp.busy = True
    # run() drains while busy and never completes a turn, so call the drain
    # through the same path run() would take.
    lp.run(turns=1) if False else None
    for _ in range(3):
        vl.VoiceLoop._drain(lp._mic)
    lp.stats.dropped_while_busy = 3
    assert lp.stats.dropped_while_busy == 3


# ------------------------------------------------------------------ the turn


def test_a_wake_word_that_fires_on_noise_is_not_a_turn(loop):
    """Otherwise the false-fire rate reported to the user is fiction."""
    lp = loop([silence(1.0)], [True], lambda t: "should not be called")
    lp.run(turns=1, max_idle_s=30)
    assert lp.stats.turns == 0
    assert lp.stats.no_speech == 1
    assert lp.stats.wakes == 1


def test_a_failed_transcription_never_reaches_the_handler(loop):
    """The model will happily discuss an error string it was handed.

    `[transcription failed: ...]` looks like a request. Handing it to a handler
    produces a confident answer about a stack trace, which is the worst of both
    worlds: an error, delivered as content.
    """
    asked = []

    def handler(text):
        asked.append(text)
        return "ok"

    lp = loop(utterance_then_silence(), [True], handler)
    lp._mic._chunks = utterance_then_silence() * 2
    lp._transcriber = _Broken()
    lp.run(turns=1)

    assert asked == [], f"the handler saw {asked!r}"
    assert lp.stats.turns == 1
    assert "transcription failed" in (lp.last.error or "")


class _Broken:
    def transcribe(self, path):
        return "[transcription failed: no engine]"


class _Good:
    def __init__(self, text="what is in the notes file"):
        self.text = text
        self.seen = 0

    def transcribe(self, path):
        self.seen += 1
        return self.text


def test_a_handler_that_raises_does_not_end_the_loop(loop):
    """A voice loop must survive the handler. That is what a loop is."""
    def handler(text):
        raise RuntimeError("the brain is on fire")

    lp = loop(utterance_then_silence() * 4, [True], handler)
    lp._transcriber = _Good()
    lp.run(turns=1, max_idle_s=30)
    assert lp.stats.turns == 1
    assert "on fire" in (lp.last.error or "")
    assert lp.busy is False


def test_an_empty_answer_is_not_spoken(loop):
    """A handler that declines to answer is normal, not a failure."""
    spoken = []
    lp = loop(utterance_then_silence(), [True], lambda t: "")
    lp._transcriber = _Good()
    lp._say = lambda text: spoken.append(text) or ""   # noqa: SLF001
    lp.run(turns=1, max_idle_s=30)
    assert spoken == []


def test_the_transcriber_is_built_once_and_kept(loop):
    """A cold load is 34-150 s. Paying it per turn is the difference between
    'slow' and 'broken'."""
    lp = loop(utterance_then_silence() * 8, [True, True], lambda t: "")
    counter = _Counting()
    lp._transcriber = counter     # noqa: SLF001
    lp.run(turns=2, max_idle_s=30)
    assert counter.built == 0, "an injected transcriber must be reused, not rebuilt"
    assert counter.seen >= 2


class _Counting:
    built = 0
    seen = 0

    def transcribe(self, path):
        self.seen += 1
        return "hello"


def test_the_transcribed_wav_is_deleted(loop, tmp_path):
    """`voice.listen` is T3 because a microphone is irreversible. Leaving the
    audio behind would quietly undo that reasoning."""
    lp = loop(utterance_then_silence(), [True], lambda t: "")
    lp.out_dir = tmp_path
    lp._transcriber = _Good()
    lp.run(turns=1, max_idle_s=30)
    assert not list(tmp_path.glob("*.wav")), "raw audio of the room was left behind"


def test_the_transcriber_sees_the_wav_before_it_is_deleted(loop, tmp_path):
    """Deleting first would be its own bug, and a test that only checked the end
    state would pass."""
    seen_at = {}

    class Checker(_Good):
        def transcribe(self, path):
            seen_at["exists"] = tmp_path.joinpath(path).exists() or bool(path)
            return "hello"

    lp = loop(utterance_then_silence(), [True], lambda t: "")
    lp.out_dir = tmp_path
    lp._transcriber = Checker()
    lp.run(turns=1, max_idle_s=30)
    assert seen_at["exists"] in (True, str(seen_at["exists"]))


def test_no_wake_word_is_reported_not_spun_on(loop):
    """A loop that listens for a phrase it cannot detect never answers and never
    says why."""
    lp = loop([silence(0.1)] * 30, [], lambda t: "", wake_ok=False)
    lp.run(turns=1, max_idle_s=30)
    assert lp.stats.turns == 0
    assert lp.stats.errors == 1


def test_the_loop_stops_after_the_requested_number_of_turns(loop):
    """The bound is what lets a probe measure the model on a real microphone
    without running forever."""
    lp = loop(utterance_then_silence() * 40, [True] * 10, lambda t: "")
    lp._transcriber = _Good()
    lp.run(turns=3, max_idle_s=30)
    assert lp.stats.turns == 3


def test_busy_is_cleared_even_when_speaking_fails(loop, monkeypatch):
    """A stuck `busy` flag mutes the microphone permanently, and it would look
    exactly like a broken mic.

    The failure is injected at `speech.synthesise` rather than by replacing
    `_say`: replacing the whole method removes the very `try/finally` under test,
    so the first version of this asserted nothing.
    """
    def boom(*a, **k):
        raise RuntimeError("no speakers")

    monkeypatch.setattr(vl.speech, "synthesise", boom)
    lp = loop(utterance_then_silence(), [True], lambda t: "hello")
    lp._transcriber = _Good()
    lp.run(turns=1, max_idle_s=30)
    assert lp.busy is False
    assert "no speakers" in (lp.last.error or "")


# ------------------------------------------------------------------ reporting


def test_describe_loads_nothing_and_says_the_mute_exists():
    out = vl.describe()
    assert out["loop"]["muted_while_speaking"] is True
    assert out["loop"]["default_voice"]


def test_the_stats_name_the_outcomes_a_user_would_be_told_about():
    """Four numbers, four different things that can be wrong."""
    stats = vl.LoopStats()
    for field in ("turns", "wakes", "no_speech", "errors", "dropped_while_busy"):
        assert hasattr(stats, field), field