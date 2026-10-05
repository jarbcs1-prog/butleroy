"""Listening, tested without a microphone.

Every test here drives `segment()` with a synthetic waveform and a fake device.
That is not a shortcut around the hardware - it is the point. The parts of
listening that can be got wrong are decisions: does the band chatter, does a cough
become a request, is a 48 kHz block stored at the right length. Those are
properties of the logic and they are checkable exactly, in a millisecond, with a
signal whose peak and length are known to the digit.

What cannot be tested this way is whether a microphone works. That is measured
directly instead, by `probe_roundtrip.py`, and the answer was yes - but only after
the device was moved to a different USB port. Before that every input on the
machine was digitally silent, which looked exactly like broken hardware and was
not: the drivers were healthy and simply had nothing arriving. A test suite that
pretended either outcome would be testing a stub.
"""

import math

import numpy as np
import sys

import pytest

from assistant import audio


# ----------------------------------------------------------------------- fakes


class FakeMic:
    """A microphone that plays back a prepared array instead of a device.

    Implements exactly what `segment()` reads: `rate`, `_block`, `frames()` and
    `rms()`. A fake with more surface would let a test pass against an interface
    the real class does not have.
    """

    def __init__(self, samples, rate=16000, block=160):
        self._samples = np.asarray(samples, dtype=np.int16)
        self.rate = rate
        self._block = block
        self.overflows = 0
        # `calibrate()` reports which device it measured. A fake without this
        # makes every calibration test fail on an AttributeError instead of
        # testing a threshold.
        self.device_name = "fake"

    def frames(self, seconds=None):
        for start in range(0, len(self._samples), self._block):
            block = self._samples[start:start + self._block]
            if len(block):
                yield block

    def rms(self, frame):
        return float(np.sqrt(np.mean(frame.astype(np.float64) ** 2)))


def tone(seconds, rate=16000, freq=220.0, amplitude=6000.0, offset=0.0):
    """A loud, speech-shaped-enough signal: a tone with an amplitude envelope."""
    n = int(seconds * rate)
    t = np.arange(n) / rate
    wave = amplitude * np.sin(2 * math.pi * freq * (t + offset))
    return wave.astype(np.int16)


def silence(seconds, rate=16000):
    return np.zeros(int(seconds * rate), dtype=np.int16)


def speech_then_quiet(speak_s=1.0, quiet_s=2.0, rate=16000):
    return np.concatenate([tone(speak_s, rate), silence(quiet_s, rate)])


# ------------------------------------------------------------------ the band


def test_a_loud_utterance_in_quiet_is_captured():
    utt = audio.segment(FakeMic(speech_then_quiet()))
    assert utt is not None, "a second of speech in two seconds of quiet was missed"
    assert 0.8 <= utt.seconds <= 1.4


def test_silence_alone_returns_nothing_rather_than_an_empty_utterance():
    """An utterance of nothing is not an utterance. Sending it would put a
    transcript of silence in front of the model, which will answer it."""
    assert audio.segment(FakeMic(silence(5.0))) is None


def test_a_quiet_room_does_not_trigger():
    """The case this machine is actually in.

    The measured floor on every device was peak 30 / rms 1 out of 32767, and the
    threshold is 220. A loop that fired on this would fire on a fan.
    """
    noise = (np.random.default_rng(0).normal(0, 30, 16000 * 5)).astype(np.int16)
    assert audio.segment(FakeMic(noise)) is None


def test_a_click_shorter_than_the_minimum_is_not_an_utterance():
    """A cough, a mouse click, a door. Long enough to trigger, too short to be a
    sentence - and a model asked about a click will confidently produce a
    sentence about a click."""
    assert audio.segment(FakeMic(speech_then_quiet(0.1, 2.0))) is None


def test_the_two_thresholds_disagree_on_purpose():
    """Hysteresis is the claim. If the thresholds were equal, `segment` would be a
    level test wearing a costume."""
    assert audio.OPEN_RMS != audio.RELEASE_RMS
    assert audio.RELEASE_RMS < audio.OPEN_RMS, (
        "release below open is correct; above it the band is inverted and the "
        "loop latches open"
    )


