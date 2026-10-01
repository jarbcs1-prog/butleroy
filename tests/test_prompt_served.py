"""The system prompt the model actually receives, not the one in a variable.

This exists because of a gap that was carried for a long time. Every earlier test
of the prompt checked the function; none checked what was served. Those are the
same thing only while nothing stands between them - and between them stand a
pipe, a child process, a lease handshake and a roots reader that polls the daemon
every five seconds. A prompt that is correct in the parent and empty on the
child is a brain that cannot form a usable tool call, and it fails as a refusal
rather than as an error, which is the hardest kind of failure to notice.

So the assertion is on the served bytes, read back over the same pipe the daemon
uses.
"""

import pathlib
import tempfile

import pytest

from assistant import brain_server, ipc
from assistant.brain import Brain, BrainConfig


ROOTS = ("G:/approved/one", "G:/approved/two")


class _NoLease:
    """The lease surface a brain that never loads anything needs, and no more.

    The stubs *refuse* rather than grant. A stub that granted would let a
    regression acquire gigabytes during a test run, which is precisely the failure
    this suite exists to catch and a bad stub would hide it.
    """

    def acquire(self, *a, **k):
        raise AssertionError("this test must not acquire a lease")

    def renew(self, *a, **k):
        raise AssertionError("this test must not renew a lease")

    def release(self, *a, **k):
        return False

    @property
    def available(self) -> int:
        return 0


def _brain(roots=None):
    """A brain that builds its prompt and grammar but holds no model."""
    return Brain(_config(), lease=_NoLease(), roots=roots)


def _config() -> BrainConfig:
    """A brain that will never be asked to load a model.

    `n_gpu_layers` is irrelevant because nothing here loads anything; the point
    is that prompt construction, grammar building and describing are all pure.
    """
    return BrainConfig(model_path="unused.gguf", n_gpu_layers=0)


# -- what the prompt says -----------------------------------------------------


def test_the_prompt_names_every_approved_root():
    """If a root is not named, the model reaches for somewhere it remembers."""
    prompt = _brain(lambda: ROOTS).current_prompt()
    for root in ROOTS:
        assert root in prompt, f"{root} is approved but the prompt does not say so"


def test_the_prompt_is_not_empty_without_roots():
    """An empty prompt is not a neutral prompt; it is a brain with no instructions."""
    prompt = _brain(lambda: ()).current_prompt()
    assert prompt.strip(), "a brain with no approved roots still gets instructions"


def test_the_prompt_is_not_empty_without_a_roots_reader():
    """The static-prompt path is the one a test is most likely to hit by accident."""
    brain = _brain()
    assert brain.current_prompt().strip()


def test_the_prompt_says_a_nameless_file_belongs_under_a_root():
    """The lesson that was measured, not assumed.

    With the roots listed and nothing else, the 4B answered "copy notes.txt to a
    backup copy" by reaching for `C:/Users/<name>/Desktop` - a folder it remembered
    from training data. The rule it needed was stated, not implied.
    """
    prompt = _brain(lambda: ROOTS).current_prompt()
    lowered = prompt.lower()
    assert "not in a folder you remember" in lowered
    assert "under the root" in lowered


def test_the_prompt_changes_when_the_roots_change():
    """A revoked workspace must stop being advertised, not linger in the text."""
    roots = [ROOTS[0]]
    brain = _brain(lambda: tuple(roots))
    assert ROOTS[0] in brain.current_prompt()
    roots.append(ROOTS[1])
    assert ROOTS[1] in brain.current_prompt()


def test_the_prompt_is_stable_when_the_roots_do_not_change():
    """Rebuilding on every call would be a needless cost per turn, and would make
    a prompt diff useless as evidence of anything."""
    brain = _brain(lambda: ROOTS)
    assert brain.current_prompt() == brain.current_prompt()


def test_an_absolute_path_rule_is_stated():
    """A bare name means a different thing to every process on the machine."""
    prompt = _brain(lambda: ROOTS).current_prompt()
    assert "absolute" in prompt.lower()


# -- what is served -----------------------------------------------------------


@pytest.fixture
def served():
    """A brain on the box the child reads from, and the handler that serves it.

    `brain_box` is the same dict the real child uses, so putting a brain in it is
    the same act the child's own load path performs - not a second arrangement
    invented for the test.
    """
    box = {"stop": False, "brain": _brain(lambda: ROOTS)}
    handler = brain_server._handler_factory(box, _NoLease())  # noqa: SLF001
    return handler, box


def test_the_served_prompt_names_the_roots(served):
    """The assertion this file exists for, on the bytes that cross the pipe."""
    handler, _box = served
    reply = handler(ipc.Request(op="describe", args={}, id=1))
    prompt = reply["prompt"]
    assert prompt.strip()
    for root in ROOTS:
        assert root in prompt


