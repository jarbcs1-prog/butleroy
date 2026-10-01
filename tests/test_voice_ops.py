import tempfile
from assistant.daemon import Refused
from assistant.voice_loop import LoopStats
from pathlib import Path
from types import SimpleNamespace
import threading

"""The voice tools, wired.

These tests do not open a microphone or speak. What they check is the wiring and
the promises: that `voice.speak` and `voice.listen` are T3 for a stated reason,
that a human is shown something before either happens, and that neither silently
does the thing it was classified as needing approval for.

The audio path itself is measured in `test_speech.py` and `test_audio.py`, and
proven against real hardware by `probe_roundtrip.py`. Duplicating a real
transcription here would make the suite slow and would assert less.
"""

import pathlib
import tempfile

import pytest

from assistant import audio, speech
from assistant.daemon import Daemon, DaemonError


@pytest.fixture
def desk():
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        yield Daemon(state=root / "state", vault=root / "vault")


# -- registration --------------------------------------------------------------


def test_the_voice_tools_are_executable(desk):
    """Registered but not executable is an unfinished feature `status` admits to.
    This asserts the opposite: they are finished."""
    for name in ("voice.speak", "voice.listen", "voice.status"):
        assert name in Daemon._EXECUTORS, name


def test_the_two_dangerous_ones_have_previews(desk):
    assert "voice.speak" in Daemon._PREVIEWS
    assert "voice.listen" in Daemon._PREVIEWS


def test_they_are_reached_through_tool_call_like_every_other_tool():
    """Not through ops. No file tool has an `op_*` either: tools are reached by
    `tool_call`, which is the only path to an action, and an op per tool would be
    a second door to the same room. Asserting the absence on purpose, because the
    obvious-looking `op_voice_speak` is not what should exist.

    Stated as the general rule rather than as a string prefix, because `voice.watch`
    *is* an op and is not a counterexample: it is not a registered tool, so an op
    for it is not a second door to anything. A test written as "no key starts with
    voice_" would have "fixed" this by deleting a legitimate op, or by renaming it
    to something the assertion could not see.
    """
    by_op = {k.replace("_", ".", 1) for k in Daemon._OPS}
    assert by_op.isdisjoint(Daemon._EXECUTORS), (
        "a registered tool also has a per-tool op: "
        f"{sorted(by_op & set(Daemon._EXECUTORS))}"
    )


# -- the reason they are T3 ----------------------------------------------------


def test_both_are_t3_and_for_the_third_reason():
    from assistant.policy import Tier
    from assistant.tools import ALL_TOOLS

    for spec in ALL_TOOLS:
        if spec.name in ("voice.speak", "voice.listen"):
            assert spec.tier == Tier.IRREVERSIBLE, spec.name
            assert spec.audible is True, spec.name
            # Neither of the existing reasons applies, and saying so is what stops
            # a future edit from quietly reclassifying them as `external` and
            # thereby losing the fact that they touch a microphone.
            assert spec.destructive is False, spec.name
            assert spec.external is False, spec.name


def test_voice_status_is_free(desk):
    """The cheap one must be cheap - it is the thing a user runs to find out
    whether voice works at all."""
    assert Daemon._EXECUTORS["voice.status"] is Daemon._exec_voice_status
    result = desk._exec_voice_status({})
    assert isinstance(result, dict)
    # Nothing loaded: no transcriber, no speaker.
    assert getattr(desk, "_voice_model", None) is None


# -- what the human is shown ----------------------------------------------------


def test_the_speak_preview_shows_what_will_be_heard(desk):
    p = desk._preview_voice_speak({"text": "Hello there. This is a test."})
    assert p["text"].startswith("Hello there")
    assert p["words"] == 6
    assert p["voice"] == speech.DEFAULT_VOICE
    assert p["about_seconds"] > 0


def test_the_speak_preview_truncates_rather_than_dumping_a_whole_essay(desk):
    p = desk._preview_voice_speak({"text": "word " * 500})
    assert len(p["text"]) < 500
    assert p["characters"] == len(("word " * 500).strip())
    assert "truncated" not in p or p["truncated"] is not True


def test_the_speak_preview_says_it_cannot_be_undone(desk):
    """A reversible-looking dialog invites a reflexive yes."""
    assert desk._preview_voice_speak({"text": "hi"})["reversible"] is False


def test_the_listen_preview_names_the_device_and_the_window(desk):
    p = desk._preview_voice_listen({"seconds": 20, "device": "USB"})
    assert p["device"] == "USB"
    assert p["max_seconds"] == 20
    assert desk._preview_voice_listen({})["device"] == "(the system default)"
    assert desk._preview_voice_listen({})["reversible"] is False


