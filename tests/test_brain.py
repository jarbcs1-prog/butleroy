"""
Tests for Plane B's brain.

The whole point of the two-pass design is that the *decision* is the fragile
step, so most of these tests are about what happens when a model does the wrong
thing rather than the right thing. A suite that only checked the happy path would
have passed the original `tool_choice="auto"` implementation, which is the thing
that was broken.

`FakeLlm` is a real object with the same call shape as `llama_cpp.Llama` for the
two methods this module uses. It is a fake rather than a mock of our own code on
purpose: mocking `Brain` would test the test, not the sequencing. What is NOT
faked is the grammar compilation - `_make` really calls `LlamaGrammar.from_string`,
because D36 says a constraint that silently does nothing is the worst failure
available and only a real compile proves it is applied.
"""

import sys
import threading
import time

import pytest

from assistant import brain as B
from assistant.grammar import build_tool_grammar
from assistant.tools import ALL_TOOLS

MODEL = "G:/localaized/models/Qwen3.5-0.8B-Q4_0.gguf"


# --------------------------------------------------------------- fakes


class FakeLlm:
    """Stands in for llama_cpp.Llama for the two calls Brain makes.

    Records the grammar it was handed, because the load-bearing claim of D34 is
    that the constraint is actually reaching the sampler. Asserting on that is
    the difference between testing the mechanism and testing that a string was
    built.
    """

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def create_chat_completion(self, *, messages, grammar=None, max_tokens=None,
                               temperature=None, stream=False, **_):
        self.calls.append({"messages": messages, "grammar": grammar,
                           "max_tokens": max_tokens, "stream": stream})
        reply = self.replies.pop(0) if self.replies else ""

        if stream:
            def gen():
                for piece in reply:
                    yield {"choices": [{"delta": {"content": piece}}]}
            return gen()

        return {"choices": [{"message": {"content": reply}}]}


class Denied:
    """What the arbiter hands back when it says no (see arbiter.Denial)."""

    def __init__(self, reason="DENIED brain: needs 4.92 GiB, only 2.00 GiB free"):
        self.denied = True
        self.reason = reason

    def __str__(self):
        return self.reason


@pytest.fixture
def cfg():
    return B.BrainConfig(model_path=MODEL, n_ctx=2048, idle_ttl_s=90.0)


def make_brain(cfg, replies, *, lease=None, **kw):
    lease = lease if lease is not None else (lambda: {"ok": True})
    return B.Brain(cfg, lease=lease,
                   llm_factory=lambda c: FakeLlm(replies), **kw)


# ------------------------------------------------- 1. the ordering that matters


def test_the_lease_is_taken_before_the_model_is_loaded(cfg):
    """D1. This ordering is the whole reason the class exists.

    Load first, ask later, and you have already taken the memory by the time you
    discover you were not allowed to. The only remedy left is to unload, which is
    slow, visible, and racy against the user's own CUDA work.
    """
    order = []

    def lease():
        order.append("lease")
        return {"ok": True}

    b = B.Brain(cfg, lease=lease, llm_factory=lambda c: (order.append("load"), FakeLlm([]))[1])
    b.load()
    assert order == ["lease", "load"], order


def test_a_denied_lease_means_the_model_is_never_loaded(cfg):
    """The denial has to be cheap. Nothing loaded means nothing taken."""
    def lease():
        raise B.LeaseDenied("no lease, no model")

    b = B.Brain(cfg, lease=lease,
                llm_factory=lambda c: pytest.fail("the factory must not be called"))
    with pytest.raises(B.LeaseDenied):
        b.load()
    assert b.status()["loaded"] is False
    assert b.status()["lease"] is False


def test_a_denial_object_is_honoured_and_not_just_a_truthy_lease(cfg):
    """arbiter.acquire returns a Denial object on refusal, not False or None.

    A `if lease:` check would pass a Denial through as a successful lease, and the
    failure would only appear later as a mysterious model that will not load.
    """
    b = B.Brain(cfg, lease=lambda: Denied(),
                llm_factory=lambda c: pytest.fail("must not load"))
    with pytest.raises(B.LeaseDenied) as exc:
        b.load()
    assert "2.00 GiB" in str(exc.value)