def test_the_served_roots_match_the_served_prompt(served):
    """Two sources of truth is one too many.

    If these ever disagree, the model has been told it may work somewhere the gate
    refuses - which is exactly the mismatch that produced "it kept reaching for
    Desktop".
    """
    handler, _box = served
    reply = handler(ipc.Request(op="describe", args={}, id=1))
    assert reply["roots"] == sorted(ROOTS)
    for root in reply["roots"]:
        assert root in reply["prompt"]


def test_describing_costs_no_model_and_says_so(served):
    """The whole description is answerable for free, so it must not claim a load.

    ``loaded`` here means a model is resident, not "we can describe this". Folding
    the two together would make the cheap question look like an expensive one and
    the expensive one look like it had happened.
    """
    handler, _box = served
    reply = handler(ipc.Request(op="describe", args={}, id=1))
    assert reply["loaded"] is False
    assert reply["tools"], "no tools are describable, which is not the same as none"
    assert reply["prompt"].strip()


def test_no_brain_at_all_is_reported_as_no_brain(served):
    """Distinct from a brain that holds nothing, which can still describe itself."""
    _handler, box = served
    box.pop("brain")
    reply = brain_server._handler_factory(box, _NoLease())(
        ipc.Request(op="describe", args={}, id=1)
    )
    assert reply == {"loaded": False}


def test_the_daemon_can_read_the_served_prompt_without_a_model(served):
    """One pipe, one authkey, the bytes the daemon would really see."""
    handler, _box = served
    with tempfile.TemporaryDirectory() as tmp:
        key = pathlib.Path(tmp) / "authkey.bin"
        ipc.store_authkey(key, b"s" * 32)
        server = ipc.PipeServer(r"\\.\pipe\test.prompt.served",
                                lambda req: handler(req),
                                authkey=b"s" * 32)
        server.start()
        try:
            client = ipc.PipeClient(r"\\.\pipe\test.prompt.served",
                                    authkey=b"s" * 32, timeout=20.0)
            reply = client.call("describe")
        finally:
            server.stop()
    assert ROOTS[0] in reply["prompt"]

# -- the two outcomes, and the grammar's agreement with them ------------------
#
# The prompt tells the model it may answer in words instead of calling a tool, and
# that this is a complete answer rather than a failure. That claim is only true
# while the decision grammar still admits the word. If the grammar drops it, the
# prompt is instructing the model to take an option it cannot take - which is what
# D49 describes happening in reverse.


def test_the_prompt_offers_answering_in_words_as_a_real_choice():
    prompt = _brain(lambda: ROOTS).current_prompt()
    assert "two ways to respond" in prompt
    assert "complete answer" in prompt


def test_the_prompt_does_not_tell_it_to_say_so_without_saying_how():
    """The exact defect D49 fixes, asserted so the old wording cannot come back.

    "Say so" with no channel for saying so is not an instruction; it is a hint that
    the model cannot act on, and it measurably lost every time.
    """
    prompt = _brain(lambda: ROOTS).current_prompt()
    assert "say so rather than guessing" not in prompt


def test_the_prompt_forbids_inventing_a_path_to_make_a_call():
    """A plausible wrong path is worse than an admission of not knowing."""
    prompt = _brain(lambda: ROOTS).current_prompt()
    assert "Never invent a path" in prompt


def test_answering_in_words_is_not_described_as_a_failure():
    """The wording has to make declining feel available, not like giving up."""
    lowered = _brain(lambda: ROOTS).current_prompt().lower()
    assert "never the wrong choice" in lowered
    assert "not expected to act on every request" in lowered


def test_the_grammar_still_admits_the_outcome_the_prompt_offers():
    """The coupling. A prompt promising an option the grammar forbids is a lie
    the model can only discover by trying, and it always tries the other one."""
    from assistant.grammar import build_named_decision_grammar
    from assistant.tools import ALL_TOOLS

    gbnf = build_named_decision_grammar(ALL_TOOLS).gbnf
    assert '"answer"' in gbnf, (
        "the prompt offers a plain-word answer but the decision grammar no longer "
        "admits one"
    )


def test_the_two_outcomes_the_prompt_offers_are_the_two_the_grammar_admits():
    """Counted on both sides, because a third outcome is how they drift apart.

    The grammar emits `answer` or `tool:<name>` - exactly two. The prompt must
    therefore describe exactly two ways to respond, and must not imply a third
    option the model cannot take. The prompt deliberately does not quote the
    grammar tokens: `tool:<name>` is an implementation detail the model never
    sees, and teaching it the syntax would tie the prompt to a grammar that is
    free to change.
    """
    from assistant.grammar import build_named_decision_grammar
    from assistant.tools import ALL_TOOLS

    root = next(line for line in build_named_decision_grammar(
        ALL_TOOLS).gbnf.splitlines() if line.startswith("root ::="))
    alternatives = root.split("::=", 1)[1].count("|") + 1
    assert alternatives == 2, f"the grammar now admits {alternatives} outcomes"

    prompt = _brain(lambda: ROOTS).current_prompt()
    assert "You have two ways to respond" in prompt