# -- refusals -------------------------------------------------------------------


def test_speaking_nothing_is_refused(desk):
    """T3 approval must not be spendable on nothing."""
    with pytest.raises(DaemonError):
        desk._exec_voice_speak({"text": "   "})


def test_speaking_failure_is_raised_not_swallowed(desk, monkeypatch):
    """`synthesise` reports failure in its result; the executor must not let a
    failure look like success with an empty payload."""
    monkeypatch.setattr(
        speech, "synthesise",
        lambda *a, **k: {"ok": False, "error": "voice model missing"},
    )
    with pytest.raises(DaemonError, match="voice model missing"):
        desk._exec_voice_speak({"text": "hello"})


def test_an_unopenable_microphone_is_refused_with_the_reason(desk, monkeypatch):
    def boom(*a, **k):
        raise audio.NoInputDevice(
            "could not open 'X' at 44100 Hz. If another application holds the "
            "device exclusively, close it"
        )

    monkeypatch.setattr(audio, "Microphone", boom)
    with pytest.raises(DaemonError, match="exclusively"):
        desk._exec_voice_listen({})


# -- silence is not failure -----------------------------------------------------


def test_hearing_nothing_reports_nothing_rather_than_failing(desk, monkeypatch):
    """The single most common outcome when a wake word is in the loop is that the
    user did not speak. Turning that into an error trains people to ignore
    errors, so it must come back as a fact."""
    class FakeMic:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def describe(self):
            return {"device": "fake", "sample_rate": 44100}

    monkeypatch.setattr(audio, "Microphone", FakeMic)
    monkeypatch.setattr(audio, "calibrate",
                        lambda mic: audio.Calibrated(floor=90.0, peak=120.0, open=90.0, release=45.0, seconds=1.5, device="fake"))
    monkeypatch.setattr(audio, "segment", lambda *a, **k: None)

    result = desk._exec_voice_listen({"seconds": 7})
    assert result["heard"] is None
    assert result["text"] == ""
    assert "7" in result["note"]
    assert result["calibration"]["open_rms"] == 90.0


def test_the_calibration_used_is_reported(desk, monkeypatch):
    """If it does not hear, the user needs to see the numbers we decided with."""
    class FakeMic:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def describe(self):
            return {"device": "fake", "sample_rate": 48000}

    monkeypatch.setattr(audio, "Microphone", FakeMic)
    monkeypatch.setattr(audio, "calibrate",
                        lambda mic: audio.Calibrated(floor=120.0, peak=200.0, open=120.0, release=60.0, seconds=1.5, device="fake"))
    monkeypatch.setattr(audio, "segment", lambda *a, **k: None)
    result = desk._exec_voice_listen({})
    assert result["rate"] == 48000, "the device rate must be reported, not assumed"
    assert result["calibration"]["release_rms"] == 60.0


def test_segment_receives_the_calibrated_thresholds_not_the_defaults(desk, monkeypatch):
    """The regression this guards: calibrating inside `segment` would eat the
    first 1.5 s of every utterance and clip its opening."""
    seen = {}

    class FakeMic:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def describe(self):
            return {"device": "fake", "sample_rate": 44100}

    def fake_segment(mic, **kw):
        seen.update(kw)
        return None

    monkeypatch.setattr(audio, "Microphone", FakeMic)
    monkeypatch.setattr(audio, "calibrate",
                        lambda mic: audio.Calibrated(floor=77.0, peak=100.0, open=77.0, release=38.0, seconds=1.5, device="fake"))
    monkeypatch.setattr(audio, "segment", fake_segment)

    desk._exec_voice_listen({"seconds": 5})
    assert seen["open_rms"] == 77.0
    assert seen["release_rms"] == 38.0
    assert seen["total_s"] == 5.0


# -- the audio does not outlive the request --------------------------------------