def test_turning_before_loading_raises_rather_than_pretending(cfg):
    b = make_brain(cfg, ["tool"])
    with pytest.raises(B.BrainNotLoaded):
        b.decide("hello")
    with pytest.raises(B.BrainNotLoaded):
        b.turn("hello")
    with pytest.raises(B.BrainNotLoaded):
        list(b.stream("hello"))


# ------------------------------------------------------- 2. the two-pass turn


def test_a_tool_decision_routes_to_the_generated_grammar(cfg):
    b = make_brain(cfg, ["tool", '{"tool": "file.list", "arguments": {"path": "C:\\\\a"}}'])
    b.load()
    action = b.turn("list the files in C:\\a")
    assert action.kind == "tool"
    assert action.tool == "file.list"
    assert action.arguments == {"path": "C:\\a"}
    # pass 1 got the two-way decision grammar, pass 2 got the tool grammar
    assert b._llm.calls[0]["grammar"] is not None
    assert b._llm.calls[1]["grammar"] is not None
    assert b._llm.calls[0]["grammar"] is not b._llm.calls[1]["grammar"]


def test_an_answer_decision_gets_no_grammar_at_all(cfg):
    """Pass 2 for prose is deliberately UNCONSTRAINED.

    A tool grammar here would force a call even when the user asked a question,
    which is the opposite failure and just as bad: an assistant that can only
    call tools cannot teach.
    """
    b = make_brain(cfg, ["answer", "A paperclip holds paper together."])
    b.load()
    action = b.turn("what is a paperclip for?")
    assert action.kind == "answer"
    assert action.tool is None
    assert "paperclip" in action.text
    assert b._llm.calls[1]["grammar"] is None, "prose must not be grammar-constrained"


def test_the_decision_grammar_admits_exactly_two_words(cfg):
    """Pass 1 admits no third option, which is the entire trick.

    Under tool_choice=auto the model narrates instead of committing. There is
    nowhere for a narration to go here.
    """
    from assistant.grammar import build_decision_grammar

    gbnf = build_decision_grammar().gbnf
    assert '"tool"' in gbnf and '"answer"' in gbnf
    # a rule that admits anything at all would reintroduce the failure
    assert ".*" not in gbnf


def test_an_unrecognised_decision_is_treated_as_an_answer(cfg):
    """Never guess 'tool' and execute nothing - that is the worse failure."""
    b = make_brain(cfg, ["hmm, let me think about that", "Here is my answer."])
    b.load()
    action = b.turn("what is 2+2?")
    assert action.kind == "answer"
    assert b._llm.calls[1]["grammar"] is None


def test_the_turn_totals_both_pass_latencies(cfg):
    """A turn's latency is both passes, not just the second.

    Asserting "> 0" would test nothing: the fake returns instantly and Windows'
    clock granularity is ~15 ms, so a fast turn legitimately measures 0.0. What
    is worth asserting is that a SLOW pass is visible in the total - a caller
    deciding whether to escalate to the deep tier needs to see where the time
    went.
    """
    class SlowLlm(FakeLlm):
        def create_chat_completion(self, **kw):
            time.sleep(0.05)
            return super().create_chat_completion(**kw)

    b = B.Brain(cfg, lease=lambda: {"ok": True},
                llm_factory=lambda c: SlowLlm(["answer", "four"]))
    b.load()
    action = b.turn("what is 2+2?")
    assert action.latency_s >= 0.1, action.latency_s
    # and it survives the wire representation, which is where rounding hides things
    assert action.to_dict()["latency_s"] >= 0.1


# --------------------------------------------- 3. the grammar is really applied


def test_the_grammars_are_really_compiled_not_strings(cfg):
    """D36. A constraint that silently does nothing is the worst failure mode.

    So the object handed to the sampler must be a real compiled grammar. A str
    would raise deep in the sampler; None would mean unconstrained generation
    and a model that goes back to narrating.
    """
    b = make_brain(cfg, ["tool", "{}"])
    b.load()
    for call in b._llm.calls:
        pass
    assert b._decision_grammar is not None
    assert b._grammar is not None
    assert not isinstance(b._decision_grammar, str)
    assert not isinstance(b._grammar, str)
    assert b._decision_grammar is not b._grammar


