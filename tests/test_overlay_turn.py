"""A tool turn must end in the tool running, not in a line of prose about it.

This is the file for the defect that 1,080 passing tests did not contain.

What was wrong
--------------
`Overlay._run_turn` printed `proposing <tool> (<args>)` and returned. It never
sent the proposal back through `tool_call`, which is the only path to an action
(`daemon.op_brain_turn` says so in its own docstring: "It returns the tool name
and arguments to the caller, who must send them back through `tool_call`"). So
the window - the one front door the user is physically sitting in front of -
could never run a tool at all.

It failed in the worst available way (D36, one layer up). Nothing said so:

* the transcript read like an answer in progress, so a dead end looked slow;
* the audit chain verified, because the proposal genuinely was audited;
* `system.gpu_state` is T0 and needs no approval, so the read that should have
  worked most obviously did not;
* and every other front door was fine, which is what made it look like a model
  problem rather than a missing line. `_voice_turn` carries its proposal through.
  Telegram deliberately does not, and says so. The window did neither.

So the finding is NOT D34's. The grammar commits; the plumbing stopped afterwards.

What these tests are for
-----------------------
The project's blind spot, in its own words: *tests that assert a property of a
component in isolation cannot find a defect living in the transition between two
instances of it.* Every test here drives `brain_turn` -> `tool_call` on one
overlay against one daemon-shaped `call`, and asserts on the conversation that
comes out. The FakeDaemon is the transport boundary (the module's existing
convention), not a mock of the code under test.

Two of them are marked MUTATION CHECK: revert the fix and they must go red.
"""

from __future__ import annotations

import pytest

from assistant import overlay as O


# --------------------------------------------------------------------------- #
# a daemon that actually commits to a tool
# --------------------------------------------------------------------------- #


GPU = {
    "gpus": [{
        "name": "NVIDIA GeForce RTX 3060",
        "vram_total_bytes": 12884901888.0,
        "vram_used_bytes": 939524096.0,
        "vram_free_bytes": 11765022720.0,
        "utilisation_percent": 5.0,
        "temperature_c": 49.0,
    }],
    "count": 1,
    "measured": True,
}


class CommittingDaemon:
    """Stands in for `Daemon.handle` on a turn that COMMITS.

    `tool_call` is answered with a real payload rather than `{}`, because a
    fake returning an empty dict would let a fix that prints "Done: X." pass -
    and "Done: X." is precisely what D68 rendered as a successful answer while
    the user learned nothing.
    """

    def __init__(self, *, tool_result=None, tool_error=None, not_implemented=False):
        self.tool_result = GPU if tool_result is None else tool_result
        self.tool_error = tool_error
        self.not_implemented = not_implemented
        self.calls = []

    def __call__(self, op, **args):
        self.calls.append((op, args))
        if op == "brain_turn":
            return {"ok": True, "result": {
                "kind": "tool",
                "tool": "system.gpu_state",
                "arguments": {},
                "text": '{"tool":"system.gpu_state","arguments":{}}',
                "latency_s": 0.21,
            }}
        if op == "tool_call":
            if self.tool_error is not None:
                return {"ok": False, "error": self.tool_error, "refused": True}
            if self.not_implemented:
                return {"ok": False, "not_implemented": True,
                        "error": "system.gpu_state is registered but has no "
                                "executor yet."}
            return {"ok": True, "result": self.tool_result}
        if op == "confirm_list":
            return {"ok": True, "result": {"pending": [], "grants": {}}}
        return {"ok": True, "result": {}}

    def ops(self):
        return [op for op, _ in self.calls]

    def args_for(self, op):
        for name, a in self.calls:
            if name == op:
                return a
        raise AssertionError(f"{op} was never called; saw {self.ops()}")


class AnsweringDaemon(CommittingDaemon):
    """A turn that legitimately has nothing to call."""

    def __call__(self, op, **args):
        self.calls.append((op, args))
        if op == "brain_turn":
            return {"ok": True, "result": {
                "kind": "answer", "text": "Hello.", "latency_s": 0.1}}
        if op == "confirm_list":
            return {"ok": True, "result": {"pending": [], "grants": {}}}
        return {"ok": True, "result": {}}