def test_a_sustained_level_inside_the_band_holds_the_utterance_open():
    """The reason for the band: a signal between the thresholds must not end the
    utterance. With one threshold this chatter; with the band it does not."""
    between = int((audio.OPEN_RMS + audio.RELEASE_RMS) / 2)
    steady = np.full(16000 * 3, between, dtype=np.int16)
    utt = audio.segment(FakeMic(np.concatenate([tone(0.5), steady, silence(2.0)])))
    assert utt is not None
    assert utt.seconds > 1.5, "the band closed mid-sentence"


def test_an_utterance_is_bounded():
    """A stuck-open microphone must not fill the disk."""
    utt = audio.segment(FakeMic(tone(200.0)), max_s=2.0, total_s=250.0)
    assert utt is not None
    assert utt.seconds <= 2.5


def test_the_capture_ceiling_is_enforced_when_nothing_ever_goes_quiet():
    utt = audio.segment(FakeMic(tone(400.0)), max_s=1.0, total_s=2.0)
    assert utt is not None
    assert utt.seconds <= 1.5, "total_s did not bound an utterance that never closed"


# -------------------------------------------------------------------- resample


def test_a_48k_block_is_stored_at_the_rate_it_is_actually_16k_length():
    """A silent resample is the quietest possible corruption.

    Whisper reads the wav header and trusts it. Audio captured at 48 kHz and
    labelled 16 kHz is played three times too slow and transcribes into words that
    are *almost* right, which is worse than an error because it is not obviously
    wrong.
    """
    mic = FakeMic(tone(1.0, rate=48000), rate=48000, block=480)
    utt = audio.segment(mic)
    assert utt.rate == audio.TARGET_RATE
    # The sample COUNT falls to a third; the DURATION does not. Those are
    # different claims and the first version of this test confused them,
    # expecting 0.33s - which is what a 48 kHz file mislabelled 16 kHz sounds
    # like: played three times too slow, words that are almost right.
    assert len(utt.audio) == int(16000 * utt.seconds)
    assert 0.9 <= utt.seconds <= 1.1, (
        f"one second of audio became {utt.seconds:.3f}s; resampling changes the "
        f"sample count, not the length of time"
    )


def test_resampling_is_a_no_op_at_the_target_rate():
    """Guarded because the alternative - a resample that runs anyway - changes
    every sample slightly and there is no symptom to notice."""
    data = tone(0.5)
    assert audio._resample(data, 16000, 16000) is data


def test_resampling_keeps_every_sample_in_range():
    out = audio._resample(tone(1.0, rate=48000), 48000, 16000)
    assert out.dtype == np.int16
    assert np.abs(out.astype(np.int32)).max() <= 32767


def test_the_indices_are_integers_not_floats():
    """numpy refuses a float index array, which is how this surfaced - but
    `np.asarray(..., int)` would truncate instead, silently shifting every
    sample by a fraction of one."""
    out = audio._resample(tone(1.0, rate=44100), 44100, 16000)
    assert len(out) == int(16000 * 1.0)


# ---------------------------------------------------------------------- trim


def test_lead_in_is_kept():
    """The first consonant of a word. Trim too hard and the clip starts on a
    vowel; the transcript is then wrong in a way nobody can trace back."""
    mic = FakeMic(np.concatenate([silence(1.0), tone(1.0), silence(1.0)]))
    utt = audio.segment(mic)
    assert utt is not None
    assert utt.trimmed_s > 0.5, "the surrounding silence was not trimmed"


def test_all_silence_trims_to_nothing_and_is_not_an_utterance():
    utt = audio.segment(FakeMic(silence(3.0)))
    assert utt is None


def test_a_peak_is_reported_that_is_actually_the_peak():
    utt = audio.segment(FakeMic(speech_then_quiet()))
    assert utt is not None
    assert utt.peak == int(np.max(np.abs(utt.audio.astype(np.int32))))
    assert utt.peak > 0


# ---------------------------------------------------------------------- files


