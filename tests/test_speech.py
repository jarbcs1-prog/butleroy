"""Speaking and listening, and the measurements the design rests on.

The expensive checks here are marked and skipped by default: loading piper and
whisper takes seconds and downloads tens of megabytes on a cold machine, and a
test suite that does that on every run is a test suite nobody runs. What is always
checked is the part that costs nothing and is easy to get wrong - paths, refusals,
the memory probe actually working - because those are the parts that fail silently.

Run the slow ones with `LOCALAIZED_SLOW_TESTS=1`.
"""

import os
import pathlib
import struct
import wave

import pytest

from assistant import speech

SLOW = os.environ.get("LOCALAIZED_SLOW_TESTS") == "1"
slow = pytest.mark.skipif(not SLOW, reason="set LOCALAIZED_SLOW_TESTS=1 to run")


# --- paths and refusals ------------------------------------------------------


def test_the_default_voice_is_one_we_know_where_to_find():
    """A default that cannot be downloaded is a default that does not work."""
    assert speech.DEFAULT_VOICE in speech.VOICE_PATHS


def test_an_unknown_voice_is_refused_by_name():
    """Refused rather than guessed.

    Guessing the layout is how a request for a 63 MB voice turns into a download
    of a 500 MB one, and both present to the user as "voice is broken".
    """
    with pytest.raises(KeyError) as caught:
        speech.voice_files("en_US-marshmallow-ultra-expressive")
    message = str(caught.value)
    assert "known voices" in message
    assert speech.DEFAULT_VOICE in message


def test_a_voice_path_is_inside_the_project():
    """Voices live in the project so they can be deleted with it."""
    for name in speech.VOICE_PATHS:
        model, config = speech.voice_files(name)
        assert speech.VOICES in model.parents
        assert speech.VOICES in config.parents


def test_the_model_and_the_config_are_both_accounted_for():
    """A voice without its `.onnx.json` loads and then speaks at the wrong speed.

    Piper reads its sample rate and phoneme map from that config. Missing it does
    not error - it produces fast chipmunk speech that is almost intelligible,
    which is a much worse failure than a refusal.
    """
    for name in speech.VOICE_PATHS:
        model, config = speech.voice_files(name)
        assert model.suffix == ".onnx"
        assert config.suffix == ".json"
        assert str(model.with_suffix(".onnx.json")) == str(config)


def test_no_voice_is_resident_until_something_asks_for_one():
    """Import cost is paid by the daemon on every start and by every test."""
    state = speech.describe()
    assert state["loaded"] is False
    assert "nothing is resident until then" in state["note"]


def test_describe_is_free_and_says_what_voice_would_cost():
    """The daemon can answer "what is voice using" before the user has spoken."""
    assert speech.describe()["whisper_model"] == speech.DEFAULT_WHISPER
    assert speech.describe()["device"] == "cpu"


# --- wav handling -------------------------------------------------------------


def test_a_wav_length_is_read_from_the_header(tmp_path):
    """Not by counting samples through soundfile: a two-second wav should not take
    a round trip to a library to measure."""
    target = tmp_path / "two_seconds.wav"
    with wave.open(str(target), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(speech.SAMPLE_RATE)
        fh.writeframes(b"\x00\x00" * speech.SAMPLE_RATE * 2)
    assert speech.wav_seconds(target) == pytest.approx(2.0, abs=0.01)


def test_capture_format_is_what_whisper_wants():
    """16 kHz mono. Capturing at 48 kHz would store three times the bytes to be
    resampled down to the same place."""
    assert speech.SAMPLE_RATE == 16000
    assert speech.CHANNELS == 1
    assert speech.SAMPLE_WIDTH == 2


def test_synthesise_writes_the_rate_it_promises(tmp_path):
    """The docstring says 16 kHz mono. Piper renders 22050 Hz.

    For a stretch the code wrote piper's native rate into the header while the
    docstring claimed 16 kHz - and whisper resamples internally, so the round
    trip stayed green and nobody noticed. The probe that feeds raw samples to
    the wake word noticed: it measured a resampling rather than a wake word.
    A file that is not what its header is not the only lie; a function that is
    not what its docstring says is the same one.
    """
    import wave

    made = speech.synthesise(["hey jarvis"], tmp_path)
    assert made, "piper produced no audio"
    with wave.open(str(made[0][1]), "rb") as fh:
        assert fh.getframerate() == speech.SAMPLE_RATE == 16000
        assert fh.getnchannels() == 1
        assert fh.getsampwidth() == speech.SAMPLE_WIDTH


# --- the memory probe ---------------------------------------------------------


def test_the_memory_probe_reports_a_real_number():
    """It once reported 0.00 GiB for a loaded model, because the signatures were
    missing and ctypes returned a 32-bit int for the BOOL - the call failed
    silently every time.

    An identical number across cases that obviously differ is the tell. So this
    asserts the probe can see a real difference, which is the only way to know it
    is measuring.
    """
    before = speech.resident_gib()
    # Not 0.00, and not an arbitrary floor: a freshly started interpreter with
    # psapi loaded is tens of megabytes. The ballast check below is the one that
    # actually catches a broken probe; this only catches "it always returns zero".
    assert before > 0.005, f"a running Python cannot be using {before:.4f} GiB"

    ballast = [bytearray(24 * 1024 * 1024) for _ in range(4)]   # ~96 MiB
    for block in ballast:          # touch every page so it is really resident
        block[::4096] = b"\x01" * len(block[::4096])
    after = speech.resident_gib()
    del ballast
    assert after > before + 0.05, (
        f"allocating ~96 MiB moved the working set by only "
        f"{after - before:.4f} GiB, so the probe is not working"
    )


# --- the measurement that decided the design (slow) ---------------------------


@slow
def test_whisper_transcribes_what_piper_said(tmp_path):
    """The round trip that justifies the whole of Phase 5, checked for accuracy.

    Piper renders a sentence we chose; whisper transcribes it; the words we
    started from are the answer key. Ground truth from our own text-to-speech
    rather than a corpus transcribed on someone else's machine.
    """
    sentences = ["What is in the notes file?", "Copy notes to a backup copy."]
    made = speech.synthesise(sentences, tmp_path)
    assert made, "piper produced no audio"

    engine = speech.Transcriber()
    try:
        for text, wav in made:
            heard = engine.transcribe(wav)
            assert "".join(c for c in heard.lower() if c.isalnum() or c == " ") \
                .split() == "".join(
                    c for c in text.lower() if c.isalnum() or c == " "
                ).split(), f"asked {text!r}, heard {heard!r}"
    finally:
        engine.unload()


@slow
def test_whisper_is_cheap_enough_to_keep_resident():
    """The number D50 rests on. If a future model pushes this up, the design
    decision should be revisited deliberately rather than by accident."""
    engine = speech.Transcriber()
    try:
        assert engine.ram_gib < 2.0, (
            f"small.en now costs {engine.ram_gib:.2f} GiB; D50 assumed ~0.3"
        )
    finally:
        engine.unload()


@slow
def test_a_broken_wav_is_reported_not_raised():
    """The caller is a voice loop. An exception there ends the loop and leaves the
    user with a microphone that has silently stopped working."""
    engine = speech.Transcriber()
    try:
        assert "failed" in engine.transcribe("no-such-file.wav")
    finally:
        engine.unload()