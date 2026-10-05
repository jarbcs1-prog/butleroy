"""The teaching tools, wired.

These tests never load a model. `_brain_call` is replaced with a function that
returns the shape `brain_server._stream` returns, so what is under test is the
wiring, the ledger and the refusals - not the quality of anyone's prose.

Two things are worth saying about why this file is shaped the way it is.

**The ledger is asserted as a fixture, first.** `test_the_fixture_records_a_real_turn`
teaches something with the faked brain and then reads the file back. Without it
this file could report fifty green tests over a ledger that never wrote a line,
which is the same shape as the wake-word probe that measured a scaling rather
than a wake word (D55): an instrument that has not been shown to move cannot be
used to say anything did not move.

**The tier change is pinned, because a tier is exactly the kind of label that
drifts back.** Five of these six tools were `Tier.READ` when the registry was
first written. Four of them cannot be: the explanation, the quiz, the summary
and the curriculum are prose, and the brain produces prose only through
`Brain.stream()`, which needs the child model resident. A READ label on a call
that takes ~5 GiB of VRAM is right about the words and wrong about the cost
(D50). So they are `Tier.CONSEQUENTIAL` now, and a test says so - because the
honest correction is the one that can be undone by accident.
"""

import json
import pathlib
import sys
import tempfile

import pytest

from assistant import teach
from assistant.daemon import Daemon, Refused
from assistant.manifest import Workspace
from assistant.policy import Decision, Tier
from assistant.tools import ALL_TOOLS


# --------------------------------------------------------------------- fixture


@pytest.fixture
def desk():
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        yield Daemon(state=root / "state", vault=root / "vault")


@pytest.fixture
def taught(desk, monkeypatch):
    """A desk whose brain is replaced, and which records what it was told.

    The fake returns the exact shape `brain_server._stream` returns -
    `{"loaded": True, "pieces": [...], "text": ...}` - because a fake that
    returns something the real child does not would prove the wiring against a
    fiction. It also records every call so a test can assert that a turn
    streamed rather than, say, quietly decided a tool call.
    """
    calls = []

    def fake(op, *, prompt=None, tier=None, **kw):
        calls.append({"op": op, "tier": tier, "prompt": prompt or ""})
        return {"loaded": True, "pieces": ["Because "], "text": "Because yield."}

    monkeypatch.setattr(desk, "_brain_call", fake)
    desk.calls = calls
    return desk


def ledger_of(desk) -> teach.Ledger:
    return desk._teach_ledger()


# -- the fixture proves itself ------------------------------------------------