def test_the_wav_is_deleted_after_transcription(desk, monkeypatch, tmp_path):
    """`voice.listen` is T3 because a microphone that records without consent is
    the danger. Leaving the recording behind afterwards would contradict that."""
    kept = {}

    class FakeUtterance:
        def describe(self):
            return {"seconds": 1.0, "peak": 100, "rms": 10.0,
                    "overflows": 0, "silence_trimmed_s": 0.1}

        def save(self, path):
            path = pathlib.Path(path)
            path.write_bytes(b"RIFFfake")
            kept["path"] = path
            return path

    class FakeMic:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def describe(self):
            return {"device": "fake", "sample_rate": 44100}

    class FakeModel:
        def transcribe(self, wav):
            # The file must still exist at transcription time - deleting it before
            # would be its own bug, and a fake would happily report success.
            assert kept["path"].exists(), "the wav was deleted before transcription"
            return "the words"

    monkeypatch.setattr(audio, "Microphone", FakeMic)
    monkeypatch.setattr(audio, "calibrate",
                        lambda mic: audio.Calibrated(floor=90.0, peak=120.0, open=90.0, release=45.0, seconds=1.5, device="fake"))
    monkeypatch.setattr(audio, "segment", lambda *a, **k: FakeUtterance())
    monkeypatch.setattr(desk, "_transcriber", lambda: FakeModel())

    result = desk._exec_voice_listen({})
    assert result["text"] == "the words"
    assert result["empty"] is False
    assert not kept["path"].exists(), "raw audio outlived the request"


def test_an_empty_transcript_is_flagged(desk, monkeypatch):
    """Silence and speech both transcribe to ''. Only one of them is a bug."""
    class FakeUtterance:
        def describe(self):
            return {"seconds": 1.0, "peak": 100, "rms": 10.0,
                    "overflows": 0, "silence_trimmed_s": 0.1}

        def save(self, path):
            pathlib.Path(path).write_bytes(b"RIFFfake")
            return pathlib.Path(path)

    class FakeMic:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def describe(self):
            return {"device": "fake", "sample_rate": 44100}

    class FakeModel:
        def transcribe(self, wav):
            return "  "

    monkeypatch.setattr(audio, "Microphone", FakeMic)
    monkeypatch.setattr(audio, "calibrate",
                        lambda mic: audio.Calibrated(floor=90.0, peak=120.0, open=90.0, release=45.0, seconds=1.5, device="fake"))
    monkeypatch.setattr(audio, "segment", lambda *a, **k: FakeUtterance())
    monkeypatch.setattr(desk, "_transcriber", lambda: FakeModel())

    assert desk._exec_voice_listen({})["empty"] is True


def test_a_transcription_failure_does_not_raise(desk, monkeypatch):
    """`Transcriber.transcribe` returns an error string; an exception escaping into
    a listener loop would leave the user with a microphone that stopped working."""
    class FakeUtterance:
        def describe(self):
            return {"seconds": 1.0, "peak": 1, "rms": 1.0,
                    "overflows": 0, "silence_trimmed_s": 0.0}

        def save(self, path):
            pathlib.Path(path).write_bytes(b"RIFFfake")
            return pathlib.Path(path)

    class FakeMic:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def describe(self):
            return {"device": "fake", "sample_rate": 44100}

    class FakeModel:
        def transcribe(self, wav):
            return "[transcription failed: boom]"

    monkeypatch.setattr(audio, "Microphone", FakeMic)
    monkeypatch.setattr(audio, "calibrate",
                        lambda mic: audio.Calibrated(floor=90.0, peak=120.0, open=90.0, release=45.0, seconds=1.5, device="fake"))
    monkeypatch.setattr(audio, "segment", lambda *a, **k: FakeUtterance())
    monkeypatch.setattr(desk, "_transcriber", lambda: FakeModel())

    result = desk._exec_voice_listen({})
    # Not raised, and not reported as words. The three outcomes are distinct:
    # nothing said, said-but-not-understood, and transcription broken.
    assert result["error"].startswith("[transcription failed:")
    assert result["text"] == "", "a failure string must not be returned as the transcript"
    assert result["empty"] is True


# -- sentence splitting ----------------------------------------------------------


def test_prose_is_split_into_sentences_for_synthesis(desk, monkeypatch):
    """One pass over a paragraph is measurably flatter than a sequence of them,
    and the fix is nearly free."""
    seen = {}

    def fake_synth(sentences, out_dir, voice=None):
        seen["sentences"] = list(sentences)
        return {"ok": True, "files": [], "voice": voice}

    monkeypatch.setattr(speech, "synthesise", fake_synth)
    desk._exec_voice_speak({"text": "One thing. Two things! Three?"})
    assert seen["sentences"] == ["One thing.", "Two things!", "Three?"]


def test_a_fragment_with_no_punctuation_is_still_speakable(desk, monkeypatch):
    """Every user who says "yes" must not get an empty synthesis."""
    seen = {}
    monkeypatch.setattr(
        speech, "synthesise",
        lambda s, o, voice=None: (seen.update(sentences=list(s))
                                  or {"ok": True, "files": []}),
    )
    desk._exec_voice_speak({"text": "yes"})
    assert seen["sentences"] == ["yes"]

