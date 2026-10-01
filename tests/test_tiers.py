"""Two tiers, one card, and the swap between them.

The deep tier is not a second resident model. On a 12 GiB card the fast and deep
models together need more VRAM than exists, so "having two tiers" means choosing
one at a time - which makes the transition the part worth testing, because that is
where VRAM either comes back promptly or does not come back at all.

Nothing here loads a model. A test that boots a 5 GiB model to check a name is a
test that costs more than it learns, so the decision is separated from the
loading precisely so it can be checked cheaply.
"""

import pathlib
import sys
import tempfile

import pytest

from assistant import brain_server
from pathlib import Path

from assistant.brain import BrainConfig


class _NoLease:
    """Refuses every lease. Nothing in this file may take gigabytes."""

    def acquire(self, *a, **k):
        raise AssertionError("this test must not acquire a lease")

    def renew(self, *a, **k):
        raise AssertionError("this test must not renew a lease")

    def release(self, *a, **k):
        return False

    @property
    def available(self) -> int:
        return 0
from assistant import daemon as daemon_module
from assistant.daemon import Daemon, Refused
from assistant.brain_server import TIERS, resolve_tier

#: D1: our own ceiling. Every tier must ask for no more than this, or the
#: request is for more memory than the user said we may have.
CEILING = 7 * 1024 ** 3


# -- the table itself ---------------------------------------------------------


def test_both_tiers_exist():
    assert set(TIERS) == {"fast", "deep"}


def test_each_tier_requests_a_file_and_a_lease():
    for name, spec in TIERS.items():
        assert "model" in spec, name
        assert "need_bytes" in spec, name


def test_no_tier_asks_for_more_than_the_ceiling():
    """A lease larger than D1 allows would be a promise we cannot keep."""
    for name, spec in TIERS.items():
        assert spec["need_bytes"] <= CEILING, name


def test_a_lease_covers_the_file_plus_the_context():
    """Sized to the file, the lease under-requests by most of a gigabyte.

    Measured on the 4B: a 4.17 GiB file occupies 4.92 GiB, the difference being
    the CUDA context and the KV cache. Requesting the file size would let us
    exceed the ceiling while every number on the books said we were under it.
    """
    for name, spec in TIERS.items():
        size = pathlib.Path(spec["model"]).stat().st_size if \
            pathlib.Path(spec["model"]).exists() else None
        if size is None:
            continue  # not downloaded yet; the other tests still hold
        assert spec["need_bytes"] > size, name


def test_the_deep_tier_is_not_the_fast_file():
    """The two tiers must be genuinely different, not the same model twice."""
    assert TIERS["deep"]["model"] != TIERS["fast"]["model"]


def test_the_default_is_the_tier_that_is_always_present():
    """A caller that asks for nothing gets the tier whose file exists."""
    assert brain_server.DEFAULT_MODEL == TIERS["fast"]["model"]
    assert TIERS["fast"]["model"].exists()


# -- resolution ---------------------------------------------------------------


def test_a_named_tier_resolves_to_its_model_and_lease():
    model, need = resolve_tier("deep")
    assert model == str(TIERS["deep"]["model"])
    assert need == TIERS["deep"]["need_bytes"]


def test_an_explicit_model_wins_over_the_tier():
    """Useful for probing a candidate before it is in the table."""
    model, _ = resolve_tier("fast", model=r"G:\somewhere\other.gguf")
    assert model == r"G:\somewhere\other.gguf"


def test_an_unknown_tier_is_refused_rather_than_defaulted():
    """The failure this exists to prevent: answering 'deep' from the 4B.

    A silent fallback means the caller asked for the better model and got the
    worse one with no way to tell, which is worse than an error because the answer
    still looks like an answer.
    """
    with pytest.raises(ValueError) as caught:
        resolve_tier("deap")
    assert "known tiers" in str(caught.value)
    assert "deep" in str(caught.value)


# -- the swap -----------------------------------------------------------------


class _SpawnAttempted(Exception):
    """Stands in for the child actually starting.

    `_brain_ensure` ends in a `subprocess.Popen`. Swapping tests do not want that
    to happen - it would load a 6 GiB model - but they do want to reach it, so the
    spawn raises and the test inspects what happened before it.
    """


@pytest.fixture
def desk(monkeypatch):
    """A bare daemon pretending a fast-tier child is already running.

    What is under test is the *ordering* around the swap: the old child must be
    stopped before a new one is started, because the deep model only fits once the
    fast one's memory is gone. Asserting merely that both happened would pass for
    an implementation that starts first and hangs or is denied - so the sequence
    is recorded as one list and asserted in order.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        d = Daemon(state=root / "state", vault=root / "vault")
        d.log = []
        # The authkey check sits between the swap and the spawn, and refuses a
        # daemon that was never properly started. Writing one is what a real
        # `serve_forever` does, so this reproduces the production state rather
        # than bypassing a check.
        daemon_module.ipc.store_authkey(d.authkey_path, d.authkey)

        d._brain_alive = lambda: True                  # noqa: SLF001
        d._brain_client = object()                      # noqa: SLF001
        d._brain_tier = "fast"                          # noqa: SLF001

        def fake_stop(why: str) -> None:
            d.log.append(("stop", why))
            d._brain_tier = None
            d._brain_client = None

        d._brain_stop = fake_stop                       # noqa: SLF001

        class FakePopen:
            def __init__(self, *a, **k):
                d.log.append(("spawn", k.get("args", a[0] if a else None)))
                raise _SpawnAttempted()

        monkeypatch.setattr(daemon_module.subprocess, "Popen", FakePopen)
        yield d


def test_asking_for_the_same_tier_reuses_the_running_child(desk):
    """The common case must not cost a reload."""
    assert desk._brain_ensure("fast") is desk._brain_client
    assert desk.log == [], "a same-tier request disturbed the running child"


def test_asking_for_the_other_tier_stops_the_running_child(desk):
    """This is the whole point: the old model's VRAM comes back first."""
    with pytest.raises(_SpawnAttempted):
        desk._brain_ensure("deep")
    assert [step for step, _ in desk.log][0] == "stop"


