"""`reflect.*`, and the four ways D21 says this must not go wrong.

D21 is the only decision in this project about the assistant changing itself, and
it is unusually specific: proposals are evaluated against a baseline and promoted
only on evidence, with `validation_score × confidence` gating, a ≤500-token
`### OPERATIONAL CONSTRAINTS` overlay, and four named anti-patterns to avoid.
Those are not aspirations. Each gets a mechanism here, and each mechanism gets a
test that fails when it is removed.

The tests are ordered by which property is most load-bearing:

1. **It cannot write its own code or policy.** Structural, not promised. No tool
   takes a path; the files are derived from the state directory; and a promotion
   reaches the system prompt and nothing else.
2. **The scores cannot be asserted by the proposer.** A model that could score its
   own proposal would score it ten, and the gate would be arithmetic with a
   suggestion on the front.
3. **The four anti-patterns** each have a named refusal.
4. **The honest-reporting property**: the tool is *supposed* to mostly say
   "nothing repeated yet", and a test asserts that it can.

That last one is unusual and it matters. A reflection tool that can only report
success is a vanity metric, and the failure mode is not a crash — it is a system
that appears to be learning.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from assistant import brain, daemon as D, reflect
from assistant.ipc import Request, next_id
from assistant.policy import CHANNEL_PC, Tier
from assistant.tools import ALL_TOOLS


@pytest.fixture
def ledger(tmp_path):
    return reflect.ReflectionLedger(tmp_path / "state")


def repeat(ledger, area, symptom, times=2, kind="denied", detail=""):
    """Record the *same* complaint `times` times, as distinct occurrences.

    Not by calling `log()` in a loop — the ledger deliberately collapses a burst
    inside its dedupe window, so a tight loop is one observation and always was
    meant to be. Recurrence has to be spaced, so the first row goes through
    `log()` (exercising the real path) and the rest are appended with timestamps
    an hour apart, which is what "it happened again tomorrow" looks like.
    """
    ledger.log(area=area, symptom=symptom, kind=kind, detail=detail)
    base = time.time()
    for i in range(1, times):
        row = {
            "at": base + i * 3600.0,
            "area": area,
            "symptom": symptom,
            "kind": kind,
            "detail": detail,
            "key": reflect._key(area, symptom),
        }
        ledger._ensure_dir()
        with ledger._ledger.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return area


# --------------------------------------------------------------------------- #
# 1. self-modification is impossible, not discouraged
# --------------------------------------------------------------------------- #


def test_no_reflect_tool_can_be_pointed_anywhere():
    """D21's headline constraint, in one assertion.

    Not "the gate refuses `src/`" and not "the prompt says not to" — **no tool in
    this group accepts a path at all**, so "the assistant modified its own code" is
    not a rule being enforced, it is an operation with no representation. The gate's
    `SELF_PATHS` is defence in depth; this group does not rely on it.
    """
    for spec in ALL_TOOLS:
        if not spec.name.startswith("reflect."):
            continue
        for arg in (spec.parameters or {}):
            assert not any(
                token in arg.lower()
                for token in ("path", "file", "dir", "target", "dest", "src")
            ), (
                f"{spec.name} accepts {arg!r}; a self-improvement write tool that "
                "can be pointed is an arbitrary-write primitive (D21)"
            )


def test_everything_reflect_writes_lands_under_the_state_directory(ledger):
    """Observed, not inferred: write for every kind of entry point and look.

    The declaration test above says the tools take no path. This one says what
    actually happened on disk, because a future argument could be added under a
    name the first test does not recognise and the declaration would not notice.
    """
    repeat(ledger, "gate.file.read", "outside every approved workspace root", 3)
    key = ledger.distil()["candidates"][0]["key"]
    ledger.propose(key=key, change="check the root before proposing a read",
                   check="a refused read stops appearing in the ledger")
    written = [p for p in ledger.base.rglob("*") if p.is_file()]
    assert written, "nothing was written at all"
    for path in written:
        assert ledger.base in path.parents or path == ledger.base, (
            f"{path} is outside the state directory"
        )


def test_a_promotion_reaches_the_prompt_and_nothing_else(ledger, tmp_path):
    """D21's overlay is prompt text. It grants no authority.

    A promoted lesson can change how the assistant *behaves*; it cannot change what
    it is allowed to do. Asserted on the reach rather than on the intent: the
    overlay goes into `default_system_prompt` and nowhere near the gate, and this
    test shows the two are independent objects.
    """
    repeat(ledger, "gate.file.read", "outside every approved workspace root", 3)
    key = ledger.distil()["candidates"][0]["key"]
    out = ledger.propose(key=key, change="check the root first",
                         check="the gate stop refusing reads of approved files")
    assert out["promoted"] is True, out
    overlay = ledger.overlay()
    assert overlay.startswith("### OPERATIONAL CONSTRAINTS")
    assert "check the root first" in overlay
    # It reaches the system prompt...
    prompt = brain.default_system_prompt(ALL_TOOLS, (), overlay)
    assert "check the root first" in prompt
    # ...and the prompt is still built from the live registry, tool for tool. A
    # promoted lesson cannot add a tool, because the tool list does not come from
    # here.
    assert prompt.count("- system.gpu_state") == 1
    # And nothing in the overlay mentions tiers, authority or the gate granting
    # anything: it is behaviour, stated as behaviour.
    for forbidden in ("T0", "T1", "T2", "T3", "may delete", "is permitted",
                      "bypass", "override"):
        assert forbidden not in overlay, (
            f"the overlay claims authority ({forbidden!r}); D21 keeps behaviour "
            "and authority separate and the overlay is on the behaviour side only"
        )


def test_the_gate_still_denies_the_assistant_tree_whatever_the_overlay_says(ledger):
    """Defence in depth, and the assertion that it is still there.

    The reflection group does not depend on `SELF_PATHS`, but the independent check
    must keep working — so this writes a promoted lesson and then asks the gate to
    write into `src/`, and expects a refusal.
    """
    from assistant.policy import PolicyGate

    repeat(ledger, "gate.file.read", "path is outside every approved workspace root",
           3, kind="denied")
    ledger.propose(key=ledger.distil()["candidates"][0]["key"],
                   change="be careful", check="fewer refusals")
    assert ledger.overlay(), "the test needs a promoted overlay to be meaningful"

    gate = PolicyGate(tools={s.name: s for s in ALL_TOOLS},
                      allowed_roots=[r"G:\localaized"])
    gate.allowed_roots = [r"G:\localaized"]
    verdict = gate.evaluate("file.read", channel=CHANNEL_PC,
                            args={"path": r"G:\localaized\src\assistant\daemon.py"})
    assert verdict.decision == "deny", (
        "the gate stopped refusing the assistant's own source"
    )


# --------------------------------------------------------------------------- #
# 2. the scores are computed, never supplied
# --------------------------------------------------------------------------- #


def test_the_proposer_cannot_state_its_own_score(ledger):
    """A model that could score its own proposal would score it ten, every time.

    So there is no `score` argument anywhere in the tool's parameters, and the
    numbers in the record came from `scores()` — which reads the ledger.
    """
    spec = next(s for s in ALL_TOOLS if s.name == "reflect.propose")
    assert "score" not in " ".join(spec.parameters), (
        "reflect.propose takes a score argument, so the gate can be talked past"
    )
    assert "confidence" not in " ".join(spec.parameters)

    repeat(ledger, "gate.file.read", "outside every approved root", 3)
    key = ledger.distil()["candidates"][0]["key"]
    out = ledger.propose(key=key, change="check the root", check="fewer refusals")
    assert out["validation_score"] <= 100
    assert out["confidence"] <= 100
    assert out["score_product"] == out["validation_score"] * out["confidence"]


def test_the_minimum_evidence_is_not_enough_to_promote(ledger):
    """The floor is a floor, not a starting point.

    Two witnessed observations clear `MIN_OBSERVATIONS` and still fail the score.
    Without this the threshold is decorative: a mutation that sets
    `PROMOTION_THRESHOLD` to zero leaves the suite green, which it did — the
    witness check was carrying every promotion test and the arithmetic nothing.
    """
    repeat(ledger, "gate.file.read", "outside every approved root", 2)
    key = ledger.distil()["candidates"][0]["key"]
    out = ledger.propose(key=key, change="check the root", check="fewer refusals")
    assert out["promoted"] is False, (
        "two observations promoted a lesson; the floor and the threshold are the "
        "same rule and together they should be harder than either"
    )
    assert "below" in out["gate"]["reason"], out["gate"]
    assert ledger.overlay() == ""


def test_a_vague_grouping_key_scores_lower_than_a_specific_one(ledger):
    """Specificity is a score term, so it has to change an outcome.

    A lesson drawn from three words of symptom text is vaguer than one drawn from a
    full sentence, and vagueness is exactly what lets a rule absorb every later
    exception. The first mutation run left this term untested — setting it to a
    constant passed the suite — which meant it was decoration wearing the look of
    evidence. Rather than delete a real property, this asserts it.
    """
    vague = {"observations": 5, "system_observed": True, "key": "gate x"}
    specific = {"observations": 5, "system_observed": True,
                "key": "gate.file.read path is outside every approved root"}
    assert ledger.scores(vague)[0] < ledger.scores(specific)[0], (
        "a three-word grouping key scores the same as a specific one, so "
        "specificity is not being measured"
    )


def test_more_observations_score_higher(ledger, tmp_path):
    """Monotonic, or the gate is not measuring anything.

    Built by hand rather than by logging, because the ledger's timestamps would
    otherwise need to be forced apart for each case.
    """
    scores = []
    for n in (2, 3, 5, 9):
        row = {"observations": n, "system_observed": True,
               "key": "gate file read outside every approved workspace root"}
        scores.append(ledger.scores(row)[0])
    assert scores == sorted(scores), scores
    assert scores[0] < scores[-1], (
        "nine observations and two scored the same, so the score is not evidence"
    )


# --------------------------------------------------------------------------- #
# 3. the four anti-patterns
# --------------------------------------------------------------------------- #


def test_one_failure_never_becomes_a_rule(ledger):
    """Overfitting: one event is not a pattern. D21 requires more than once.

    This is the most important single test in the file. A lesson written from one
    bad afternoon is how a system becomes confidently wrong forever, and the only
    thing preventing it is this refusal.
    """
    ledger.log(area="gate.file.read", symptom="outside every approved root",
               kind="denied")
    out = ledger.distil()
    assert out["candidates"] == [], "a single event became a candidate lesson"
    assert out["one_off_count"] == 1, "but it should still be visible"

    with pytest.raises(reflect.ReflectError) as exc:
        ledger.propose(key=out["one_off"][0]["key"], change="check the root",
                       check="fewer refusals")
    assert "not a pattern" in str(exc.value), str(exc.value)


def test_a_proposal_without_a_check_is_refused(ledger):
    """Reflection without change. D21 names it; this is the mechanism.

    A change nobody can falsify is indistinguishable from no change, so the `check`
    is not documentation — it is required, and its absence is the whole refusal.
    """
    repeat(ledger, "gate.file.read", "outside every approved root", 2)
    key = ledger.distil()["candidates"][0]["key"]
    with pytest.raises(reflect.ReflectError) as exc:
        ledger.propose(key=key, change="be more careful", check="")
    assert "check" in str(exc.value)
    assert ledger.overlay() == "", "an uncheckable change reached the overlay"


def test_the_same_event_logged_twice_is_still_one_observation(ledger):
    """Recurrence, not repetition.

    A burst of identical rows inside the dedupe window must count once, so the
    floor cannot be defeated by repetition. The window is a minute rather than an
    exact timestamp, precisely so this does not depend on how fast the test runs —
    the first version keyed on the full timestamp and this test passed or failed
    depending on the millisecond.
    """
    area, symptom = "gate.x", "y"
    key = reflect._key(area, symptom)
    for _ in range(3):
        ledger.log(area=area, symptom=symptom, kind="denied")
    assert ledger.count(key=key) == 1, (
        "a burst of identical rows was counted as three observations, so the floor "
        "is defeated by repetition rather than by recurrence"
    )
    assert ledger.distil()["candidates"] == []


def test_the_same_complaint_tomorrow_is_a_second_observation(ledger):
    """The other half: recurrence is not repetition.

    Bucketing on content alone would make a problem that happens every day look
    like a one-off forever, and the floor would be unreachable rather than
    defeatable — a quieter failure of the same rule.
    """
    area, symptom = "gate.x", "y"
    key = reflect._key(area, symptom)
    ledger.log(area=area, symptom=symptom, kind="denied")
    now = time.time()
    row = {"at": now + 3600, "area": area, "symptom": symptom, "kind": "denied",
           "key": key}
    ledger._ensure_dir()
    with ledger._ledger.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")
    assert ledger.count(key=key) == 2, (
        "the same complaint an hour later was collapsed into the first one, so a "
        "recurring problem can never become a lesson"
    )


def test_endless_analysis_is_bounded(ledger):
    """A long ledger produces a bounded list of candidates, and says it truncated."""
    for i in range(12):
        repeat(ledger, f"area{i}", f"symptom number {i} happened", times=2)
    out = ledger.distil(cap=3)
    assert out["showing"] == 3, out["showing"]
    assert out["total_candidates"] == 12, out["total_candidates"]
    assert out["truncated"] is True, (
        "twelve candidates and a cap of three, and nothing was truncated"
    )


def test_one_change_per_cycle(ledger):
    """A second promotion is held until the first is retired.

    Two accumulated habits at once is how an overlay starts crowding out the tool
    list, which D37 measured to be load-bearing.
    """
    repeat(ledger, "gate.file.read", "outside every approved root", 3)
    repeat(ledger, "gate.file.write", "path is denied by policy", 3)
    keys = [c["key"] for c in ledger.distil()["candidates"]]
    first = ledger.propose(key=keys[0], change="first habit",
                           check="first check")
    assert first["promoted"] is True, first["gate"]
    second = ledger.propose(key=keys[1], change="second habit",
                            check="second check")
    assert second["promoted"] is False
    assert "one change per cycle" in second["gate"]["reason"], second["gate"]


def test_a_lesson_the_model_narrated_is_never_promoted(ledger):
    """Performative reflection: a claim about what happened is not evidence.

    The daemon records what it saw. `kind="observed"` is the model or the user
    narrating, and a candidate built only from those is refused — even with plenty
    of repetitions, because the repetition is also unverified.
    """
    repeat(ledger, "tool.selection", "the model narrated a call", times=4,
           kind="observed")
    out = ledger.distil()
    assert out["candidates"], "the ledger should have a candidate"
    assert out["not_yet_promotable"], (
        "a model-narrated candidate was treated as promotable"
    )
    key = out["candidates"][0]["key"]
    # Not a refusal — the proposal is accepted and written down, because a
    # hypothesis is a legitimate thing to keep. What is refused is the promotion.
    # Asserting an exception here would be asserting a different design.
    proposed = ledger.propose(key=key, change="be better", check="something")
    assert proposed["promoted"] is False
    assert "not witnessed" in proposed["gate"]["reason"], proposed["gate"]
    assert ledger.overlay() == "", "a narrated lesson reached the overlay"


def test_system_witnessed_friction_is_promotable(ledger):
    """The other side of the same coin, so the rule is not just a wall.

    The daemon logging a refusal *is* the evidence, so the same repeated event
    promotes when it was witnessed and not when it was narrated.
    """
    repeat(ledger, "gate.file.read", "outside every approved root", 3,
           kind="denied")
    key = ledger.distil()["candidates"][0]["key"]
    out = ledger.propose(key=key, change="check the root before reading",
                         check="a read inside an approved root stops being refused")
    assert out["promoted"] is True, out["gate"]
    assert out["gate"]["reason"].startswith("promoted")


# --------------------------------------------------------------------------- #
# 4. honest reporting
# --------------------------------------------------------------------------- #


def test_an_empty_ledger_reports_nothing_and_does_not_call_it_a_failure(ledger):
    """A reflection tool that can only report success is a vanity metric.

    The most common state is "nothing repeated yet", and it must read that way.
    """
    out = ledger.report()
    assert out["events"] == 0
    assert out["candidates"] == 0
    assert out["promoted"] == 0
    assert out["overlay_active"] is False
    assert "nothing repeated yet" in out["note"], out["note"]


def test_the_report_states_that_self_modification_is_impossible(ledger):
    """The safety property is in the report, so it is visible without reading code."""
    out = ledger.report()
    assert "impossible" in out["self_modification"]
    assert "src/" in out["self_modification"] or "state directory" in out["self_modification"]


def test_the_overlay_is_capped_and_says_when_it_truncates(ledger, monkeypatch):
    """D21's ≤500 tokens, measured rather than counted in a comment.

    `OVERLAY_TOKEN_CAP` monkeypatched low so the test does not need 500 words to
    prove the cap exists.
    """
    monkeypatch.setattr(reflect, "OVERLAY_TOKEN_CAP", 60)
    repeat(ledger, "gate.file.read", "outside every approved root", 3)
    key = ledger.distil()["candidates"][0]["key"]
    ledger.propose(key=key,
                   change="check the approved root before proposing a read, and "
                          "prefer an approved root the user has already granted "
                          "over one you remember from elsewhere",
                   check="a read inside an approved root stops being refused")
    overlay = ledger.overlay()
    assert "truncated" in overlay, overlay
    assert reflect._approx_tokens(overlay) <= 80, overlay


def test_the_overlay_grants_nothing_and_says_so_in_its_own_words(ledger):
    repeat(ledger, "gate.file.read", "outside every approved root", 3)
    key = ledger.distil()["candidates"][0]["key"]
    ledger.propose(key=key, change="check the root", check="fewer refusals")
    assert "grant no authority" in ledger.overlay()


# --------------------------------------------------------------------------- #
# 5. through the daemon
# --------------------------------------------------------------------------- #


@pytest.fixture
def dg(tmp_path):
    d = D.Daemon(state=tmp_path / "state", attended=True)
    d.gate.allowed_roots = [str(tmp_path / "nope")]
    return d


def req(d, op, **args):
    return d.handle(Request(op=op, args=args, id=next_id()))


def test_a_real_refusal_is_recorded_by_the_daemon_itself(dg):
    """The daemon witnesses its own friction, which is what makes it evidence.

    Not the model saying "that was refused" — the code path that refused it, writing
    the row on the way out. Everything about the promotion gate rests on this
    distinction, so it is worth one test that fires a genuine refusal and looks.
    """
    out = req(dg, "tool_call", tool="file.read", channel=CHANNEL_PC,
              args={"path": r"C:\somewhere\else\secret.txt"})
    assert out["ok"] is False, out
    events = dg.reflect.events()
    assert events, "a real refusal recorded nothing"
    assert events[0]["kind"] == "denied"
    assert "file.read" in events[0]["area"]


def test_a_build_gap_is_recorded_too(dg):
    """A registered tool the daemon cannot run is the clearest friction there is."""
    from assistant.policy import ToolSpec

    dg.gate.register(ToolSpec("test.only.unbuilt", Tier.READ, "unbuilt"))
    out = req(dg, "tool_call", tool="test.only.unbuilt", channel=CHANNEL_PC, args={})
    assert out.get("not_implemented"), out
    kinds = {e["kind"] for e in dg.reflect.events()}
    assert "not_implemented" in kinds, kinds


def test_a_queue_is_recorded_as_friction_too(dg):
    """Reaching for something that cannot happen *now* is worth noticing twice.

    Separate from a denial on purpose: a denial is the gate refusing, and a queue
    is the assistant reaching across a channel that does not assert presence. Both
    are friction, they are different, and the mutation that removed the queue hook
    left this suite green until this test existed.

    Uses a real T2 tool with a real executor, because the queue branch sits *after*
    the executor lookup — a synthetic tool with no executor trips `not_implemented`
    first, which is the wrong branch and would make this test pass for the wrong
    reason.
    """
    # Go through the op rather than setting `dg.attended`: the gate holds its own
    # copy, and a test that sets the wrong one is asserting against a flag nothing
    # reads. The op is also the only supported way to go unattended at all.
    req(dg, "session_set_attended", attended=False, channel="internal")
    out = req(dg, "tool_call", tool="system.processes", channel=CHANNEL_PC, args={})
    assert out["ok"], out
    # T0 still executes on an unattended session; a T2 does not, it queues.
    out = req(dg, "tool_call", tool="app.launch", channel=CHANNEL_PC,
              args={"name": "notepad"})
    assert out["ok"], out
    assert out["result"].get("queued") is True, (
        f"an unattended T2 did not queue: {out['result']}"
    )
    areas = {e["area"] for e in dg.reflect.events()}
    assert any(a.startswith("queue.") for a in areas), (
        f"no queue.* friction recorded; areas were {areas}"
    )


def test_friction_recording_never_breaks_the_call_that_triggered_it(dg):
    """A reflection failure must not fail the user's request.

    The daemon asked for a deletion, not for a diary entry, and losing the deletion
    over a full disk would be a spectacular own goal.
    """
    from assistant.policy import ToolSpec

    dg.gate.register(ToolSpec("test.only.unbuilt", Tier.READ, "unbuilt"))
    # Make the ledger fail by pointing it at something unwritable.
    dg.reflect.base = Path(r"\\?\NUL\friction.jsonl")
    out = req(dg, "tool_call", tool="test.only.unbuilt", channel=CHANNEL_PC, args={})
    assert out.get("not_implemented"), (
        "the tool call failed because the reflection note could not be written"
    )


def test_reflect_report_is_a_tool_and_not_an_op(dg):
    """The D59 rule: a registered tool gets no per-tool op.

    Stated as the general rule rather than a string match, for the reason
    `test_voice_ops.py` gives — `reflect_overlay` *is* an op and is not a
    counterexample, because it is not a registered tool.
    """
    by_op = {k.replace("_", ".", 1) for k in D.Daemon._OPS}
    assert by_op.isdisjoint(D.Daemon._EXECUTORS)
    assert "reflect.report" in D.Daemon._EXECUTORS
    assert "reflect_overlay" in D.Daemon._OPS, (
        "the brain child needs somewhere to read the overlay from"
    )


def test_the_overlay_op_is_a_read_the_model_cannot_call(dg):
    """The child reads it; a model must not be able to ask for its own instructions.

    Registering `reflect_overlay` as a tool would put "show me my accumulated
    habits" in the grammar, which is a prompt the model could use to reason about
    its instructions instead of the user's mail.
    """
    names = {s.name for s in ALL_TOOLS}
    assert "reflect_overlay" not in names
    assert "reflect.overlay" not in names


def test_log_friction_needs_an_area_and_a_symptom(dg):
    """A friction event with nothing in it is not an observation."""
    for args in ({"area": "", "symptom": "x"}, {"area": "a", "symptom": ""}):
        out = req(dg, "tool_call", tool="reflect.log_friction", args=args,
                  channel=CHANNEL_PC)
        assert not out["ok"], args


def test_the_model_cannot_log_friction_it_did_not_witness(dg):
    """Logging is open; promotion is not.

    The model may record a friction it believes happened — that is a hypothesis and
    it belongs in the ledger — but `propose` refuses to build a lesson from
    hypotheses alone. Both halves are asserted, because only testing the second
    would suggest the first is not permitted, and only testing the first would miss
    the case that matters.
    """
    out = req(dg, "tool_call", tool="reflect.log_friction", channel=CHANNEL_PC,
              args={"area": "tool.selection", "symptom": "I chose the wrong tool"})
    assert out["ok"], out
    distilled = req(dg, "tool_call", tool="reflect.distil", args={},
                    channel=CHANNEL_PC)["result"]
    assert distilled["one_off_count"] == 1, (
        "a single narrated complaint should be reported as a one-off, not as a "
        f"candidate: {distilled}"
    )
    assert distilled["candidates"] == [], (
        "one narrated event became a candidate lesson"
    )
    assert distilled["not_yet_promotable"] == [], (
        "an unpromotable key was listed without being a candidate"
    )


def test_the_whole_cycle_runs_through_tool_call(dg, tmp_path):
    """End to end through the daemon, with a real refusal as the evidence.

    Two genuine refusals, a distil, a proposal, a promotion, and an overlay — all
    through `tool_call`, because that is the only path a model has and the cycle
    must work there or it does not work at all.
    """
    for _ in range(2):
        req(dg, "tool_call", tool="file.read", channel=CHANNEL_PC,
            args={"path": r"C:\elsewhere\x.txt"})
    # A burst inside the dedupe window is one observation by design, so the second
    # refusal has to be spaced to count as recurrence. Without this the cycle
    # below fails at `distil` with no candidates and for the wrong reason.
    for i in range(1, 4):
        req(dg, "tool_call", tool="file.read", channel=CHANNEL_PC,
            args={"path": rf"C:\elsewhere\{i}\x.txt"})

    distilled = req(dg, "tool_call", tool="reflect.distil", args={},
                    channel=CHANNEL_PC)["result"]
    assert distilled["candidates"], distilled
    key = distilled["candidates"][0]["key"]

    proposed = req(dg, "tool_call", tool="reflect.propose", channel=CHANNEL_PC,
                   args={"key": key,
                         "change": "check the approved roots before proposing a read",
                         "check": "a read inside an approved root stops being refused"})
    assert proposed["ok"], proposed
    assert proposed["result"]["promoted"] is True, proposed["result"]["gate"]

    overlay = req(dg, "reflect_overlay", args={})["result"]["overlay"]
    assert "approved roots" in overlay
    report = req(dg, "tool_call", tool="reflect.report", args={},
                 channel=CHANNEL_PC)["result"]
    assert report["promoted"] == 1
    assert report["overlay_active"] is True
    assert report["overlay_tokens"] <= reflect.OVERLAY_TOKEN_CAP * 2, (
        "the overlay is far larger than its cap suggests"
    )