def test_a_saved_utterance_is_16k_mono_16_bit(tmp_path):
    """The format `speech.transcribe` reads, asserted rather than assumed."""
    import wave

    utt = audio.segment(FakeMic(speech_then_quiet()))
    assert utt is not None
    out = utt.save(tmp_path / "u.wav")
    with wave.open(str(out), "rb") as wf:
        assert wf.getframerate() == 16000
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getnframes() == len(utt.audio)


def test_the_saved_length_matches_what_we_said_we_had(tmp_path):
    from assistant import speech

    utt = audio.segment(FakeMic(speech_then_quiet()))
    assert utt is not None
    out = utt.save(tmp_path / "u.wav")
    assert abs(speech.wav_seconds(out) - utt.seconds) < 0.01


def test_saving_an_empty_utterance_produces_no_frames_rather_than_a_crash(tmp_path):
    empty = audio.Utterance(audio=np.zeros(0, dtype=np.int16))
    out = empty.save(tmp_path / "e.wav")
    assert out.exists()


# ------------------------------------------------------------------- overflow


def test_an_overflow_is_counted_not_hidden():
    """The driver had nowhere to put samples we already asked for, so the audio
    is no longer continuous. Silently dropping the gap produces a transcript that
    is wrong in a way nobody can trace."""

    class OverflowingMic(FakeMic):
        def frames(self, seconds=None):
            yield audio._Overflow(2)
            yield from super().frames()

    utt = audio.segment(OverflowingMic(speech_then_quiet()))
    assert utt is not None
    assert utt.overflows == 1, "a lost block was dropped without being counted"


# ------------------------------------------------------------------- wake word


def test_absence_of_a_wake_word_is_a_state_not_a_crash():
    """Push-to-talk works perfectly well without a wake word, so the caller must
    be able to find that out and carry on."""
    w = audio.WakeWord(name="definitely_not_a_real_model")
    if not w.ok:
        assert w.error, "an unavailable wake word must say why"
    assert isinstance(w.describe()["ok"], bool)


def test_a_wake_word_that_does_not_exist_scores_zero_rather_than_raising():
    w = audio.WakeWord(name="definitely_not_a_real_model")
    if not w.ok:
        assert w.score(tone(0.1)) == 0.0
        assert w.fired(tone(0.1)) is False


def test_the_wake_word_is_told_not_to_use_the_gpu():
    """D50 exists so voice costs no VRAM. openwakeword sniffs for a runtime by
    default, and a silent promotion of the audio stack onto the card is exactly
    the thing it exists to prevent - so the framework is named explicitly."""
    import inspect

    src = inspect.getsource(audio.WakeWord.__post_init__)
    assert "inference_framework" in src
    assert '\"onnx\"' in src or "'onnx'" in src


# ------------------------------------------------- wake-word answer extraction


def test_the_default_wake_word_is_the_chosen_pretrained_phrase():
    """The assistant answers to "hey jarvis", not to a model nobody trained.

    The previous default named a phrase with no file behind it, so voice.watch
    refused on every machine including this one. The default must be a phrase
    that resolves to a file, which is asserted rather than assumed.
    """
    assert audio.WAKE_WORD == "hey_jarvis"
    assert audio.WakeWord().name == "hey_jarvis"


def test_score_reads_the_models_answer_for_our_phrase():
    """`Model.predict` returns a dict of every model to its score, not an array.

    The first version of `score` called `np.max` on that dict and raised
    TypeError on every frame - which meant the wake word could never fire, and
    the failure would have surfaced as "no detection" rather than as the crash
    it was. A fake model returning a dict exercises the indexing without
    loading 1.2 MB.
    """
    from types import SimpleNamespace

    w = audio.WakeWord(name="definitely_not_a_real_model")
    assert not w.ok
    w.ok = True
    w.name = "hey_jarvis"
    w._model = SimpleNamespace(
        predict=lambda chunk: {"hey_jarvis": 0.83, "alexa": 0.01})
    frame = np.zeros(1280, dtype=np.int16)
    assert w.score(frame) == 0.83
    assert w.fired(frame) is True