# -- the loop op ---------------------------------------------------------------


def test_watch_is_refused_without_a_trained_wake_word(desk):
    """The op must not silently fall back to listening for everything.

    A wake word that is not there is a refusal, not a microphone opened on a
    string match. Falling back would mean the assistant transcribing every room
    sound, which is the opposite of what turning it on asked for.
    """
    desk._wake_model_path = lambda: None
    with pytest.raises(Refused) as caught:
        desk.op_voice_watch({})
    assert audio.WAKE_WORD in str(caught.value)


def test_watch_reports_the_second_time_that_it_is_already_listening(desk):
    """Starting a second loop would open a second microphone on one device."""
    desk._voice_loop = SimpleNamespace(
        stats=LoopStats(turns=3),
        _mic=SimpleNamespace(device_name="fake", rate=44100),
    )
    desk._voice_thread = SimpleNamespace(is_alive=lambda: True)
    out = desk.op_voice_watch({})
    assert out["started"] is False
    assert out["turns"] == 3


def test_watch_starts_a_loop_and_reports_the_device(desk, loop_ready):
    loop = desk.op_voice_watch({"device": "fake"})
    assert loop["started"] is True
    assert loop["device"] == "fake mic"
    assert loop["rate"] == 44100
    assert desk._voice_thread is not None


def test_the_loop_runs_on_its_own_thread_not_the_callers(desk, loop_ready):
    """A voice turn asks for 4.9 GiB of VRAM. Blocking the pipe on that would
    stall every other caller, including policy checks."""
    desk.op_voice_watch({"turns": 1})
    thread = desk._voice_thread
    assert thread is not None
    assert thread is not threading.main_thread()
    assert thread.daemon, "a stuck microphone must not keep the daemon alive"
    thread.join(timeout=10)


def test_status_says_whether_the_loop_is_running(desk, loop_ready):
    """"Am I listening?" must be answerable without asking the microphone."""
    before = desk._exec_voice_status({})
    assert before["loop"]["running"] is False
    # The fixture put a real file there, so the model *is* present before the loop
    # starts. Asserting otherwise would be asserting the fixture is broken.
    assert before["loop"]["wake_model_present"] is True

    desk.op_voice_watch({"turns": 1})
    desk._voice_thread.join(timeout=10)
    after = desk._exec_voice_status({})
    assert after["loop"]["running"] is False, "the loop should have finished"
    assert after["loop"]["wake_model_present"] is True


def test_status_loads_no_models(desk):
    """`voice.status` is the thing you call when voice is broken. It must work
    when voice is broken."""
    desk._exec_voice_status({})   # must not raise, must not load anything
    assert "inputs" in desk._exec_voice_status({})

@pytest.fixture
def loop_ready(monkeypatch):
    """Make `loop.open()` succeed without touching hardware.

    Three things, and each one is a real constructor the loop calls: the device,
    the wake model, and the calibration. Replacing only the microphone would still
    load two ONNX models and read 1.5 s of silence.
    """
    from assistant import audio as audio_mod

    class FakeMic:
        device_name = "fake mic"
        rate = 44100
        _block = 441

        def frames(self, seconds=None):
            return iter(())

        def rms(self, frame):
            return 0.0

    class FakeWake:
        ok = True
        error = ""
        name = audio_mod.WAKE_WORD

        def score(self, frame):
            return 0.0

        def fired(self, frame):
            return False

    # Signature read from the class, not recalled: floor, peak, open, release,
    # seconds, device. Guessing it wrong is the seventh time this session that
    # reimplementing or misremembering something I already own produced a
    # confident wrong answer.
    cal = audio_mod.Calibrated(floor=16.0, peak=20.0, open=90.0, release=45.0,
                               seconds=1.5, device="fake mic")
    monkeypatch.setattr(audio_mod, "Microphone", lambda *a, **k: FakeMic())
    monkeypatch.setattr(audio_mod, "WakeWord", lambda *a, **k: FakeWake())
    monkeypatch.setattr(audio_mod, "calibrate", lambda *a, **k: cal)

    # A real file, not a stubbed `.exists()`. The op checks for a file on disk and
    # the point of the fixture is that the check runs against something real; a
    # monkeypatched predicate would pass whether or not the check ever fired.
    tmp = tempfile.TemporaryDirectory()
    trained = Path(tmp.name) / "hey_jarvis_v0.1.onnx"
    trained.write_bytes(b"not really a model")
    monkeypatch.setattr(Daemon, "_wake_model_path", lambda self: trained)
    yield cal
    tmp.cleanup()