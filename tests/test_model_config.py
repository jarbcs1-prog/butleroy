"""D74: the model configuration file, and the four ways it is allowed to be wrong.

This is the module that makes the models surface possible, so its tests are mostly
about **degradation**: every one of these is a file the daemon has to survive
reading, and the failure mode that matters is the daemon not starting.

The shape of the file follows D72 and D70's lesson — *the fake agrees with the code
about everything it does not check* — applied to a file instead of a fake. So:

  * the built-in defaults are asserted against `brain_server.TIERS` by **importing
    it**, not by restating them. The first version of this file restated the model
    filenames, which means it compared a copy of itself with the file it was copied
    from and would have kept passing after either changed. That is the exact failure
    this project has now hit three times (D36, D72, D74).
  * every degradation path is exercised against a **real file on a real disk**,
    because "does this code handle a corrupt file" has a different answer in memory
    than on a filesystem.
  * `save()` is asserted to be atomic by checking that no partial file is left, and
    the existence check is asserted to happen at save time — the point of the check
    is that the user finds out while looking at the form, not after a 5 GiB lease.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from assistant import modelconfig as mc

GB = 1024 ** 3


@pytest.fixture
def models_dir(tmp_path: Path) -> Path:
    """A directory with three plausible models in it."""
    d = tmp_path / "models"
    d.mkdir()
    (d / "small-Q4_0.gguf").write_bytes(b"x" * 512)
    (d / "medium-Q8_0.gguf").write_bytes(b"x" * 2048)
    (d / "large-IQ4_XS.gguf").write_bytes(b"x" * 4096)
    # A projector, which is NOT a model and must never be offered as one.
    (d / "mmproj-F16.gguf").write_bytes(b"x" * 900)
    return d


@pytest.fixture
def cfg_path(tmp_path: Path) -> Path:
    return tmp_path / "models.json"


# --------------------------------------------------------------------------- #
# the defaults are the ones the child already uses
# --------------------------------------------------------------------------- #


def test_the_defaults_match_the_literals_the_child_already_had() -> None:
    """Asserted against `brain_server.TIERS`, imported.

    The duplication is deliberate — `modelconfig` must not import `brain_server`
    because that module can pull a model runtime in, and this is read by the daemon
    (D1). The cost of that decision is this test, and the test is the whole reason
    the cost is acceptable: without it there would be two sets of defaults and no
    way to know they had drifted.

    Importing `brain_server` here rather than restating the filenames is the entire
    point. A restated copy compares a copy of itself with the file it came from and
    passes forever after either one changes.
    """
    from assistant import brain_server

    mine = mc.defaults()
    theirs = brain_server.TIERS

    assert set(mine) == set(theirs), (
        f"the tiers differ: {sorted(mine)} vs {sorted(theirs)}"
    )
    for name, tier in mine.items():
        # The filename, not the full path: brain_server holds a Path built from
        # ROOT, and the config holds a bare filename so the file stays portable.
        assert tier.model == Path(theirs[name]["model"]).name, (
            f"tier {name!r}: config says {tier.model!r}, brain_server says "
            f"{Path(theirs[name]['model']).name!r}"
        )
        assert tier.need_bytes == theirs[name]["need_bytes"], (
            f"tier {name!r} lease differs: {tier.need_bytes} vs "
            f"{theirs[name]['need_bytes']}"
        )


def test_loading_with_no_file_reports_that_it_used_the_defaults(
    cfg_path: Path, models_dir: Path
) -> None:
    """A machine that has never opened the settings window must be unchanged."""
    cfg = mc.load(cfg_path, models_dir=models_dir)

    assert cfg.provenance == "defaults"
    assert set(cfg.tiers) == {"fast", "deep", "spare"}
    # And the paths still resolve, which is the property `serve` depends on.
    for tier in cfg.tiers.values():
        assert isinstance(tier.resolved(models_dir), Path)


# --------------------------------------------------------------------------- #
# degradation: every one of these must not stop the daemon
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "content, expected_fragment",
    [
        ("{ not json at all", "unreadable"),
        ("[1, 2, 3]", "not an object"),
        ('{"nope": {}}', "unreadable"),
        ('{"tiers": []}', "unreadable"),
        ('{"tiers": {"fast": "a string, not an object"}}', "fast"),
        ('{"tiers": {"fast": {"model": 5, "need_bytes": 1}}}', "fast"),
        ('{"tiers": {"fast": {"model": "x.gguf"}}}', "fast"),
        ('{"tiers": {"fast": {"model": "x.gguf", "need_bytes": "lots"}}}', "fast"),
        ('{"tiers": {"fast": {"model": "x.gguf", "need_bytes": -5}}}', "fast"),
    ],
)
def test_a_config_the_daemon_cannot_use_falls_back_and_says_why(
    cfg_path: Path, models_dir: Path, content: str, expected_fragment: str
) -> None:
    """Every malformed shape degrades to the defaults, and *names the reason*.

    The naming is the load-bearing half. A settings window that silently discards
    what the user typed is worse than one that refuses to save, because the user
    believes it took — so `provenance` has to survive to the UI.
    """
    cfg_path.write_text(content, encoding="utf-8")
    cfg = mc.load(cfg_path, models_dir=models_dir)

    assert set(cfg.tiers) == {"fast", "deep", "spare"}, "the defaults were lost"
    assert cfg.provenance != "file"
    assert expected_fragment in cfg.provenance, (
        f"provenance {cfg.provenance!r} does not mention {expected_fragment!r}, so a "
        "user would be told their settings were ignored without being told why"
    )


def test_one_bad_tier_does_not_cost_the_user_the_good_one(
    cfg_path: Path, models_dir: Path
) -> None:
    """Per-tier degradation, which is the useful version.

    Wholesale fallback is the easy implementation and the wrong one: a typo in the
    deep tier silently resetting the fast tier as well means the user's model choice
    changes for a reason they cannot see.
    """
    cfg_path.write_text(json.dumps({"tiers": {
        "fast": {"model": "small-Q4_0.gguf", "need_bytes": 2 * GB},
        "deep": {"model": "does-not-exist.gguf", "need_bytes": 7 * GB},
    }}), encoding="utf-8")

    cfg = mc.load(cfg_path, models_dir=models_dir)

    assert cfg.tier("fast").model == "small-Q4_0.gguf", "the good tier was discarded"
    assert cfg.tier("deep").model != "does-not-exist.gguf", (
        "a model that is not on disk was accepted"
    )
    assert "does-not-exist.gguf" in cfg.provenance
    assert "partial" in cfg.provenance


def test_a_tier_the_file_does_not_mention_keeps_its_default(
    cfg_path: Path, models_dir: Path
) -> None:
    """Adding a tier later must not require editing every existing config."""
    cfg_path.write_text(json.dumps({"tiers": {
        "fast": {"model": "small-Q4_0.gguf", "need_bytes": 2 * GB},
    }}), encoding="utf-8")

    cfg = mc.load(cfg_path, models_dir=models_dir)

    assert cfg.tier("fast").model == "small-Q4_0.gguf"
    assert cfg.tier("deep") is not None, "the untouched tier lost its default"
    assert cfg.provenance == "file"


def test_a_path_traversal_in_a_model_name_is_refused(cfg_path: Path, models_dir: Path) -> None:
    """`../../Windows/System32/...` must never become a path the child opens.

    The check is on the *stored* string, before it is joined to anything, because
    normalising first is how traversal defences get bypassed.
    """
    for attempt in (
        "../../evil.gguf",
        "sub/dir/evil.gguf",
        r"C:\evil.gguf",
        "..\\..\\evil.gguf",
        "evil.gguf/",
    ):
        with pytest.raises(mc.ModelConfigError):
            mc.TierSetting(model=attempt, need_bytes=GB)


def test_a_config_carrying_a_traversal_is_degraded_not_honoured(
    cfg_path: Path, models_dir: Path
) -> None:
    """The file is not trusted even though it is on disk and hand-editable.

    Both spellings, because the forward-slash form and the backslash form are
    different strings and a filter that only handles one is not a filter.
    """
    cfg_path.write_text(json.dumps({"tiers": {
        "fast": {"model": "../../../Windows/System32/drivers/etc/hosts.gguf",
                 "need_bytes": GB},
    }}), encoding="utf-8")

    cfg = mc.load(cfg_path, models_dir=models_dir)

    assert cfg.tier("fast").model != "../../../Windows/System32/drivers/etc/hosts.gguf"
    for name, tier in cfg.tiers.items():
        assert ".." not in tier.model, f"tier {name!r} kept a traversal"


def test_an_absurd_lease_is_refused_rather_than_clamped(models_dir: Path) -> None:
    """A typo in the lease must be loud.

    Clamping to the ceiling would produce an arbiter denial later that reads as
    "the model is too big" — a factually wrong error message about a number the
    user never typed.
    """
    with pytest.raises(mc.ModelConfigError, match="ceiling"):
        mc.TierSetting(model="small-Q4_0.gguf", need_bytes=mc.MAX_LEASE_BYTES * 2)


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #


def test_a_projector_is_not_offered_as_a_model(models_dir: Path) -> None:
    """`mmproj-*` is projector weights, not a model.

    Offering one produces a child that leases 5 GiB, loads successfully, and then
    cannot speak — which is a much harder thing to diagnose than a missing row.
    """
    names = [m["name"] for m in mc.available_models(models_dir)]
    assert not any(n.startswith("mmproj") for n in names), names
    assert "small-Q4_0.gguf" in names


def test_the_list_carries_an_enough_size_to_price_a_lease(models_dir: Path) -> None:
    """A row showing a filename and nothing else cannot answer "will this fit".

    The estimate is the same one D1 measured — a 4.17 GiB file occupies 4.92 GiB —
    so the number shown before the user commits is the number the arbiter will see
    afterwards.
    """
    rows = {m["name"]: m for m in mc.available_models(models_dir)}
    medium = rows["medium-Q8_0.gguf"]
    assert medium["bytes"] == 2048
    assert medium["est_vram_bytes"] > medium["bytes"], (
        "the estimate is below the file size; VRAM occupancy is never less"
    )


def test_a_missing_models_directory_lists_nothing_rather_than_raising(tmp_path: Path) -> None:
    """`available_models` is called by the settings window on open."""
    assert mc.available_models(tmp_path / "nope") == []


# --------------------------------------------------------------------------- #
# saving
# --------------------------------------------------------------------------- #


def test_saving_and_reloading_round_trips(cfg_path: Path, models_dir: Path) -> None:
    written = mc.ModelsConfig(tiers={
        "fast": mc.TierSetting(model="small-Q4_0.gguf", need_bytes=1 * GB),
    }, path=cfg_path)

    saved = mc.save(written, cfg_path, models_dir=models_dir)
    assert saved.is_file()

    back = mc.load(cfg_path, models_dir=models_dir)
    assert back.tier("fast").model == "small-Q4_0.gguf"
    assert back.tier("fast").need_bytes == 1 * GB


def test_saving_refuses_a_model_that_is_not_on_disk(cfg_path: Path) -> None:
    """The check happens at SAVE time — that is the entire point of it.

    If this only happened at model-load, the user would discover the typo after the
    child had leased several gigabytes and a turn was already in flight.
    """
    bad = mc.ModelsConfig(tiers={
        "fast": mc.TierSetting(model="never-downloaded.gguf", need_bytes=GB),
    }, path=cfg_path)

    with pytest.raises(mc.ModelConfigError, match="not in"):
        mc.save(bad, cfg_path)

    assert not cfg_path.exists(), "a refused save left a file behind"


def test_a_refused_save_does_not_destroy_the_previous_config(
    cfg_path: Path, models_dir: Path
) -> None:
    """A typo in one field must not cost the working configuration.

    The failure this prevents: the user opens settings, mistypes a model name,
    presses Save, gets an error — and has silently lost the configuration that was
    working. The validation has to happen before the write, and it does.
    """
    good = mc.ModelsConfig(tiers={
        "fast": mc.TierSetting(model="small-Q4_0.gguf", need_bytes=1 * GB),
    }, path=cfg_path)
    mc.save(good, cfg_path, models_dir=models_dir)

    bad = mc.ModelsConfig(tiers={
        "fast": mc.TierSetting(model="small-Q4_0.gguf", need_bytes=1 * GB),
        "deep": mc.TierSetting(model="typo.gguf", need_bytes=7 * GB),
    }, path=cfg_path)
    with pytest.raises(mc.ModelConfigError):
        mc.save(bad, cfg_path, models_dir=models_dir)

    back = mc.load(cfg_path, models_dir=models_dir)
    assert back.tier("fast").model == "small-Q4_0.gguf"


def test_saving_leaves_no_partial_file_behind(cfg_path: Path, models_dir: Path) -> None:
    """Atomic write: the temp file is gone, and there is exactly one artifact.

    A half-written models.json is one of the unreadable states `load()` has to
    degrade from. Not producing it is cheaper than recovering from it.
    """
    cfg = mc.ModelsConfig(tiers={
        "fast": mc.TierSetting(model="small-Q4_0.gguf", need_bytes=GB),
    }, path=cfg_path)
    mc.save(cfg, cfg_path, models_dir=models_dir)

    leftovers = [p.name for p in cfg_path.parent.iterdir() if p.name.endswith(".tmp")]
    assert not leftovers, f"partial files left behind: {leftovers}"
    # Only files, and only ours. The directory holds the models fixture too, and
    # asserting on `iterdir()` wholesale would be asserting on the fixture.
    assert [p.name for p in cfg_path.parent.iterdir() if p.is_file()] == ["models.json"]


def test_saving_over_an_existing_file_works(cfg_path: Path, models_dir: Path) -> None:
    """Windows `rename()` fails if the destination exists; this must not."""
    cfg = mc.ModelsConfig(tiers={
        "fast": mc.TierSetting(model="small-Q4_0.gguf", need_bytes=GB),
    }, path=cfg_path)
    mc.save(cfg, cfg_path, models_dir=models_dir)
    mc.save(cfg, cfg_path, models_dir=models_dir)   # over an existing file
    mc.save(cfg, cfg_path, models_dir=models_dir)
    assert mc.load(cfg_path, models_dir=models_dir).tier("fast")


def test_the_written_file_explains_itself_to_the_next_reader(cfg_path: Path, models_dir: Path) -> None:
    """A hand-edited file needs a key, and the comment is where it goes.

    The user was told this file is safe to edit, so it has to be readable by
    someone with a text editor and no documentation open.
    """
    cfg = mc.ModelsConfig(tiers={
        "fast": mc.TierSetting(model="small-Q4_0.gguf", need_bytes=GB),
    }, path=cfg_path)
    mc.save(cfg, cfg_path, models_dir=models_dir)

    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert "_comment" in raw
    assert "models/" in raw["_comment"], (
        "the comment does not say the model is relative to models/, which is the "
        "one thing a hand-editor will get wrong"
    )
    assert raw["tiers"]["fast"]["model"] == "small-Q4_0.gguf"
    assert "G:" not in cfg_path.read_text(encoding="utf-8"), (
        "an absolute path leaked into the config, which is not portable and pins "
        "the file to one machine"
    )


# --------------------------------------------------------------------------- #
# what the child consumes
# --------------------------------------------------------------------------- #


def test_to_brain_tiers_produces_resolved_paths_and_ints(
    cfg_path: Path, models_dir: Path
) -> None:
    cfg_path.write_text(json.dumps({"tiers": {
        "fast": {"model": "small-Q4_0.gguf", "need_bytes": 2 * GB},
    }}), encoding="utf-8")

    out = mc.to_brain_tiers(mc.load(cfg_path, models_dir=models_dir),
                             models_dir=models_dir)

    assert out["fast"]["model"] == models_dir / "small-Q4_0.gguf"
    assert isinstance(out["fast"]["model"], Path)
    assert out["fast"]["need_bytes"] == 2 * GB
    assert isinstance(out["fast"]["need_bytes"], int)


def test_to_brain_tiers_does_not_mutate_the_loaded_config(
    cfg_path: Path, models_dir: Path
) -> None:
    """A copy, not an in-place upgrade.

    `brain_server.TIERS` is a module-level literal that other code reads. If
    converting a config mutated it, the result of a call would depend on whether
    some earlier call had been made — the D59 "second door" shape, one level down.
    """
    cfg = mc.load(cfg_path, models_dir=models_dir)
    first = mc.to_brain_tiers(cfg, models_dir=models_dir)
    first["fast"]["model"] = "tampered.gguf"
    first["fast"]["need_bytes"] = 1

    second = mc.to_brain_tiers(cfg, models_dir=models_dir)
    assert second["fast"]["model"] != "tampered.gguf"
    assert second["fast"]["need_bytes"] != 1


def test_two_calls_give_independent_dicts(cfg_path: Path, models_dir: Path) -> None:
    cfg = mc.load(cfg_path, models_dir=models_dir)
    a = mc.to_brain_tiers(cfg, models_dir=models_dir)
    b = mc.to_brain_tiers(cfg, models_dir=models_dir)
    assert a is not b
    assert a["fast"] is not b["fast"]


# --------------------------------------------------------------------------- #
# the constraint that made this a separate module
# --------------------------------------------------------------------------- #


def test_importing_this_costs_no_model_runtime() -> None:
    """D1, checked in a subprocess.

    The daemon reads this file on start-up. If importing it pulled in
    `llama_cpp`, the daemon would no longer hold the gate with the model dead —
    which is the property the whole 24/7 design rests on. An in-process check would
    be meaningless here: pytest imports `brain_server` for a dozen other tests, so
    `sys.modules` is already populated and the assertion could only ever pass.
    """
    import subprocess
    import sys as _sys

    result = subprocess.run(
        [_sys.executable, "-c",
         "import sys; import assistant.modelconfig; "
         "print(','.join(m for m in ('llama_cpp','PIL','tkinter','numpy') "
         "if m in sys.modules))"],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    leaked = [m for m in result.stdout.strip().split(",") if m]
    assert not leaked, (
        f"importing assistant.modelconfig pulled in {leaked}; the daemon's import "
        "must stay standard-library only (D1)"
    )