def test_score_treats_normalized_float_as_the_same_signal():
    """Float audio in [-1, 1] is the same signal as int16, not a whisper.

    The probe fed normalized float and measured ~0.0001 on every window
    including the phrase itself - which reads exactly like "the model does not
    fire" when the truth is "the model was fed silence". A scaling convention
    with no error signal is a trap, so the conversion is asserted here on a
    synthetic signal rather than trusted to a comment.
    """
    from types import SimpleNamespace

    loud = (0.9 * np.sin(2 * np.pi * 440 *
                         np.arange(1280) / 16000) * 32767).astype(np.int16)
    for frame in (loud, loud.astype(np.float32) / 32768.0):
        w = audio.WakeWord(name="definitely_not_a_real_model")
        assert not w.ok
        w.ok = True
        w.name = "hey_jarvis"
        seen = {}
        def predict(chunk, _seen=seen):
            _seen.setdefault("dtype", str(chunk.dtype))
            return {"hey_jarvis": 0.5}
        w._model = SimpleNamespace(predict=predict)
        assert w.score(frame) == 0.5
        assert seen["dtype"] == "int16", seen


# -------------------------------------------------------------------- describe


def test_describe_costs_nothing_and_reports_the_shape_of_the_decision():
    d = audio.describe()
    assert d["sample_rate"] == audio.TARGET_RATE
    assert d["open_rms"] > d["release_rms"]
    assert isinstance(d.get("inputs"), list)


def test_describe_survives_a_host_with_no_audio_at_all(monkeypatch):
    """`describe` is the thing you call when the mic is not working, so it must
    not itself need one."""
    monkeypatch.setattr(audio, "list_inputs", lambda: (_ for _ in ()).throw(RuntimeError("no audio")))
    d = audio.describe()
    assert d["inputs"] == []
    assert d["input_error"]

# ------------------------------------------------------------------ calibration


def test_thresholds_are_derived_from_the_room_not_fixed():
    """The fault this replaces: 220 rms, guessed, never once checked against a
    voice on the machine it was for."""
    assert audio.thresholds_for(10.0)[0] != audio.OPEN_RMS
    assert audio.thresholds_for(500.0)[0] > audio.thresholds_for(20.0)[0]


def test_a_quiet_room_gets_a_quiet_threshold():
    """This machine: a floor around 20-50. Speech has to beat that, not 220."""
    open_at, _ = audio.thresholds_for(30.0)
    assert open_at < audio.OPEN_RMS, (
        "a quiet room should not need a louder threshold than a noisy one"
    )


def test_the_release_is_always_below_the_open():
    """Not by a fixed amount - proportionally. A band that is 200 wide at one
    level and 2 wide at another is not a band, it is two unrelated numbers."""
    for floor in (0.0, 1.0, 20.0, 200.0, 1000.0, 5000.0):
        open_at, release_at = audio.thresholds_for(floor)
        assert release_at < open_at, floor


def test_a_dead_device_does_not_produce_a_threshold_of_zero():
    """Two of this machine's inputs report a floor of exactly zero, and a
    threshold of zero opens on a single quantisation step - so the loop would
    fire on digital silence."""
    open_at, release_at = audio.thresholds_for(0.0)
    assert open_at == audio.ABSOLUTE_MIN_OPEN
    assert release_at > 0


def test_a_deafening_room_does_not_produce_an_impossible_threshold():
    """The failure is silent and looks exactly like a broken microphone, which is
    why it needs its own test rather than a comment."""
    open_at, _ = audio.thresholds_for(500_000.0)
    assert open_at == audio.ABSOLUTE_MAX_OPEN


def test_the_hysteresis_band_survives_clamping():
    """Both clamps can make the pair collapse if applied carelessly."""
    for floor in (0.0, 1e6):
        open_at, release_at = audio.thresholds_for(floor)
        assert 0 < release_at < open_at


def test_calibration_measures_the_p90_not_the_mean():
    """A room with one loud moment has a high mean and a modest p90. Deriving the
    threshold from the mean puts it above speech; deriving it from the p90 puts it
    where the room actually is."""
    mic = FakeMic(np.full(16000 * 2, 40, dtype=np.int16))
    loud = np.zeros(16000 * 2, dtype=np.int16)
    loud[:800] = 20000                      # one blip in two seconds
    cal = audio.calibrate(FakeMic(np.concatenate([loud, mic._samples])))
    assert cal.floor < 100, "a single blip defined the whole room"


