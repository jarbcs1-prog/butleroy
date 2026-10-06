"""The two model ops, and the two things they must not be.

D75 added `model_config` and `model_config_set` so the settings window has
something to read and something to write. The interesting part is not that they
work - it is the two ways they are *deliberately not* the thing a tool is.

**Not a tool.** `brain.turn` returns proposals, and a proposal is executed through
`tool_call`, which goes through the gate. If model configuration were a
`system.*` tool then the assistant could propose to change which model it is - and
a system that can rewrite its own reasoning substrate is D21's exact prohibition,
stated in the project's own hard boundaries ("the assistant never rewrites its own
code or policy. Permanently, at every phase"). These are ops, reachable only over
the pipe by a process holding the authkey, which is the local user.

**Not unaudited.** "The human changed the configuration" is precisely the kind of
fact the audit chain exists to keep, and an un-audited write is a write nobody can
account for afterwards. So the write is audited at `Tier.WRITE` even though no gate
was consulted - the gate governs what the *assistant* may do, and this was not the
assistant.

Also asserted here because they are the kind of thing that breaks silently:

  * a partial table must not silently delete the tiers it omits;
  * a loaded child must not be restarted underneath a turn in flight;
  * a refused write must leave the file on disk untouched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from assistant import daemon as daemon_mod
from assistant import modelconfig as mc

GB = 1024 ** 3


@pytest.fixture
def models_dir(tmp_path: Path) -> Path:
    d = tmp_path / "models"
    d.mkdir()
    (d / "small-Q4_0.gguf").write_bytes(b"x" * 512)
    (d / "large-IQ4_XS.gguf").write_bytes(b"x" * 4096)
    return d


@pytest.fixture
def desk(tmp_path: Path, models_dir: Path, monkeypatch: pytest.MonkeyPatch):
    """A daemon wired to a temp state dir and the temp models dir.

    `modelconfig` resolves both from module constants, so both are redirected. Done
    here rather than in a fixture in `modelconfig` because the ops must be tested
    against the real daemon dispatch - a fake `op_model_config` would agree with the
    code about the dispatch, which is the thing under test.
    """
    monkeypatch.setattr(mc, "MODELS_DIR", models_dir)
    monkeypatch.setattr(mc, "DEFAULT_CONFIG_PATH", tmp_path / "models.json")
    return daemon_mod.Daemon(state=tmp_path / "state")


def _call(desk, op: str, args: dict | None = None) -> dict:
    """Through `handle`, so dispatch, channel validation and the error envelope
    are all exercised. Not a direct method call: calling `op_model_config(desk, {})`
    would skip exactly the machinery that could be wrong.

    Built with `Request.from_dict` rather than `Request(...)` so the real
    deserialisation runs - protocol version, op and args validation included. The
    first version of this helper constructed `Request(op=..., args=...)` and failed
    on a missing `id`, which says nothing about the ops and is exactly the kind of
    thing that would have been "fixed" by passing a dummy id forever.
    """
    payload = {"v": daemon_mod.ipc.PROTOCOL, "id": 1, "op": op, "args": args or {}}
    return desk.handle(daemon_mod.ipc.Request.from_dict(payload))


# --------------------------------------------------------------------------- #
# these are ops, not tools - and that is the safety property
# --------------------------------------------------------------------------- #


def test_model_config_is_not_a_tool_the_assistant_may_propose() -> None:
    """The D21 boundary, asserted structurally rather than in a comment.

    The failure this prevents is specific: someone adding `system.set_model` to the
    registry to make the window's life easier, which would put "change which model
    you are" one `brain.turn` proposal away from being executed.
    """
    from assistant.tools import ALL_TOOLS

    names = {spec.name for spec in ALL_TOOLS}
    for forbidden in ("system.set_model", "system.model_config",
                      "model.config", "model.set"):
        assert forbidden not in names, (
            f"{forbidden} is a registered tool; the assistant could then propose "
            "to change its own reasoning substrate (D21)"
        )

    assert "model_config" in daemon_mod.Daemon._OPS, "the read op is not wired up"
    assert "model_config_set" in daemon_mod.Daemon._OPS, "the write op is not wired up"
    # And neither reached the executor table, which is the tool surface.
    assert "model_config" not in daemon_mod.Daemon._EXECUTORS
    assert "model_config_set" not in daemon_mod.Daemon._EXECUTORS


def test_the_registry_and_executors_are_still_closed(desk) -> None:
    """D71's invariant must survive adding two ops.

    Worth re-asserting here rather than trusting it: this file is the first thing
    to touch the tables since they were closed, and an op that accidentally
    registered a tool would show up as a count mismatch.
    """
    from assistant.tools import ALL_TOOLS

    registered = {spec.name for spec in ALL_TOOLS}
    orphans = set(desk._EXECUTORS) - registered
    assert not orphans, f"executors with no registered tool: {sorted(orphans)}"
    assert set(desk._OPS) >= {"model_config", "model_config_set"}


# --------------------------------------------------------------------------- #
# reading
# --------------------------------------------------------------------------- #


def test_reading_with_no_config_reports_the_defaults_and_says_so(desk) -> None:
    res = _call(desk, "model_config")

    assert res["ok"], res
    result = res["result"]
    assert set(result["tiers"]) == {"fast", "deep"}
    assert result["provenance"] == "defaults"
    assert result["provenance"] != "file", (
        "provenance must never claim a file that does not exist"
    )
    assert "defaults" in result["provenance"], (
        "the user cannot tell 'your settings' from 'the built-in ones' without this"
    )


def test_reading_lists_the_models_actually_on_disk(desk, models_dir: Path) -> None:
    res = _call(desk, "model_config")
    names = [m["name"] for m in res["result"]["models"]]

    assert "small-Q4_0.gguf" in names
    assert "large-IQ4_XS.gguf" in names
    assert not any(n.startswith("mmproj") for n in names), names


def test_reading_resolves_to_absolute_paths_so_the_window_need_not_guess(
    desk, models_dir: Path
) -> None:
    """The resolved paths are the daemon's view, not a re-derivation.

    The window showing a path it computed itself is how it ends up showing a path
    the child would not open - two copies of the join, disagreeing quietly.
    """
    res = _call(desk, "model_config")
    resolved = res["result"]["resolved"]

    for name, path in resolved.items():
        assert Path(path).is_absolute(), f"{name} resolved to a relative {path}"
        assert Path(path).parent == models_dir, (
            f"{name} resolved to {Path(path).parent}, not {models_dir}"
        )


def test_reading_never_reports_an_absolute_path_in_the_tier_table(desk) -> None:
    """Portability. The stored form is a filename; only `resolved` is absolute."""
    res = _call(desk, "model_config")
    for name, tier in res["result"]["tiers"].items():
        assert "/" not in tier["model"] and "\\" not in tier["model"], (
            f"tier {name!r} stores {tier['model']!r}, which is a path, not a name"
        )


# --------------------------------------------------------------------------- #
# writing
# --------------------------------------------------------------------------- #


def test_writing_persists_and_reloads(desk, tmp_path: Path) -> None:
    res = _call(desk, "model_config_set", {"tiers": {
        "fast": {"model": "small-Q4_0.gguf", "need_bytes": 1 * GB},
        "deep": {"model": "large-IQ4_XS.gguf", "need_bytes": 7 * GB},
    }})

    assert res["ok"], res
    assert res["result"]["provenance"] == "file"

    # Through the file, not through the daemon's memory.
    on_disk = json.loads((tmp_path / "models.json").read_text(encoding="utf-8"))
    assert on_disk["tiers"]["fast"]["model"] == "small-Q4_0.gguf"
    assert on_disk["tiers"]["deep"]["need_bytes"] == 7 * GB

    # And the next read sees it.
    again = _call(desk, "model_config")
    assert again["result"]["tiers"]["fast"]["model"] == "small-Q4_0.gguf"
    assert again["result"]["provenance"] == "file"


def test_a_partial_table_is_refused_rather_than_merged(desk, tmp_path: Path) -> None:
    """Sending only `fast` must not silently delete `deep`.

    This is the whole reason the op takes a whole table. A window that sent a patch
    would delete a tier the user never looked at, and the next deep request would
    fail with "unknown tier deep" - an error about something they did not touch.
    """
    res = _call(desk, "model_config_set", {"tiers": {
        "fast": {"model": "small-Q4_0.gguf", "need_bytes": 1 * GB},
    }})

    assert not res["ok"]
    assert res.get("refused"), res
    assert "whole" in res["error"] or "non-empty" in res["error"], res["error"]
    assert not (tmp_path / "models.json").exists(), "a refused write left a file"


def test_an_empty_table_is_refused(desk) -> None:
    for payload in ({}, {"tiers": {}}, {"tiers": []}, {"tiers": "nope"}):
        res = _call(desk, "model_config_set", payload)
        assert not res["ok"], f"{payload!r} was accepted"
        assert res.get("refused"), f"{payload!r} failed rather than being refused"


def test_a_model_that_is_not_on_disk_is_refused_and_nothing_is_written(
    desk, tmp_path: Path
) -> None:
    res = _call(desk, "model_config_set", {"tiers": {
        "fast": {"model": "small-Q4_0.gguf", "need_bytes": 1 * GB},
        "deep": {"model": "typo-in-the-name.gguf", "need_bytes": 7 * GB},
    }})

    assert not res["ok"]
    assert res.get("refused"), res
    assert "typo-in-the-name.gguf" in res["error"], (
        f"the refusal does not name the offending model: {res['error']!r}"
    )
    assert not (tmp_path / "models.json").exists()


def test_a_refused_write_leaves_a_working_config_untouched(desk, tmp_path: Path) -> None:
    """Mistype one field, lose nothing.

    The sequence that must be safe: save a good table, then try to save a bad one,
    and find the good one still there. A window that validates after writing would
    take the working configuration down with the typo.
    """
    good = {"tiers": {
        "fast": {"model": "small-Q4_0.gguf", "need_bytes": 1 * GB},
        "deep": {"model": "large-IQ4_XS.gguf", "need_bytes": 7 * GB},
    }}
    assert _call(desk, "model_config_set", good)["ok"]

    bad = {"tiers": {
        "fast": {"model": "small-Q4_0.gguf", "need_bytes": 1 * GB},
        "deep": {"model": "gone.gguf", "need_bytes": 7 * GB},
    }}
    assert not _call(desk, "model_config_set", bad)["ok"]

    still = _call(desk, "model_config")
    assert still["result"]["tiers"]["fast"]["model"] == "small-Q4_0.gguf"
    assert still["result"]["tiers"]["deep"]["model"] == "large-IQ4_XS.gguf"


def test_a_traversal_in_a_tier_name_is_refused(desk) -> None:
    res = _call(desk, "model_config_set", {"tiers": {
        "fast": {"model": "../../Windows/System32/drivers/etc/hosts.gguf",
                 "need_bytes": GB},
    }})

    assert not res["ok"]
    assert res.get("refused"), res


def test_a_write_does_not_restart_a_running_child(desk) -> None:
    """Nothing is killed underneath a turn in flight.

    Restarting would return the VRAM and reload several gigabytes, which is a much
    bigger surprise than "my change takes effect on the next turn" - and the whole
    point of D22's tier swap being lazy is that it does not cost anything when the
    user did not ask for it. The result says so, so the window can say so.
    """
    res = _call(desk, "model_config_set", {"tiers": {
        "fast": {"model": "small-Q4_0.gguf", "need_bytes": 1 * GB},
        "deep": {"model": "large-IQ4_XS.gguf", "need_bytes": 7 * GB},
    }})

    assert res["ok"], res
    assert res["result"]["brain_running"] is False, (
        "brain_running should be False here - no child was ever started - and the "
        "op reporting True would mean it started one"
    )
    assert "until the daemon next starts a child" in res["result"]["note"], (
        f"the user is not told when the change takes effect: {res['result']['note']!r}"
    )


# --------------------------------------------------------------------------- #
# the write is audited
# --------------------------------------------------------------------------- #


def test_a_write_is_recorded_in_the_audit_chain(desk) -> None:
    """A change to the configuration leaves a trace.

    No gate was consulted - a human reconfiguring the product is not an assistant
    action - and that is exactly why the audit record matters more here rather than
    less. "Who changed the model, and when" is the first question anyone asks after
    something odd happens.
    """
    before = desk.audit.verify().checked

    assert _call(desk, "model_config_set", {"tiers": {
        "fast": {"model": "small-Q4_0.gguf", "need_bytes": 1 * GB},
        "deep": {"model": "large-IQ4_XS.gguf", "need_bytes": 7 * GB},
    }})["ok"]

    records = [json.loads(line) for line in
               (desk.state / "audit.jsonl").read_text(encoding="utf-8").splitlines()
               if line.strip()]
    hits = [r for r in records if r.get("tool") == "model_config.set"]

    assert len(hits) == 1, f"expected one record, found {len(hits)}"
    record = hits[0]
    assert record["decision"] == "allow"
    assert record["channel"], "the record does not say who did it"
    assert record["args"]["tiers"]["fast"] == "small-Q4_0.gguf"
    assert desk.audit.verify().checked == before + 1


def test_a_read_is_not_audited(desk) -> None:
    """Reading configuration is not an event.

    If it were, the audit chain would fill with one record every time the window was
    opened, and the records that matter - a write, a refusal, a destructive action -
    would be harder to find. The asymmetry is deliberate.
    """
    before = desk.audit.verify().checked
    _call(desk, "model_config")
    _call(desk, "model_config")
    assert desk.audit.verify().checked == before


def test_a_refused_write_is_not_recorded_as_a_change(desk) -> None:
    """Refusals go to the caller, not into the chain as a change.

    Recording a refusal as an allow would be worse than not recording it. Nothing
    changed, so the chain must show nothing changed.
    """
    before = desk.audit.verify().checked
    assert not _call(desk, "model_config_set", {"tiers": {
        "fast": {"model": "nope.gguf", "need_bytes": GB},
    }})["ok"]
    assert desk.audit.verify().checked == before


# --------------------------------------------------------------------------- #
# a corrupt config, seen from the op
# --------------------------------------------------------------------------- #


def test_a_corrupt_config_is_reported_not_hidden(desk, tmp_path: Path) -> None:
    """The window must be able to tell the user their settings were ignored.

    The end-to-end version of `test_model_config.py`'s degradation test: the file is
    unreadable, the daemon still answers, and the answer says *why* — so the window
    can put a sentence in front of the user rather than showing defaults as though
    they were chosen.
    """
    (tmp_path / "models.json").write_text("{ this is not json", encoding="utf-8")

    res = _call(desk, "model_config")

    assert res["ok"], "a corrupt config stopped the op answering"
    assert set(res["result"]["tiers"]) == {"fast", "deep"}
    assert "unreadable" in res["result"]["provenance"]
    assert res["result"]["provenance"] != "file"


def test_a_corrupt_config_does_not_stop_the_windows_from_overwriting_it(
    desk, tmp_path: Path
) -> None:
    """A bad file must be recoverable *through the UI*, not only by hand-editing.

    Otherwise the one thing the user cannot do with the settings window is fix the
    settings window's own broken state.
    """
    (tmp_path / "models.json").write_text("{{{", encoding="utf-8")

    res = _call(desk, "model_config_set", {"tiers": {
        "fast": {"model": "small-Q4_0.gguf", "need_bytes": 1 * GB},
        "deep": {"model": "large-IQ4_XS.gguf", "need_bytes": 7 * GB},
    }})
    assert res["ok"], res

    assert _call(desk, "model_config")["result"]["provenance"] == "file"


# --------------------------------------------------------------------------- #
# what the child will now use
# --------------------------------------------------------------------------- #


def test_the_child_resolves_its_tier_from_the_file(desk, monkeypatch) -> None:
    """The end-to-end claim: writing a model makes the *child* use it.

    Asserted against `brain_server.resolve_tier`, which is the function the child
    actually calls - not against `modelconfig` again, which would be the D70 shape
    (a fake agreeing with the code about the thing it does not check). This is the
    one test that would fail if the child kept reading its own literals.
    """
    from assistant import brain_server

    assert _call(desk, "model_config_set", {"tiers": {
        "fast": {"model": "small-Q4_0.gguf", "need_bytes": 1 * GB},
        "deep": {"model": "large-IQ4_XS.gguf", "need_bytes": 7 * GB},
    }})["ok"]

    path, need = brain_server.resolve_tier("fast")
    assert Path(path).name == "small-Q4_0.gguf", (
        f"the child would still load {Path(path).name} after the config was written"
    )
    assert need == 1 * GB

    path, need = brain_server.resolve_tier("deep")
    assert Path(path).name == "large-IQ4_XS.gguf"
    assert need == 7 * GB


def test_an_explicit_model_still_wins_over_the_file(desk) -> None:
    """`--model` on the command line is still an override.

    Not an accident: the child is started with a tier name, but a developer running
    one tier by hand from the console passes a path, and that must keep working.
    """
    from assistant import brain_server

    _call(desk, "model_config_set", {"tiers": {
        "fast": {"model": "small-Q4_0.gguf", "need_bytes": 1 * GB},
    }})

    path, _ = brain_server.resolve_tier("fast", model="some/explicit/model.gguf")
    assert path == "some/explicit/model.gguf", (
        f"an explicit --model was overridden by the config, giving {path!r}. The "
        "command line is the override; a file is configuration."
    )


def test_an_unknown_tier_is_still_refused_after_the_change(desk) -> None:
    """The error message must still be able to name the tiers.

    `TIERS` remains the list of *known names* even when the file supplies the
    values, so this message stays true. If someone "fixed" it to read the names from
    the file too, a typo in a tier name would report a list that might be empty.
    """
    from assistant import brain_server

    _call(desk, "model_config_set", {"tiers": {
        "fast": {"model": "small-Q4_0.gguf", "need_bytes": 1 * GB},
    }})

    with pytest.raises(ValueError) as caught:
        brain_server.resolve_tier("fats")     # typo
    assert "fast" in str(caught.value), (
        "the refusal no longer names the known tiers, so a typo is unfixable "
        f"from the message: {caught.value}"
    )