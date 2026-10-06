"""D76: the voice catalogue, the ops behind it, and the one that reads a path.

The catalogue module is mostly about *discovery*: a voice can arrive three ways
(downloaded, imported, or dropped in by hand) and all three have to show up. So the
tests are mostly about what is found rather than what is stored, and the assertions
that matter are the ones that would still pass if the code did nothing.

`import_voice` is the only op in this project that accepts a path the caller
supplied, so it gets the most attention: what it refuses, and the fact that
"the assistant cannot reach an op" is the real safeguard rather than a path check.

Every test here uses a temporary `models/voices` directory. The real one has three
60 MB voices in it, and a test that downloaded, imported or removed any of them would
be a test that changes the machine it runs on.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from assistant import daemon as daemon_mod
from assistant import voiceconfig as vc

MB = 1024 ** 2


@pytest.fixture
def voices_dir(tmp_path: Path) -> Path:
    """An empty voices directory, so nothing touches the real 60 MB files."""
    d = tmp_path / "voices"
    d.mkdir()
    return d


@pytest.fixture
def cfg_path(tmp_path: Path) -> Path:
    return tmp_path / "voices.json"


def _make_voice(directory: Path, name: str, *, size: int = 1000,
                with_config: bool = True) -> tuple[Path, Path | None]:
    """Write a plausible voice pair."""
    model = directory / f"{name}.onnx"
    model.write_bytes(b"x" * size)
    config = directory / f"{name}.onnx.json"
    if with_config:
        config.write_text("{}", encoding="utf-8")
        return model, config
    return model, None


# --------------------------------------------------------------------------- #
# discovery, which is the whole design
# --------------------------------------------------------------------------- #


def test_a_voice_dropped_in_by_hand_is_found(voices_dir: Path) -> None:
    """Discovery rather than a registry, for the same reason as D74.

    A manifest is a second place for the truth to live, and the one that goes stale
    is always the manifest. This is the assertion that would fail if the module
    started trusting `state/voices.json` to list what exists.
    """
    _make_voice(voices_dir, "someone-elses-voice")

    found = vc.installed_voices(voices_dir)

    assert "someone-elses-voice" in found
    assert found["someone-elses-voice"].installed
    assert found["someone-elses-voice"].source == "imported"


def test_a_lone_model_is_found_but_marked_incomplete(voices_dir: Path) -> None:
    """Half a voice is *visible*, not hidden.

    Hiding it would leave a file on disk that the window never mentions, and the user
    with no way to see why their voice does not work. `complete` is what the window
    colours the row by.
    """
    _make_voice(voices_dir, "half", with_config=False)

    found = vc.installed_voices(voices_dir)

    assert "half" in found
    assert found["half"].installed is True
    assert found["half"].complete is False
    assert "onnx.json" in found["half"].note, (
        f"the note does not say which half is missing: {found['half'].note!r}"
    )


def test_a_zero_byte_model_is_not_complete(voices_dir: Path) -> None:
    """`speech.ensure_voice` writes to a `.part` file and renames on success.

    So a zero-byte `.onnx` means something went wrong in a way that would otherwise
    look installed - and a voice that loads as silence is worse than one that errors.
    """
    _make_voice(voices_dir, "empty", size=0)

    found = vc.installed_voices(voices_dir)

    assert found["empty"].complete is False


def test_the_config_is_paired_by_suffix_not_by_stem(voices_dir: Path) -> None:
    """`foo.onnx` pairs with `foo.onnx.json`, not with `foo.json`.

    The first version used `Path.with_suffix`, which replaces `.onnx` and looks for
    `foo.json` - a file that does not exist. Every voice then read as incomplete, and
    the failure looked like a download problem rather than a naming one.
    """
    model, config = _make_voice(voices_dir, "pairing")

    found = vc.installed_voices(voices_dir)

    assert found["pairing"].complete, (
        f"the pair was not matched: model={model.name} config={config.name}"
    )
    assert found["pairing"].config_bytes > 0


def test_the_catalogue_merges_known_and_installed(voices_dir: Path) -> None:
    """Known-but-absent voices are still listed, so the window can offer to fetch.

    Dropping them would leave the download button with nothing to download.
    """
    _make_voice(voices_dir, "en_US-amy-medium")

    cat = vc.catalogue(voices_dir)

    assert cat["en_US-amy-medium"].installed is True
    assert cat["en_US-amy-medium"].source == "known+installed"
    # The other known voices are present and marked not-installed.
    others = [v for n, v in cat.items() if n != "en_US-amy-medium"]
    assert others, "the known voices disappeared from the catalogue"
    assert all(not v.installed for v in others)
    assert all(v.source == "known" for v in others)


def test_an_empty_directory_still_lists_the_known_voices(voices_dir: Path) -> None:
    cat = vc.catalogue(voices_dir)
    assert cat, "a fresh machine has no voices to offer at all"
    assert all(not v.installed for v in cat.values())


def test_a_missing_directory_lists_nothing_rather_than_raising(tmp_path: Path) -> None:
    assert vc.installed_voices(tmp_path / "nope") == {}


# --------------------------------------------------------------------------- #
# the config file degrades, like the model one
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("content", ["{ not json", "[1,2]", '"a string"'])
def test_an_unusable_config_falls_back_and_says_why(
    cfg_path: Path, voices_dir: Path, content: str
) -> None:
    cfg_path.write_text(content, encoding="utf-8")

    cfg = vc.load(cfg_path, voices_dir)

    assert cfg.provenance != "file"
    assert "unreadable" in cfg.provenance
    assert cfg.default == vc.load(directory=voices_dir).default


def test_choosing_a_voice_that_is_not_downloaded_is_allowed(
    cfg_path: Path, voices_dir: Path
) -> None:
    """Deliberately unlike the model config, and the reason matters.

    A model that is not on disk is refused because by the time it fails the user has
    a lease outstanding and a turn in flight. A voice that is not downloaded yet is
    the *normal* state on a fresh machine, and choosing it is how you say "this is
    the one I want" before spending 63 MB. Refusing would make the download button
    unreachable.
    """
    cfg = vc.VoiceConfig(default="en_US-amy-medium")
    vc.save(cfg, cfg_path)

    back = vc.load(cfg_path, voices_dir)

    assert back.default == "en_US-amy-medium"
    assert "not downloaded" in back.provenance, (
        f"the fact that it is not downloaded is not surfaced: {back.provenance!r}"
    )


def test_a_traversal_in_the_default_is_refused_on_save(
    cfg_path: Path, voices_dir: Path
) -> None:
    for attempt in ("../../evil", "sub/dir", r"C:\evil", ""):
        with pytest.raises(vc.VoiceConfigError):
            vc.save(vc.VoiceConfig(default=attempt), cfg_path)


def test_an_unknown_whisper_model_is_refused_on_save(cfg_path: Path) -> None:
    with pytest.raises(vc.VoiceConfigError, match="whisper"):
        vc.save(vc.VoiceConfig(default="en_US-amy-medium", whisper="gpt-9"), cfg_path)


def test_an_unknown_whisper_model_in_the_file_falls_back(
    cfg_path: Path, voices_dir: Path
) -> None:
    """A hand-edited typo must not make the daemon unable to answer."""
    cfg_path.write_text(json.dumps({"default": "en_US-amy-medium",
                                    "whisper": "typo"}), encoding="utf-8")

    cfg = vc.load(cfg_path, voices_dir)

    assert cfg.whisper == vc.DEFAULT_WHISPER
    assert "typo" in cfg.provenance


def test_saving_is_atomic_and_leaves_no_partial_file(
    cfg_path: Path, voices_dir: Path
) -> None:
    vc.save(vc.VoiceConfig(default="en_US-amy-medium"), cfg_path)
    vc.save(vc.VoiceConfig(default="en_US-amy-medium"), cfg_path)   # over existing

    leftovers = [p.name for p in cfg_path.parent.iterdir() if p.name.endswith(".tmp")]
    assert not leftovers, f"partial files left behind: {leftovers}"


def test_the_written_file_explains_itself(cfg_path: Path, voices_dir: Path) -> None:
    vc.save(vc.VoiceConfig(default="en_US-amy-medium"), cfg_path)

    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert "_comment" in raw
    assert "models/voices/" in raw["_comment"], (
        "the comment does not say where voices live, which is the one thing a "
        "hand-editor needs"
    )
    assert "G:" not in cfg_path.read_text(encoding="utf-8"), "an absolute path leaked"


# --------------------------------------------------------------------------- #
# import: the one op that reads a path the user chose
# --------------------------------------------------------------------------- #


def test_import_copies_both_halves(voices_dir: Path, tmp_path: Path) -> None:
    src = tmp_path / "downloads"
    src.mkdir()
    model, config = _make_voice(src, "my-voice", size=4321)

    voice = vc.import_voice(model, directory=voices_dir)

    assert voice.name == "my-voice"
    assert voice.complete
    assert voice.model_bytes == 4321
    assert (voices_dir / "my-voice.onnx").is_file()
    assert (voices_dir / "my-voice.onnx.json").is_file()


def test_import_renames_on_request(voices_dir: Path, tmp_path: Path) -> None:
    src = tmp_path / "downloads"
    src.mkdir()
    model, _ = _make_voice(src, "ugly-name-2024")

    voice = vc.import_voice(model, name="Narrator", directory=voices_dir)

    assert voice.name == "Narrator"
    assert (voices_dir / "Narrator.onnx").is_file()


def test_import_refuses_a_model_with_no_config(voices_dir: Path, tmp_path: Path) -> None:
    """The chipmunk case.

    `speech.ensure_voice` already documents it: a voice without its config loads and
    then speaks at the wrong speed - fast, almost-intelligible speech, which is far
    worse to diagnose than an error.
    """
    src = tmp_path / "downloads"
    src.mkdir()
    model, _ = _make_voice(src, "lonely", with_config=False)

    with pytest.raises(vc.VoiceConfigError, match="onnx.json"):
        vc.import_voice(model, directory=voices_dir)

    assert not list(voices_dir.iterdir()), "a refused import left files behind"


def test_import_refuses_a_traversal_in_the_destination_name(
    voices_dir: Path, tmp_path: Path
) -> None:
    src = tmp_path / "downloads"
    src.mkdir()
    model, _ = _make_voice(src, "ok")

    for attempt in ("../../evil", "sub/dir/evil", r"C:\evil", "..\\evil"):
        with pytest.raises(vc.VoiceConfigError):
            vc.import_voice(model, name=attempt, directory=voices_dir)

    assert not list(voices_dir.iterdir())


def test_import_refuses_a_file_that_is_not_a_model(
    voices_dir: Path, tmp_path: Path
) -> None:
    src = tmp_path / "downloads"
    src.mkdir()
    wav = src / "clip.wav"
    wav.write_bytes(b"RIFF")

    with pytest.raises(vc.VoiceConfigError, match="onnx"):
        vc.import_voice(wav, directory=voices_dir)


def test_import_refuses_a_missing_file(voices_dir: Path, tmp_path: Path) -> None:
    with pytest.raises(vc.VoiceConfigError, match="not a file"):
        vc.import_voice(tmp_path / "nothing.onnx", directory=voices_dir)


def test_import_refuses_a_directory(voices_dir: Path, tmp_path: Path) -> None:
    """`is_file()` rather than `exists()`. A directory named `x.onnx` is not a model."""
    d = tmp_path / "x.onnx"
    d.mkdir()
    with pytest.raises(vc.VoiceConfigError, match="not a file"):
        vc.import_voice(d, directory=voices_dir)


def test_import_does_not_leave_a_truncated_voice_on_failure(
    voices_dir: Path, tmp_path: Path
) -> None:
    """The copy goes to a `.part` name and renames, like `ensure_voice`.

    Simulated by making the *config* unreadable after the model check passes, so the
    second copy fails. Nothing may be left behind: a half-imported voice looks
    installed and speaks wrongly.
    """
    src = tmp_path / "downloads"
    src.mkdir()
    model, config = _make_voice(src, "flaky")
    # Make the config a directory, so copying it raises.
    config.unlink()
    config.mkdir()

    with pytest.raises(Exception):
        vc.import_voice(model, directory=voices_dir)

    leftovers = [p.name for p in voices_dir.iterdir()]
    assert not leftovers, f"a failed import left {leftovers}"


# --------------------------------------------------------------------------- #
# removal
# --------------------------------------------------------------------------- #


def test_remove_deletes_both_halves(voices_dir: Path) -> None:
    _make_voice(voices_dir, "disposable")

    removed = vc.remove_voice("disposable", directory=voices_dir)

    assert sorted(removed) == ["disposable.onnx", "disposable.onnx.json"]
    assert not list(voices_dir.iterdir())


def test_remove_refuses_the_builtin_fallback(voices_dir: Path) -> None:
    """Removing the voice the assistant speaks with leaves it mute.

    The failure would appear on the next turn rather than at the moment of removal,
    which is the worst time to find out.
    """
    from assistant import speech

    _make_voice(voices_dir, speech.DEFAULT_VOICE)

    with pytest.raises(vc.VoiceConfigError, match="fallback"):
        vc.remove_voice(speech.DEFAULT_VOICE, directory=voices_dir)

    assert (voices_dir / f"{speech.DEFAULT_VOICE}.onnx").is_file(), (
        "the refusal still deleted the file"
    )


def test_remove_refuses_something_that_is_not_installed(voices_dir: Path) -> None:
    with pytest.raises(vc.VoiceConfigError, match="not installed"):
        vc.remove_voice("never-heard-of-it", directory=voices_dir)


# --------------------------------------------------------------------------- #
# the ops
# --------------------------------------------------------------------------- #


@pytest.fixture
def desk(tmp_path: Path, voices_dir: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(vc, "VOICES_DIR", voices_dir)
    monkeypatch.setattr(vc, "DEFAULT_CONFIG_PATH", tmp_path / "voices.json")
    return daemon_mod.Daemon(state=tmp_path / "state")


def _call(desk, op: str, args: dict | None = None) -> dict:
    payload = {"v": daemon_mod.ipc.PROTOCOL, "id": 1, "op": op, "args": args or {}}
    return desk.handle(daemon_mod.ipc.Request.from_dict(payload))


def test_voice_ops_are_ops_and_not_tools(desk) -> None:
    """The D21 boundary, and it matters more here than for the model ops.

    `voice_import` reads a path. If it were a tool, `brain.turn` could propose an
    arbitrary file read with no gate in front of it - D59's second door exactly.
    Reachability is the safeguard, so it has to be asserted rather than assumed.
    """
    from assistant.tools import ALL_TOOLS

    names = {spec.name for spec in ALL_TOOLS}
    for forbidden in ("voice.import", "voice.install", "voice.remove",
                      "system.set_voice"):
        assert forbidden not in names, f"{forbidden} is a registered tool"

    for op in ("voice_config", "voice_config_set", "voice_install",
               "voice_import", "voice_remove"):
        assert op in daemon_mod.Daemon._OPS, f"{op} is not wired up"
        assert op not in daemon_mod.Daemon._EXECUTORS, (
            f"{op} reached the executor table, which is the tool surface"
        )


def test_the_registry_is_still_closed_after_adding_five_ops(desk) -> None:
    from assistant.tools import ALL_TOOLS

    registered = {spec.name for spec in ALL_TOOLS}
    assert not set(desk._EXECUTORS) - registered, "an executor has no registered tool"
    assert len(desk._EXECUTORS) == len(registered)


def test_reading_the_voice_config_lists_the_installed_voices(desk, voices_dir) -> None:
    _make_voice(voices_dir, "imported-one")

    res = _call(desk, "voice_config")

    assert res["ok"], res
    result = res["result"]
    assert "imported-one" in result["voices"]
    assert result["voices"]["imported-one"]["complete"] is True
    assert result["whisper_models"], "the whisper list is empty"
    assert result["builtin_default"], "the window cannot say what the fallback is"


def test_setting_the_default_writes_and_reloads(desk, tmp_path: Path) -> None:
    res = _call(desk, "voice_config_set",
                {"default": "en_US-amy-medium", "whisper": "base.en"})

    assert res["ok"], res
    on_disk = json.loads((tmp_path / "voices.json").read_text(encoding="utf-8"))
    assert on_disk["default"] == "en_US-amy-medium"
    assert on_disk["whisper"] == "base.en"
    assert _call(desk, "voice_config")["result"]["default"] == "en_US-amy-medium"


def test_setting_a_traversal_default_is_refused(desk, tmp_path: Path) -> None:
    res = _call(desk, "voice_config_set", {"default": "../../evil"})

    assert not res["ok"]
    assert res.get("refused"), res
    assert not (tmp_path / "voices.json").exists()


def test_setting_an_unknown_whisper_model_is_refused(desk) -> None:
    res = _call(desk, "voice_config_set",
                {"default": "en_US-amy-medium", "whisper": "nope"})
    assert not res["ok"]
    assert res.get("refused"), res


def test_importing_through_the_op_audits_the_source_path(
    desk, voices_dir, tmp_path: Path
) -> None:
    """Where a voice came from is the question asked after something sounds wrong.

    An unrecorded import is an unanswerable one, which is why the source path is in
    the record rather than just the voice name.
    """
    src = tmp_path / "downloads"
    src.mkdir()
    model, _ = _make_voice(src, "audited")

    res = _call(desk, "voice_import", {"source": str(model)})
    assert res["ok"], res

    records = [json.loads(line) for line in
               (desk.state / "audit.jsonl").read_text(encoding="utf-8").splitlines()
               if line.strip()]
    hits = [r for r in records if r.get("tool") == "voice_import"]
    assert len(hits) == 1, f"expected one record, found {len(hits)}"
    assert hits[0]["args"]["voice"] == "audited"
    assert str(model) in hits[0]["args"]["source"], (
        "the audit record does not say where the voice came from"
    )
    assert hits[0]["decision"] == "allow"


def test_a_refused_import_is_not_audited_as_a_change(desk, tmp_path: Path) -> None:
    before = desk.audit.verify().checked
    res = _call(desk, "voice_import", {"source": str(tmp_path / "nothing.onnx")})
    assert not res["ok"]
    assert res.get("refused"), res
    assert desk.audit.verify().checked == before, (
        "a refusal was recorded as though something changed"
    )


def test_import_through_the_op_refuses_a_missing_source(desk) -> None:
    res = _call(desk, "voice_import", {})
    assert not res["ok"]
    assert res.get("refused")


def test_removing_through_the_op_refuses_the_fallback(desk, voices_dir) -> None:
    from assistant import speech

    res = _call(desk, "voice_remove", {"voice": speech.DEFAULT_VOICE})
    assert not res["ok"]
    assert res.get("refused"), res


def test_installing_an_unknown_voice_is_refused_without_downloading(desk) -> None:
    """Refused before any network call, so a typo costs nothing."""
    res = _call(desk, "voice_install", {"voice": "en_US-nonexistent-medium"})
    assert not res["ok"]
    assert res.get("refused"), res
    assert "known" in res["error"]


def test_installing_a_traversal_name_is_refused(desk) -> None:
    res = _call(desk, "voice_install", {"voice": "../../evil"})
    assert not res["ok"]
    assert res.get("refused")


def test_a_read_is_not_audited_but_a_write_is(desk, voices_dir) -> None:
    before = desk.audit.verify().checked
    _call(desk, "voice_config")
    _call(desk, "voice_config")
    assert desk.audit.verify().checked == before, "reads are filling the chain"

    assert _call(desk, "voice_config_set", {"default": "en_US-amy-medium"})["ok"]
    assert desk.audit.verify().checked == before + 1


# --------------------------------------------------------------------------- #
# the constraint that keeps this out of the daemon's import path
# --------------------------------------------------------------------------- #


def test_importing_this_costs_no_model_runtime() -> None:
    """D1, in a subprocess, because the daemon reads this module.

    `voiceconfig` imports `speech` lazily inside functions precisely so that the
    daemon's import stays standard-library only. An in-process check would be
    meaningless: pytest imports far more than this for other tests.
    """
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c",
         "import sys; import assistant.voiceconfig; "
         "print(','.join(m for m in ('faster_whisper','piper','onnxruntime',"
         "'llama_cpp','numpy') if m in sys.modules))"],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    leaked = [m for m in result.stdout.strip().split(",") if m]
    assert not leaked, (
        f"importing assistant.voiceconfig pulled in {leaked}; the daemon's import "
        "must stay standard-library only (D1)"
    )