@pytest.fixture
def window():
    """An overlay with a real `_run_turn`, and no Tk anywhere.

    `_run_turn` is called directly rather than through `_submit`, because
    `_submit` spawns a thread and the property under test is what the turn does,
    not how it was scheduled. The events it posts are drained by hand.
    """
    def build(daemon):
        ov = O.Overlay(daemon)
        ov._root = object()      # non-None: no Tk is ever constructed
        return ov
    return build


def said(ov):
    """Everything the overlay told the user, in order."""
    out = []
    while True:
        try:
            kind, *rest = ov._events.get_nowait()
        except Exception:
            return out
        if kind == "say":
            out.append(str(rest[0]))


def run(ov, prompt="how much VRAM am I using right now"):
    # `_submit` sets `_busy` before it spawns the turn thread, so a test that
    # calls `_run_turn` directly must set it too. It did not at first, and
    # `test_a_raise_mid_turn...` passed against a mutation that removed the
    # `finally` - a green test asserting that a flag was False when nothing had
    # ever set it True. Found by trying to break it, which is the only way.
    ov._busy = True
    ov._run_turn(prompt)
    return said(ov)


# --------------------------------------------------------------------------- #
# the defect
# --------------------------------------------------------------------------- #


def test_a_tool_turn_reaches_tool_call(window):
    """MUTATION CHECK. Revert `_carry_through` and this goes red.

    The whole defect in one assertion: a committed proposal must be sent back
    through the only path to an action. Not "the window called something", and
    not "the window displayed the tool name" - it displayed the tool name
    perfectly well while the tool never ran, which is what made this so easy to
    miss.
    """
    d = CommittingDaemon()
    ov = window(d)
    run(ov)
    assert "tool_call" in d.ops(), (
        "the model committed to system.gpu_state and the window never sent the "
        f"proposal anywhere; it asked for {d.ops()}"
    )


def test_the_real_output_comes_back_not_a_summary_of_it(window):
    """MUTATION CHECK. The user asked for a number and must be shown a number.

    Asserted on the *payload*, because the failure mode is a window that looks
    finished. `_describe_result` would render this as `Done: system.gpu_state.`
    - correct for a voice turn read aloud, useless here, and indistinguishable
    from success on screen.
    """
    ov = window(CommittingDaemon())
    lines = "\n".join(run(ov))
    assert "3060" in lines, lines
    # Grouped separators: the payload arrives as raw float bytes and
    # `vram_used_bytes: 939524096` is not a number a person reads off a screen.
    assert "939,524,096" in lines, (
        "the VRAM figure the user asked for is not on screen:\n" + lines
    )
    assert "12,884,901,888" in lines, lines


def test_the_proposal_is_still_shown_before_the_result(window):
    """The user sees what was called, not only what came back.

    Ordering, not cosmetics: a window that showed only the result would hide
    which tool produced it, and `system.diagnose` and `system.gpu_state` are
    both one line of text with very different meanings.
    """
    ov = window(CommittingDaemon())
    lines = run(ov)
    assert any("proposing system.gpu_state" in line for line in lines), lines
    assert any("proposing system.gpu_state" in line for line in lines[:1]), lines


def test_the_call_carries_the_channel_the_gate_needs(window):
    """`channel="pc"` is what makes T2 reachable and T3 askable.

    Dropped on the floor, the gate falls back to its default channel - which
    does not assert presence - so a read would still pass but the whole tier
    model would be quietly wrong for the one front door the user is watching.
    """
    d = CommittingDaemon()
    ov = window(d)
    run(ov)
    args = d.args_for("tool_call")
    assert args["channel"] == "pc", args
    assert args["tool"] == "system.gpu_state", args
    assert args["actor"] == "overlay", args


def test_an_answer_turn_still_does_not_call_a_tool(window):
    """The fix must not make the window invent work.

    `Hello` produced a correct answer before D68 and must keep doing so, with no
    `tool_call` anywhere in the exchange.
    """
    d = AnsweringDaemon()
    ov = window(d)
    lines = run(ov, "Hello")
    assert lines == ["Hello."], lines
    assert "tool_call" not in d.ops(), d.ops()