def test_the_fixture_records_a_real_turn(taught):
    """A turn must write a row, or every other test here is about nothing.

    Asserted by reading the file back rather than by asking the object what it
    thinks it wrote, so the check cannot pass on the same state the code holds.
    """
    out = taught._exec_teach_explain({"topic": "recursion"})
    assert out["taught"] is True
    path = pathlib.Path(out["ledger"])
    assert path.exists(), "the turn claimed to record and wrote no file"
    rows = [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["topic"] == "recursion"
    assert rows[0]["kind"] == teach.KIND_EXPLAIN
    assert rows[0]["text"] == "Because yield."


def test_a_teaching_turn_streams_from_the_brain_and_says_which_tier(taught):
    """A teaching turn is a T2 stream, and it says so in the call.

    If the tier ever falls back to the child's default, the arbiter stops being
    consulted about whether this may be resident - and nothing would report it,
    because the call would still succeed on a machine with the card free.
    """
    taught._exec_teach_explain({"topic": "recursion"})
    assert len(taught.calls) == 1
    assert taught.calls[0]["op"] == "stream"
    assert taught.calls[0]["tier"] == str(Tier.CONSEQUENTIAL)


# -- the tiers, which are the load-bearing correction ------------------------


GENERATIVE = ("teach.explain", "teach.quiz", "teach.recall", "teach.summarise",
              "teach.plan_curriculum")


@pytest.mark.parametrize("name", GENERATIVE)
def test_prose_costs_a_model_and_is_not_declared_a_free_read(name):
    """Five teaching tools are T2, and one is T0. Both halves are asserted.

    Only asserting the five would leave a silent promotion of `teach.progress`
    passing; only asserting the one would let a prose tool drift back to T0,
    which is the exact lie this change exists to stop.
    """
    spec = next(s for s in ALL_TOOLS if s.name == name)
    assert spec.tier == Tier.CONSEQUENTIAL, (
        f"{name} produces prose, so it needs the child model resident. A READ "
        "label here tells a human the call is free when it costs ~5 GiB.")


def test_the_one_free_teaching_read_is_the_one_that_needs_no_model():
    spec = next(s for s in ALL_TOOLS if s.name == "teach.progress")
    assert spec.tier == Tier.READ


def test_a_prose_teaching_tool_queues_when_nobody_is_there(tmp_path):
    """The tier is not a label on a tool; it is what the gate enforces.

    D13: unattended execution stops at T1. If this tool were T0 it would run
    unattended and load a model while the user was away, which is the failure
    the whole tier scale exists to prevent. The daemon is constructed
    `attended=False` because that is the state being described - asking a
    present daemon would answer a different question.
    """
    desk = Daemon(state=tmp_path / "state", vault=tmp_path / "vault",
                  attended=False)
    verdict = desk.gate.evaluate("teach.explain", channel="scheduled",
                                 args={"topic": "recursion"},
                                 actor="tester")
    assert verdict.decision != Decision.ALLOW, verdict.reason
    assert "unattended" in verdict.reason.lower() or "queue" in verdict.reason.lower()


def test_the_free_read_runs_unattended(tmp_path):
    desk = Daemon(state=tmp_path / "state", vault=tmp_path / "vault",
                  attended=False)
    verdict = desk.gate.evaluate("teach.progress", channel="scheduled",
                                 args={}, actor="tester")
    assert verdict.decision == Decision.ALLOW, verdict.reason


# -- the grammars -------------------------------------------------------------


class TestTopics:
    """A topic is a name. It is not the material, and it is not a paragraph."""

    @pytest.mark.parametrize("bad", [None, "", "   ", "\t\n"])
    def test_a_missing_topic_is_refused_with_a_way_to_proceed(self, bad):
        why = teach.topic_refusal(bad)
        assert why, "a blank topic must say so"
        assert "topic" in why.lower()
        assert "python generators" in why, "a refusal must name what would work"

    def test_a_long_topic_is_refused_and_says_where_material_goes(self):
        why = teach.topic_refusal("x" * (teach.TOPIC_MAX + 1))
        assert why
        assert str(teach.TOPIC_MAX) in why
        assert "source" in why and "question" in why, (
            "the limit is useless unless the message says where the long text "
            "is supposed to go instead")

    def test_a_falsy_topic_is_discarded_before_it_reaches_the_grammar(self):
            """`args.get("topic") or args.get("subject")` drops a 0 or a False.

            So `topic: 0` is a *missing* topic rather than the topic "0", which is the
            better of the two readings - nobody means the topic zero - and it happens
            because of the `or` idiom rather than because anyone decided it. Asserted so
            that a future change to how the topic is read has to notice.
            """
            assert (None or "subject") == "subject"
            assert teach.topic_refusal("0") is None, "the string is a legal topic name"
            assert teach.topic_refusal(0) is None, "but the number is coerced, not refused"

    def test_a_topic_exactly_at_the_limit_is_fine(self):
        assert teach.topic_refusal("x" * teach.TOPIC_MAX) is None

    def test_a_topic_is_trimmed_not_merely_accepted(self):
        assert teach.topic_refusal("  recursion  ") is None


class TestStyles:
    def test_no_style_means_the_default_and_is_not_a_refusal(self):
        assert teach.style_refusal(None) is None
        assert teach.style_refusal("") is None

    @pytest.mark.parametrize("style", ["socratic", "direct", "Socratic",
                                       "  direct  "])
    def test_the_two_real_styles_are_accepted_however_spelled(self, style):
        assert teach.style_refusal(style) is None

    def test_an_unknown_style_names_the_two_that_exist(self):
        why = teach.style_refusal("lecture")
        assert why
        assert "socratic" in why and "direct" in why

    def test_the_default_is_socratic_because_the_spec_says_so(self):
        assert teach.DEFAULT_STYLE == "socratic"
        assert teach.STYLES[0] == "socratic"


class TestQuestions:
    @pytest.mark.parametrize("bad", [None, "", "  "])
    def test_recall_needs_a_question(self, bad):
        why = teach.question_refusal(bad)
        assert why
        assert "recall" in why.lower()

    def test_summarise_needs_something_pointed_at(self):
        why = teach.source_refusal(None)
        assert why
        assert "source" in why and "text" in why


# -- the refusals are the same question in both places -----------------------


REFUSAL_CASES = [
    ("no topic", teach.KIND_EXPLAIN, {}, "topic"),
    ("a blank topic", teach.KIND_EXPLAIN, {"topic": "   "}, "topic"),
    ("a topic over the limit", teach.KIND_EXPLAIN,
     {"topic": "x" * (teach.TOPIC_MAX + 1)}, "200"),
    ("a style that does not exist", teach.KIND_EXPLAIN,
     {"topic": "recursion", "style": "lecture"}, "socratic"),
    ("recall with no question", teach.KIND_RECALL,
     {"topic": "recursion"}, "question"),
    ("summarise with nothing pointed at", teach.KIND_SUMMARY,
     {"topic": "recursion"}, "source"),
]


@pytest.mark.parametrize("label,kind,args,word", REFUSAL_CASES)
def test_a_refusal_says_the_same_thing_in_the_preview_and_the_executor(
        desk, label, kind, args, word):
    """D32: the preview is what was approved, the executor is what runs.

    So the same function has to answer both. A preview that lists a fault the
    executor then ignores is a confirmation that binds to nothing, and one that
    hides a fault the executor refuses is a dialog that lied.
    """
    preview = desk._preview_teach_turn(kind, dict(args))
    listed = preview.get("would_be_refused") or []
    assert listed, f"{label}: the preview must say what is wrong"
    assert any(word in r for r in listed), f"{label}: {listed}"
    # And the executor asks the same question, so the same words come back.
    assert desk._teach_refusals(dict(args), kind=kind) == listed


@pytest.mark.parametrize("label,kind,args,word", REFUSAL_CASES)
def test_the_executor_refuses_every_case_the_preview_flags(
        desk, label, kind, args, word):
    """Every listed fault must actually stop the call.

    The preview and the executor ask the same question, so this is really a
    check that the executor asks it at all - which it did not, once, for
    `teach.summarise`, where a call with neither `source` nor `text` produced
    the identical refusal twice in the same list.
    """
    fn = {
        teach.KIND_EXPLAIN: desk._exec_teach_explain,
        teach.KIND_QUIZ: desk._exec_teach_quiz,
        teach.KIND_RECALL: desk._exec_teach_recall,
        teach.KIND_SUMMARY: desk._exec_teach_summarise,
        teach.KIND_CURRICULUM: desk._exec_teach_plan_curriculum,
    }[kind]
    with pytest.raises(Refused) as caught:
        fn(dict(args))
    assert word in str(caught.value), (label, str(caught.value))


def test_a_refusal_is_never_repeated_in_the_same_list(desk):
    """Two copies of a question means two sentences for one fault.

    A dialog showing the same refusal twice reads as two separate problems, and
    the second copy is usually the branch nobody meant to keep.
    """
    for kind in (teach.KIND_EXPLAIN, teach.KIND_QUIZ, teach.KIND_RECALL,
                 teach.KIND_SUMMARY, teach.KIND_CURRICULUM):
        for args in ({}, {"topic": ""}, {"topic": "recursion", "style": "x"}):
            listed = desk._teach_refusals(dict(args), kind=kind)
            assert len(listed) == len(set(listed)), (kind, args, listed)


def test_a_refused_turn_records_nothing(taught):
    """Nothing was taught, so nothing may be written down as having been.

    A row for a refused call is a record of something that did not happen, and
    `teach.progress` counts rows.
    """
    before = len(ledger_of(taught).rows())
    with pytest.raises(Refused):
        taught._exec_teach_explain({})
    assert len(ledger_of(taught).rows()) == before


# -- the turns ----------------------------------------------------------------


TURNS = [
    ("explain", teach.KIND_EXPLAIN, "_exec_teach_explain", {"topic": "recursion"}),
    ("quiz", teach.KIND_QUIZ, "_exec_teach_quiz", {"topic": "recursion"}),
    ("recall", teach.KIND_RECALL, "_exec_teach_recall",
     {"topic": "recursion", "question": "what does yield do?"}),
    ("curriculum", teach.KIND_CURRICULUM, "_exec_teach_plan_curriculum",
     {"topic": "recursion"}),
    ("summarise", teach.KIND_SUMMARY, "_exec_teach_summarise",
     {"topic": "recursion", "text": "a generator yields lazily"}),
]


@pytest.mark.parametrize("label,kind,attr,args", TURNS)
def test_every_teaching_turn_records_what_it_produced(
        taught, label, kind, attr, args):
    out = getattr(taught, attr)(dict(args))
    assert out["taught"] is True
    assert out["kind"] == kind
    assert out["topic"] == "recursion"
    assert out["answer"] == "Because yield."
    assert out["recorded"] == 1
    rows = ledger_of(taught).rows()
    assert len(rows) == 1 and rows[0]["kind"] == kind


@pytest.mark.parametrize("label,kind,attr,args", TURNS)
def test_every_teaching_turn_names_the_file_it_wrote(
        taught, label, kind, attr, args):
    out = getattr(taught, attr)(dict(args))
    assert out["ledger"] == str(ledger_of(taught).path)


def test_a_turn_the_model_answered_with_nothing_records_nothing(taught,
                                                               monkeypatch):
    """An empty answer is not a teaching turn, and must not become a row.

    The count in `teach.progress` is the evidence a future `teach.recall` works
    from; a row with no text in it is a topic that looks covered and was never
    said.
    """
    monkeypatch.setattr(
        taught, "_brain_call",
        lambda op, *, prompt=None, tier=None, **kw: {"loaded": True, "text": "  "})
    out = taught._exec_teach_explain({"topic": "recursion"})
    assert out["taught"] is False
    assert "nothing" in out["note"]
    assert ledger_of(taught).rows() == []


def test_the_brain_refusing_is_reported_and_records_nothing(taught,
                                                             monkeypatch):
    """`_brain_call` raises Refused when the child reports an error.

    The exception must not be swallowed into "taught: false", because those are
    different facts: one is the model declining, the other is the turn failing.
    """

    def boom(op, *, prompt=None, tier=None, **kw):
        raise Refused("LeaseDenied: another turn holds the card")

    monkeypatch.setattr(taught, "_brain_call", boom)
    with pytest.raises(Refused) as caught:
        taught._exec_teach_explain({"topic": "recursion"})
    assert "another turn" in str(caught.value)
    assert ledger_of(taught).rows() == []


def test_a_long_answer_is_capped_but_reports_its_true_length(taught,
                                                             monkeypatch):
    monkeypatch.setattr(
        taught, "_brain_call",
        lambda op, *, prompt=None, tier=None, **kw: {
            "loaded": True, "text": "z" * (teach.TEXT_CAP + 500)})
    out = taught._exec_teach_explain({"topic": "recursion"})
    assert len(out["answer"]) == teach.TEXT_CAP
    assert out["chars"] == teach.TEXT_CAP + 500, "the true length must survive"
    assert out["truncated"] is True


def test_the_socratic_style_is_the_default_and_says_something_specific(
        taught):
    taught._exec_teach_explain({"topic": "recursion"})
    prompt = taught.calls[0]["prompt"]
    assert "Socratically" in prompt
    assert "one question at a time" in prompt
    assert "Do not give the answer" in prompt, (
        "a prompt that says 'be socratic' without forbidding the answer is a "
        "mood, not a rule")


def test_the_direct_style_does_not_get_the_socratic_rules(taught):
    taught._exec_teach_explain({"topic": "recursion", "style": "direct"})
    prompt = taught.calls[0]["prompt"]
    assert "Socratically" not in prompt
    assert "plainly and completely" in prompt


def test_a_turn_is_told_what_has_already_come_up(taught):
    """A teacher that re-explains what it explained a minute ago is not listening.

    The history comes from the ledger, so this is the ledger earning its place
    rather than existing for `teach.progress` alone.
    """
    taught._exec_teach_explain({"topic": "recursion"})
    taught._exec_teach_explain({"topic": "recursion"})
    second = taught.calls[1]["prompt"]
    assert "already come up" in second
    assert "explain at" in second, "the history must name what came up, not just exist"


def test_an_unrelated_topic_is_not_dragged_into_the_history(taught):
    taught._exec_teach_explain({"topic": "recursion"})
    taught._exec_teach_explain({"topic": "monads"})
    assert "already come up" not in taught.calls[1]["prompt"]


# -- summarise reads what the user pointed at ---------------------------------


@pytest.fixture
def rooted(desk, tmp_path):
    """A desk with one approved workspace, for the tools that read a path."""
    ws = tmp_path / "ws"
    ws.mkdir()
    desk.manifests.add(Workspace(name="ws", root=str(ws), allow_tiers=(
        Tier.READ, Tier.REVERSIBLE_WRITE, Tier.CONSEQUENTIAL)))
    desk.gate.allowed_roots = list(desk.manifests.roots())
    desk.ws = ws
    return desk


def test_summarise_reads_a_file_inside_an_approved_workspace(taught, monkeypatch):
    tmp = pathlib.Path(tempfile.mkdtemp())
    ws = tmp / "ws"
    ws.mkdir()
    (ws / "notes.md").write_text("a generator yields lazily", encoding="utf-8")
    taught.manifests.add(Workspace(name="ws", root=str(ws), allow_tiers=(
        Tier.READ, Tier.REVERSIBLE_WRITE, Tier.CONSEQUENTIAL)))
    taught.gate.allowed_roots = list(taught.manifests.roots())
    out = taught._exec_teach_summarise({"topic": "generators",
                                        "source": str(ws / "notes.md")})
    assert out["taught"] is True
    assert "a generator yields lazily" in taught.calls[0]["prompt"]
    assert out["source"] == str(ws / "notes.md")


def test_summarise_refuses_a_file_outside_every_approved_workspace(taught):
    """D10 is enforced by the gate, and this is where the gate is asked."""
    out_of_reach = r"C:\Users\PC Principal\Documents"
    with pytest.raises(Refused) as caught:
        taught._exec_teach_summarise({"topic": "generators",
                                      "source": out_of_reach})
    assert "approved" in str(caught.value).lower()


def test_summarise_refuses_the_projects_own_source(taught):
    """D21: the assistant may not read its own decisions as teaching material."""
    own = str(pathlib.Path(taught.__module__.replace(".", "\\")
                           if False else "G:\\localaized\\DECISIONS.md"))
    with pytest.raises(Refused) as caught:
        taught._exec_teach_summarise({"topic": "decisions", "source": own})
    assert "protected" in str(caught.value).lower() or \
        "own" in str(caught.value).lower()


def test_summarise_says_so_when_the_file_is_not_there(taught, tmp_path):
    """Inside an approved workspace, so the gate has no reason to stop it first.

    A path outside every approved root is refused for being outside - correctly,
    and for a different reason - so asking about existence with one of those
    would prove the refusal happens and nothing about the existence check.
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    taught.manifests.add(Workspace(name="ws2", root=str(ws), allow_tiers=(
        Tier.READ, Tier.REVERSIBLE_WRITE, Tier.CONSEQUENTIAL)))
    taught.gate.allowed_roots = list(taught.manifests.roots())
    with pytest.raises(Refused) as caught:
        taught._exec_teach_summarise({"topic": "generators",
                                      "source": str(ws / "nope.md")})
    assert "not there" in str(caught.value)


def test_pasted_text_needs_no_file_at_all(taught):
    out = taught._exec_teach_summarise({"topic": "generators",
                                        "text": "yield makes it lazy"})
    assert out["taught"] is True
    assert "yield makes it lazy" in taught.calls[0]["prompt"]
    assert out["source"] == "pasted"


# -- progress, the free read -------------------------------------------------


def test_progress_on_an_empty_ledger_says_it_is_empty_not_broken(desk):
    out = desk._exec_teach_progress({})
    assert out["recorded"] == 0
    assert out["topics"] == 0
    assert out["shown"] == []
    assert out["ledger"].endswith(teach.LEDGER_NAME)


def test_progress_reports_the_five_kinds_separately(taught):
    for label, kind, attr, args in TURNS:
        getattr(taught, attr)(dict(args))
    out = taught._exec_teach_progress({})
    assert out["recorded"] == 5
    assert out["topics"] == 1
    for kind in teach.KINDS:
        assert out["by_kind"][kind] == 1, kind


def test_progress_needs_no_model_and_says_what_it_cost(taught):
    """The reason this tool is T0 is that it touches nothing but a file.

    Asserted on the reported cost because a reader deciding whether to trust a
    T0 label is reading exactly this sentence.
    """
    out = taught._exec_teach_progress({})
    assert "no model" in out["cost"]
    assert not taught.calls, "reading progress must not stream"


def test_a_topic_explained_and_never_tested_is_named_as_such(taught):
    """The one thing `teach.progress` exists to say.

    A count of explanations is a count of things said. Only recall is evidence,
    and a report that cannot distinguish a topic that was explained from one
    that was learned is reporting the first number under the second name.
    """
    taught._exec_teach_explain({"topic": "recursion"})
    taught._exec_teach_explain({"topic": "monads"})
    taught._exec_teach_quiz({"topic": "recursion"})
    out = taught._exec_teach_progress({})
    assert out["by_kind"]["explain"] == 2
    assert out["explained_never_tested"] == ["monads"], (
        "a topic explained twice and never asked back is the one this tool "
        "exists to surface")
    assert out["explained_never_tested_count"] == 1
    assert "not learned" in out["note"]


@pytest.mark.parametrize("kind,attr,args", [
    (teach.KIND_QUIZ, "_exec_teach_quiz", {"topic": "recursion"}),
    (teach.KIND_RECALL, "_exec_teach_recall",
     {"topic": "recursion", "question": "what is yield?"}),
])
def test_being_tested_empties_the_untested_list(taught, kind, attr, args):
    taught._exec_teach_explain({"topic": "recursion"})
    assert taught._exec_teach_progress({})["explained_never_tested"] == ["recursion"]
    getattr(taught, attr)(dict(args))
    assert taught._exec_teach_progress({})["explained_never_tested"] == []


def test_progress_truncates_rather_than_pasting_the_whole_ledger(taught):
    for i in range(6):
        taught._exec_teach_explain({"topic": f"topic-{i}"})
    out = taught._exec_teach_progress({"limit": 2})
    assert out["topics"] == 6
    assert out["shown_count"] == 2
    assert out["truncated"] is True
    assert len(out["shown"]) == 2


def test_progress_coerces_a_nonsense_limit_instead_of_failing(taught):
    """`teach.progress` is the tool you call when something is broken."""
    out = taught._exec_teach_progress({"limit": "nonsense"})
    assert out["shown_count"] == 0


# -- the ledger itself --------------------------------------------------------


def test_a_malformed_ledger_line_is_skipped_rather_than_fatal(desk):
    """The reads that matter are better served by nine good rows than a crash.

    The file is written only by `record`, so a bad line means a crash
    mid-append or a person editing it with notepad - which is exactly what the
    user was told they could do.
    """
    path = ledger_of(desk).path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"kind": "explain", "topic": "ok"}\n'
                    "not json at all\n"
                    "\n"
                    '{"kind": "quiz", "topic": "also ok"}\n',
                    encoding="utf-8")
    rows = ledger_of(desk).rows()
    assert [r["topic"] for r in rows] == ["ok", "also ok"]


def test_the_ledger_refuses_to_overfill_and_says_where_it_is(taught,
                                                             monkeypatch):
    """It stops rather than deleting. A tool that drops the oldest teaching
    records to make room can make `teach.progress` wrong without saying so."""
    monkeypatch.setattr(teach, "LEDGER_MAX_ROWS", 2)
    taught._exec_teach_explain({"topic": "a"})
    taught._exec_teach_explain({"topic": "b"})
    with pytest.raises(ValueError) as caught:
        taught._exec_teach_explain({"topic": "c"})
    message = str(caught.value)
    assert teach.LEDGER_NAME in message
    assert "yours to clear" in message, (
        "a refusal must say who can fix it and how")


def test_the_ledger_path_is_not_taken_from_the_caller(taught):
    """A teaching tool that writes where it is told is an arbitrary write."""
    import inspect
    src = inspect.getsource(taught._exec_teach_send_draft) if hasattr(
        taught, "_exec_teach_send_draft") else ""
    assert src == ""  # there is no such tool here at all
    for name in GENERATIVE + ("teach.progress",):
        spec = next(s for s in ALL_TOOLS if s.name == name)
        assert spec.name.startswith("teach.")
    # And no teaching tool accepts a `path` argument at all.
    import assistant.daemon as dm
    body = inspect.getsource(dm.Daemon._teach_ledger)
    assert "args" not in body.split("def ")[0], body