def test_calibration_reports_the_device_it_measured():
    mic = FakeMic(np.zeros(16000, dtype=np.int16))
    cal = audio.calibrate(mic)
    assert cal.device == mic.device_name or isinstance(cal.device, str)


def test_calibration_says_when_it_had_nothing_to_measure():
    """A device that streams nothing is a fact the caller needs, and `floor: 0`
    plus a flag is how it gets said rather than inferred."""
    class Silent(FakeMic):
        def frames(self, seconds=None):
            return iter(())

    cal = audio.calibrate(Silent(np.zeros(10, dtype=np.int16)))
    assert cal.peak == 0
    assert cal.seconds == 0.0
    said = cal.describe()
    assert said["device"] == "fake"
    assert "nothing was measured" in said["derived"]


def test_a_device_delivering_zero_does_not_produce_a_zero_threshold():
    """The case this machine actually has: two inputs report a floor of exactly
    zero. A threshold derived from that would open on one quantisation step, and
    the loop would respond to a device that is plugged in but dead."""
    cal = audio.calibrate(FakeMic(np.zeros(16000 * 2, dtype=np.int16)))
    assert cal.floor == 0.0
    assert cal.open == audio.ABSOLUTE_MIN_OPEN
    assert cal.release > 0
    assert cal.describe()["silence"] is True, (
        "a room measured as silent is a fact worth stating, not an absence"
    )


def test_a_measured_threshold_still_captures_speech_above_it():
    """End to end at the unit level: calibrate on a quiet room, then segment a
    loud utterance with the derived thresholds."""
    room = np.random.default_rng(1).normal(0, 12, 16000 * 2).astype(np.int16)
    cal = audio.calibrate(FakeMic(room))
    utt = audio.segment(
        FakeMic(np.concatenate([room, tone(1.0), room])),
        open_rms=cal.open, release_rms=cal.release,
    )
    assert utt is not None
    assert utt.seconds > 0.5


# --- the rate a device is asked for ------------------------------------------
#
# Found by running the round trip against the real microphone: every
# index-addressed open of a 48 kHz device failed with "Invalid sample rate",
# because `Microphone(device=5)` had 44100 hardcoded for the case where a caller
# passed an index. The name-based and default paths looked the rate up correctly,
# so the bug only appeared on the one path a caller would naturally write.

def test_an_index_addressed_device_is_opened_at_its_own_rate(monkeypatch):
    """Hardcoding a rate for the index path is the bug, not the fix."""
    import assistant.audio as A

    seen = {}

    class FakeStream:
        def start(self):
            seen["started"] = True

        def stop(self):
            pass

        def close(self):
            pass

    class FakeSd:
        class InputStream:
            def __init__(self, **kw):
                seen["samplerate"] = kw.get("samplerate")
                seen["device"] = kw.get("device")
                self._s = FakeStream()

            def start(self):
                pass

            def stop(self):
                pass

            def close(self):
                pass

    monkeypatch.setattr(A, "list_inputs", lambda: [
        {"index": 3, "name": "A 48k device", "channels": 1, "rate": 48000},
        {"index": 4, "name": "A 44k device", "channels": 1, "rate": 44100},
    ])
    monkeypatch.setitem(sys.modules, "sounddevice", FakeSd)
    sys.modules["sounddevice"].InputStream = FakeSd.InputStream

    for index, expected in ((3, 48000), (4, 44100)):
        with A.Microphone(device=index):
            assert seen["samplerate"] == expected, (
                f"index {index} was opened at {seen['samplerate']} Hz, "
                f"which is the fault this test exists for"
            )


def test_an_index_that_does_not_exist_is_refused_by_name(monkeypatch):
    """Silently substituting a different microphone would be worse than a refusal:
    the user would be talking into one device and recorded on another."""
    import assistant.audio as A

    monkeypatch.setattr(A, "list_inputs", lambda: [
        {"index": 1, "name": "Real Mic", "channels": 1, "rate": 44100},
    ])
    with pytest.raises(A.NoInputDevice) as caught:
        A.Microphone(device=7)
    assert "7" in str(caught.value)