# --------------------------------------------------------------------------- #
# the three outcomes that are not "it ran"
# --------------------------------------------------------------------------- #


def test_a_t3_proposal_waits_for_a_human_and_says_which_one(window):
    """The window cannot approve, so it must say what it is waiting for.

    `tool_call` answers a T3 with a confirmation id and nothing else. The armed
    poll raises the dialog within a second, so the id in the transcript is what
    the user has when the dialog lands on top of it.
    """
    class NeedsConfirm(CommittingDaemon):
        def __call__(self, op, **args):
            if op == "tool_call":
                self.calls.append((op, args))
                return {"ok": True, "result": {
                    "needs_confirmation": True,
                    "confirmation_id": "c000007",
                    "tool": "file.delete",
                    "payload_hash": "a" * 64,
                }}
            return super().__call__(op, **args)

    ov = window(NeedsConfirm())
    lines = "\n".join(run(ov, "delete notes.txt"))
    assert "c000007" in lines, (
        "a T3 proposal with no visible id leaves the user unable to tell what "
        f"the dialog is about:\n{lines}"
    )
    # The id alone is not enough. Asserting only on it let a mutation through
    # that dropped the whole branch: the envelope's own `confirmation_id` field
    # then reached the transcript as rendered data, so the substring was present
    # and the test passed while the window never said it was waiting. It has to
    # say so in words.
    assert "waiting for your approval" in lines, (
        "the window must state that it is waiting on a human, not render the "
        f"pending envelope as if it were a result:\n{lines}"
    )
    # And it must NOT read as though the deletion happened.
    assert "deleted" not in lines.lower(), lines


def test_a_refusal_is_reported_as_a_refusal(window):
    """The gate said no, and the window has to say so.

    Silence here is the D36 shape one layer up: the model proposed a read, the
    gate refused it, and the user was left with a transcript ending in a
    confident-looking line.
    """
    ov = window(CommittingDaemon(tool_error="file.read: not an approved root"))
    lines = "\n".join(run(ov, "read my diary"))
    assert "refused" in lines.lower(), lines
    assert "not an approved root" in lines, lines


def test_an_unbuilt_tool_is_named_as_unbuilt(window):
    """"I will not do this" and "I have not built this yet" are different
    answers. The daemon distinguishes them with a flag; the window must too."""
    ov = window(CommittingDaemon(not_implemented=True))
    lines = "\n".join(run(ov))
    assert "no executor yet" in lines, (
        "a build gap reported as a refusal is how unfinished work gets reported "
        f"as finished:\n{lines}"
    )


def test_a_queued_action_is_not_reported_as_done(window):
    """Queued means "waiting for you at the keyboard", and it must read that way."""
    class Queued(CommittingDaemon):
        def __call__(self, op, **args):
            if op == "tool_call":
                self.calls.append((op, args))
                return {"ok": True, "result": {
                    "queued": True, "reason": "T3 needs the PC channel",
                    "depth": 1}}
            return super().__call__(op, **args)

    ov = window(Queued())
    lines = "\n".join(run(ov))
    assert "queued" in lines.lower(), lines


# --------------------------------------------------------------------------- #
# the busy flag: a window that stops listening
# --------------------------------------------------------------------------- #


def test_a_raise_mid_turn_does_not_leave_the_window_permanently_deaf(window):
    """`_busy` gates `_submit`, so a missed reset is a dead input line.

    This one is about the `finally` rather than the carry-through. The symptom
    would be a window that looks entirely healthy and silently refuses every
    later question - no exception, no log line, nothing on screen.
    """
    class Exploding(CommittingDaemon):
        def __call__(self, op, **args):
            if op == "tool_call":
                raise RuntimeError("pipe went away mid-turn")
            return super().__call__(op, **args)

    ov = window(Exploding())
    # `_submit` sets this before it spawns the thread. Without it the flag starts
    # False and `assert ... is False` passes against code that never resets it -
    # which is exactly what happened: this test survived the mutation that
    # removed the `finally`. Set the precondition or do not assert the outcome.
    ov._busy = True
    with pytest.raises(RuntimeError):
        ov._run_turn("how much VRAM am I using right now")
    assert ov._busy is False, (
        f"the window would refuse every future question; _busy is {ov._busy}"
    )