def test_the_grammar_covers_the_described_tools_and_names_the_gaps(cfg):
    b = make_brain(cfg, ["answer", "x"])
    b.load()
    d = b.describe()
    assert d["loaded"] is True
    assert "file.list" in d["tools"], d["tools"]
    assert d["undescribed"], "a tool nobody described must show up as a gap"
    assert not set(d["tools"]) & set(d["undescribed"])


def test_a_tool_that_is_not_in_the_grammar_is_reported_not_hidden(cfg):
    """D31. A silently-dropped tool is a tool the model can never call."""
    b = make_brain(cfg, ["answer", "x"])
    b.load()
    d = b.describe()
    undescribed = set(d["undescribed"])
    # tools.py has groups (system admin, messaging, teaching, delegation,
    # reflection) that are still undescribed; they must be visible as gaps
    for name in ("telegram.send", "delegate.assign", "teach.explain"):
        if name not in d["tools"]:
            assert name in undescribed, f"{name} vanished instead of being reported"


# ------------------------------------------------------- 4. parsing the call


@pytest.mark.parametrize(
    "content,expect",
    [
        ('{"tool": "a", "arguments": {"x": 1}}', ("a", {"x": 1}, None)),
        ('{"tool": "a"}', ("a", {}, None)),
        ('  {"tool": "a", "parameters": {"y": 2}}  ', ("a", {"y": 2}, None)),
        ('{"tool": "a", "arguments": {}}', ("a", {}, None)),
    ],
)
def test_well_formed_calls_parse(content, expect):
    assert B.parse_action(content) == expect


@pytest.mark.parametrize(
    "content",
    ["", "   ", "nope", "[1,2,3]", '{"arguments": {}}', '"just a string"'],
)
def test_malformed_calls_are_reported_not_guessed_at(content):
    tool, args, err = B.parse_action(content)
    assert tool is None and args == {} and err, content


def test_arguments_of_the_wrong_type_is_an_error_not_a_coercion():
    tool, args, err = B.parse_action('{"tool": "a", "arguments": "nope"}')
    assert tool == "a" and args == {} and err


def test_a_fenced_block_still_parses():
    tool, args, err = B.parse_action('```json\n{"tool": "a", "arguments": {}}\n```')
    assert (tool, args, err) == ("a", {}, None)


def test_an_unparseable_call_is_a_result_not_an_exception(cfg):
    """A small model failing to produce JSON is normal, not a crash.

    Raising here would make the caller treat a model that rambled as a dead
    process, which is both wrong and expensive to recover from.
    """
    b = make_brain(cfg, ["tool", "I would love to help but I cannot."])
    b.load()
    action = b.turn("list files")
    assert action.kind == "tool"
    assert action.tool is None
    assert action.parse_error and "not JSON" in action.parse_error
    assert action.to_dict()["parse_error"]


def test_turn_latency_is_reported_on_both_kinds(cfg):
    """Both kinds carry a latency, and a tool turn carries its call."""
    b = make_brain(cfg, ["hi",
                         '{"tool": "file.list", "arguments": {"path": "C:/a"}}'])
    b.load()
    d = b.act("hello", B.Decision("answer", "answer", 0.0))
    assert isinstance(d.to_dict()["latency_s"], float)
    assert d.to_dict()["kind"] == "answer"
    t = b.act("list", B.Decision("tool", "tool", 0.0))
    td = t.to_dict()
    assert td["kind"] == "tool"
    assert td["tool"] == "file.list"
    assert td["arguments"] == {"path": "C:/a"}


# ----------------------------------------------------------- 5. streaming


def test_stream_yields_the_pieces_verbatim(cfg):
    b = make_brain(cfg, ["hello world"])
    b.load()
    assert list(b.stream("hi")) == list("hello world")


def test_streaming_is_unconstrained_and_asks_for_no_grammar(cfg):
    """D34 only constrains CALLS. Constraining prose would break teaching."""
    b = make_brain(cfg, ["abc"])
    b.load()
    list(b.stream("hi"))
    assert b._llm.calls[0]["grammar"] is None
    assert b._llm.calls[0]["stream"] is True


def test_a_stream_counts_as_a_turn_and_refreshes_the_ttl(cfg):
    b = make_brain(cfg, ["abc"])
    b.load()
    b._llm.replies = ["abc"]
    before = b.turns_served
    list(b.stream("hi"))
    assert b.turns_served == before + 1
    assert b.last_used is not None