def test_the_old_child_is_stopped_before_the_new_one_starts(desk):
    """Order is the claim. The reverse would ask for VRAM that is still held."""
    with pytest.raises(_SpawnAttempted):
        desk._brain_ensure("deep")
    assert [step for step, _ in desk.log] == ["stop", "spawn"]


def test_the_spawn_carries_the_tier_rather_than_a_path(desk):
    """One place names a model file, so a table cannot drift from the code."""
    with pytest.raises(_SpawnAttempted):
        desk._brain_ensure("deep")
    argv = desk.log[-1][1]
    assert "--tier" in argv
    assert argv[argv.index("--tier") + 1] == "deep"
    assert "--model" not in argv


def test_the_stop_is_named_because_a_silent_swap_is_unexplainable(desk):
    """A user watching the card empty and refill deserves to know why."""
    with pytest.raises(_SpawnAttempted):
        desk._brain_ensure("deep")
    why = desk.log[0][1]
    assert "fast" in why and "deep" in why


def test_the_daemon_refuses_an_unknown_tier_without_starting_anything(desk):
    desk._brain_stop = lambda why: desk.log.append(("stop", why))  # noqa: SLF001
    with pytest.raises(Refused) as caught:
        desk._brain_ensure("middle")
    assert "known tiers" in str(caught.value)
    assert desk.log == [], "an unknown tier disturbed a running child"


def test_status_reports_which_tier_is_held(desk):
    """'Is the deep tier loaded' must be answerable without loading anything."""
    assert desk.op_brain_status({})["tier"] == "fast"

# -- the model the tier actually loads ----------------------------------------


class _StubBrain:
    """Records the config it was handed; loads nothing."""

    def __init__(self, cfg, **kw):
        self.cfg = cfg
        self.kw = kw

    def load(self):
        return {"loaded": True, "model_path": self.cfg.model_path}

    def status(self):
        return {"loaded": True, "model_path": self.cfg.model_path}


@pytest.fixture
def stub_brain(monkeypatch):
    """Replace the real `Brain` and the real config with recording stubs.

    `_load` imports `Brain` from inside the function body, so the patch has to be
    on the module that import reads - not on `brain_server`. Both tiers' files now
    exist on disk, so a load that is really attempted would put six gigabytes in
    the test suite; this keeps the decision under test and nothing else.
    """
    import assistant.brain as brain_mod

    monkeypatch.setattr(brain_mod, "Brain", _StubBrain)
    monkeypatch.setattr(
        brain_server, "default_config",
        lambda model=None, **kw: BrainConfig(
            model_path=str(model) if model else str(brain_server.DEFAULT_MODEL),
            n_gpu_layers=0,
        ),
    )
    monkeypatch.setattr(brain_server, "_roots_reader",
                        lambda lease: (lambda: ("G:/x",)))
    yield


def _load(box_model, tier):
    box = {"stop": False, "model": box_model, "tier": tier}
    return brain_server._load(box, _NoLease())  # noqa: SLF001


def test_the_deep_tier_loads_the_deep_model(stub_brain):
    """The bug this test exists for.

    `_load` called `default_config()` with no argument. `serve` had already
    resolved the tier to the 9B path and printed it, so the child announced
    `tier=deep model=Qwen3.5-9B...` while holding the 4B. The tier comparison
    then measured the same model twice and reported identical arguments,
    identical timings and an identical 5081 MiB - which is what gave it away,
    and which nothing in the code would have.
    """
    report = _load(str(TIERS["deep"]["model"]), "deep")
    assert report["model"] == str(TIERS["deep"]["model"])
    assert report["model_path"] == str(TIERS["deep"]["model"])


def test_the_fast_tier_loads_the_fast_model(stub_brain):
    report = _load(str(TIERS["fast"]["model"]), "fast")
    assert report["model"] == str(TIERS["fast"]["model"])


def test_the_summary_names_the_tier_and_the_path_separately(stub_brain):
    """If the asked-for tier and the loaded model can ever disagree, the
    disagreement has to be in the first thing anyone reads after a load - not
    inferred three turns later from a VRAM reading.
    """
    report = _load(str(TIERS["deep"]["model"]), "deep")
    assert report["tier"] == "deep"
    assert Path(report["model"]).name.startswith("Qwen3.5-9B")


def test_a_box_with_no_model_falls_back_to_the_default(stub_brain):
    """No model on the box is a real state - a box built before the tier existed.

    It must not produce `None` as a path, and it must say which file it used.
    """
    report = _load(None, "fast")
    assert report["model"] == str(brain_server.DEFAULT_MODEL)