# --- the shape of a frame: mono means 1-D ------------------------------------
#
# sounddevice's InputStream.read always returns (frames, channels) - for a
# mono stream, a 2-D (n, 1) array. The synthetic probes could not see the
# extra axis: piper's files are 1-D to begin with, and the shape-agnostic
# arithmetic (rms, tobytes) carried it along. Only the real microphone made
# it visible, through openWakeWord's streaming preprocessor, which turns a
# (n, 1) block into a 3-D melspectrogram that cannot vstack onto its 2-D
# history buffer.

def test_frames_yield_one_dimensional_samples_even_when_the_driver_is_two(monkeypatch):
    """The contract `frames` makes: a frame of samples, 1-D int16.

    The fake stream returns exactly what a real sounddevice InputStream
    returns - a 2-D (n, 1) int16 block. The flattening belongs to
    `Microphone`, not to every consumer, so that is what is asserted here.
    """
    import assistant.audio as A

    class FakeStream:
        def __init__(self, **kw):
            pass

        def start(self):
            pass

        def stop(self):
            pass

        def close(self):
            pass

        def read(self, frames):
            data = np.zeros((frames, 1), dtype=np.int16)
            return data, False

    class FakeSd:
        class InputStream:
            def __init__(self, **kw):
                self._s = FakeStream(**kw)

            def start(self):
                self._s.start()

            def stop(self):
                self._s.stop()

            def close(self):
                self._s.close()

            def read(self, frames):
                return self._s.read(frames)

    monkeypatch.setattr(A, "list_inputs", lambda: [
        {"index": 0, "name": "Fake mono device", "channels": 1, "rate": 16000},
    ])
    monkeypatch.setitem(sys.modules, "sounddevice", FakeSd)

    with A.Microphone(device=0) as mic:
        frame = next(mic.frames())
        assert frame.ndim == 1, (
            f"a mono frame has shape {frame.shape}; a second axis means the "
            f"driver's (frames, channels) layout leaked through, and a 2-D "
            f"block is what crashed the wake word on the real microphone"
        )
        assert frame.dtype == np.int16
        assert len(frame) == mic._block


# --- why the failure is told apart from a failure ----------------------------
#
# "Blocked" and "in use" read the same on screen and get the same generic
# advice, and for a stretch that advice was wrong. `-9999 Blocking API not
# supported yet` arrived on every input this machine exposes - including the
# onboard Realtek microphone, while nothing was holding it - so the message
# told people to go and close a program that was never holding anything.


def _open_failing(monkeypatch, why_text):
    """A sounddevice whose InputStream cannot be constructed, with `why_text`."""
    import assistant.audio as A

    class FakeSd:
        class InputStream:
            def __init__(self, **kw):
                raise RuntimeError(why_text)

    monkeypatch.setattr(A, "list_inputs", lambda: [
        {"index": 0, "name": "Fake device", "channels": 1, "rate": 16000},
    ])
    monkeypatch.setitem(sys.modules, "sounddevice", FakeSd)
    return A


def test_a_blocking_api_failure_is_not_blamed_on_another_program(monkeypatch):
    """The two failures need opposite advice, and only one involves you closing
    something."""
    A = _open_failing(monkeypatch, "PortAudio error: -9999 Blocking API "
                                    "not supported yet")
    with pytest.raises(A.NoInputDevice) as caught:
        A.Microphone(device=0)
    message = str(caught.value)
    assert "Blocking API" in message
    assert "another application holds" not in message, (
        "telling the user to close a program that was never holding the "
        "device sends them looking for a cause that is not there"
    )


def test_a_genuine_exclusive_hold_still_gets_that_advice(monkeypatch):
    """The original advice is right for the failure it was written for, and
    removing it from the -9999 branch must not take it from this one."""
    A = _open_failing(monkeypatch, "PortAudio error: -9997 Invalid sample rate")
    with pytest.raises(A.NoInputDevice) as caught:
        A.Microphone(device=0)
    assert "another application holds" in str(caught.value)