# ------------------------------------------------------------- 6. lifecycle


def test_status_reports_lease_and_load_together(cfg):
    b = make_brain(cfg, ["x"])
    assert b.status() == {
        "loaded": False, "turns_served": 0, "uptime_s": 0.0,
        "idle_s": None, "ttl_s": 90.0, "lease": False,
    }
    b.load()
    s = b.status()
    assert s["loaded"] is True and s["lease"] is True
    assert s["turns_served"] == 0


def test_the_idle_ttl_is_checked_against_the_clock_not_guessed(cfg):
    b = make_brain(cfg, ["x"])
    b.load()
    base = b.last_used
    assert b.expired(base + 89) is False
    assert b.expired(base + 91) is True


def test_an_unloaded_brain_is_never_expired(cfg):
    b = make_brain(cfg, ["x"])
    assert b.expired(time.time() + 10_000) is False


def test_load_twice_is_a_no_op_that_does_not_reload(cfg):
    """Reloading would take a second lease and a second 2.5 s."""
    loads = []

    def factory(c):
        loads.append(1)
        return FakeLlm([])

    b = B.Brain(cfg, lease=lambda: {"ok": True}, llm_factory=factory)
    b.load()
    b.load()
    assert len(loads) == 1


def test_unload_returns_everything_to_the_unloaded_state(cfg):
    b = make_brain(cfg, ["x"])
    b.load()
    b.unload()
    s = b.status()
    assert s == {"loaded": False, "turns_served": 0, "uptime_s": 0.0,
                 "idle_s": None, "ttl_s": 90.0, "lease": False}
    assert b.describe() == {"loaded": False, "tools": [], "undescribed": []}


def test_the_load_summary_reports_the_grammar_gap_counts(cfg):
    s = make_brain(cfg, ["x"]).load()
    assert s["loaded"] is True
    assert s["tools_in_grammar"] > 0
    assert s["tools_undescribed"] > 0
    assert s["grammar_rules"] > 0


def test_a_missing_model_file_is_refused_before_any_lease(tmp_path):
    """No point taking a lease for a model that is not there."""
    b = B.Brain(B.BrainConfig(model_path=str(tmp_path / "nope.gguf")),
                lease=lambda: pytest.fail("must not lease"),
                llm_factory=lambda c: pytest.fail("must not load"))
    with pytest.raises(B.BrainError):
        b.load()


# ------------------------------------------------------- 7. plane separation


def test_importing_brain_does_not_import_the_model_runtime():
    """brain.py may import llama_cpp *lazily*; importing it must not."""
    # It is imported at module scope in a fresh interpreter by the import check
    # script, and this asserts the property that matters: the module compiles
    # grammars on demand, so a daemon that never calls load() never touches it.
    src = open(B.__file__, encoding="utf-8").read()
    top_level = [
        ln for ln in src.splitlines()
        if ln.startswith("from llama_cpp") or ln.startswith("import llama_cpp")
    ]
    assert not top_level, top_level


def test_the_brain_has_no_executor_and_cannot_execute_anything(cfg):
    """D16/D31. It proposes; Plane A disposes."""
    b = make_brain(cfg, ["tool", '{"tool": "file.delete", "arguments": {"path": "C:/x"}}'])
    b.load()
    action = b.turn("delete everything")
    assert action.tool == "file.delete"
    # the call is only ever a proposal; the module has no filesystem, no shell,
    # no policy, and no reference to the daemon
    assert not hasattr(b, "execute")
    assert not hasattr(b, "gate")
    src = open(B.__file__, encoding="utf-8").read()
    for forbidden in ("subprocess", "os.remove", "os.unlink", "shutil"):
        assert forbidden not in src, forbidden


# ------------------------------------------------------------ 8. thread-safety


def test_concurrent_turns_do_not_corrupt_the_grammar_reference(cfg):
    """The grammar object is shared; the replies are not.

    A race here would show as a turn that parsed yesterday's call, which is the
    kind of bug that only appears under load and is impossible to reproduce.
    """
    b = make_brain(cfg, ["answer"] * 40)
    b.load()
    seen = []

    def work():
        seen.append(b.turn("hi").kind)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(seen) == 8
    assert set(seen) <= {"answer", "tool"}
    assert b.status()["turns_served"] == 8
