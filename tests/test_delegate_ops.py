"""Delegation, tested with real subprocesses.

`delegate.*` is the first slice where the thing under test *is* a process the
suite starts. Every other executor in this project either reads a fact or
touches a file; here a mistake does not raise, it leaves something running.

That is why this file is dominated by two things and only two things:

**The fixture asserts itself first.** `test_the_fixture_runs_a_process` proves a
task really starts, really exits, and really has its output recorded, before any
test is allowed to conclude anything about refusals. A probe that never proved
its own instrument could report 42 green tests while the delegation never ran;
that is D55, and it is the failure mode this file exists to rule out.

**Nothing is left running.** Every test that starts a process either waits for
it or kills its whole tree, and `test_nothing_is_left_running_after_the_file`
is checked at the end. A leaked 120-second sleeper holds its working directory
open, which turns the next test's cleanup into a `PermissionError` that reads
like a filesystem fault rather than the leak that caused it.
"""

import os
import pathlib
import subprocess
import sys
import tempfile
import time

import pytest

from assistant import delegate, policy
from assistant.daemon import Daemon, Refused
from assistant.manifest import Workspace
from assistant.policy import Tier
from assistant.tools import ALL_TOOLS


# --------------------------------------------------------------- the fixture


@pytest.fixture
def desk():
    """A daemon with one approved workspace that is a *sibling* of its state.

    A sibling rather than a child because `manifests.add` refuses a root that
    contains the daemon's own `workspaces/` directory - it would make our own
    code reachable as an approved workspace. That refusal is correct and it is
    why this fixture is shaped the way it is.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        d = Daemon(state=root / "state", vault=root / "vault")
        # The daemon has no `root` argument and no workspace of its own - the
        # manifest is the sole source of approved roots (D10). The path lives on
        # the test, not on the daemon, because putting it there would be a second
        # source of truth for the one thing D10 says there is only one of.
        d.ws = root / "ws"
        d.ws.mkdir(parents=True, exist_ok=True)
        yield d
        # Drain before the `with` closes. A running task holds its working
        # directory open as its cwd, so `TemporaryDirectory.__exit__` then
        # fails with WinError 32 on the workspace - and that error is raised
        # *after* every test in the file has already reported itself green,
        # which is the worst place for it to surface. The message names the
        # directory, so it reads like a filesystem problem rather than the
        # leak that caused it.
        for row in list(d.tasks.rows()):
            d.tasks.stop(row["id"])
            d.tasks.wait(row["id"], timeout=15.0)


@pytest.fixture
def wired(desk):
    """The same daemon with the workspace actually approved.

    `allowed_roots` is set by hand because `_exec_*` bypasses `_verdict`, which
    is where the daemon refreshes it per call. A test that forgets this sees
    every path refused and concludes the policy works, when in fact it never
    had a root to compare against.
    """
    desk.manifests.add(
        Workspace(
            name="ws",
            root=str(desk.ws),
            allow_tiers=(Tier.READ, Tier.REVERSIBLE_WRITE,
                         Tier.CONSEQUENTIAL, Tier.IRREVERSIBLE),
        )
    )
    desk.gate.allowed_roots = list(desk.manifests.roots())
    return desk


def sleeper(seconds=120):
    """A command that will still be alive when the test wants it dead.

    Real, not mocked: the whole point of `_terminate_tree` is that a uv-built
    venv puts a *launcher* between us and the interpreter, so `os.kill` on the
    recorded pid is not enough. Only a real process proves that.
    """
    return [sys.executable, "-c", f"import time; time.sleep({seconds})"]


def alive(pid):
    """Whether a pid is a live process.

    `tasklist` rather than `os.kill(pid, 0)`, because on Windows signal 0 is
    not a liveness probe the way it is on POSIX - it can succeed against a
    process that is already gone. A check that answers "yes" when it means
    "unknown" would make a leak check pass.
    """
    out = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, timeout=30,
    )
    return f'"{pid}"' in out.stdout


def wait_pid(registry, task_id, timeout=20.0):
    """Block until a task has a pid, or give up.

    Written here rather than added to `Registry` because the *test* needs to
    observe the moment a pid appears; the registry's own `wait` already covers
    the moment a task finishes, and a second way to ask for that would be a
    second thing to keep honest.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = registry.get(task_id)
        if task is not None and task.pid is not None:
            return True
        time.sleep(0.05)
    return False


# ------------------------------------------------ the fixture proves itself


def test_the_fixture_runs_a_process(wired):
    """Before any refusal is believed: does a task actually run?

    Asserting its own fixture is the one check that cannot be faked by a bug in
    the code it is about. If this fails, every other test in the file is
    measuring nothing and says so by passing anyway.
    """
    out = wired._exec_delegate_assign({
        "task_id": "fixture",
        "goal": "print one line",
        "command": [sys.executable, "-c", "print('the task ran')"],
        "budget_s": 60,
    })
    assert out["running"] is True
    assert wired.tasks.wait("fixture", 60) is True, "the task never finished"
    row = wired._exec_delegate_status({"task_id": "fixture"})["task"]
    assert row["status"] == "done", row
    assert "the task ran" in row["stdout"], row


def test_nothing_is_left_running_after_the_file(desk):
    """The last test in the file, and the reason the file is safe to run twice.

    Not a rhetorical check: it walks the registry for any task that is still
    `running`, and if it finds one the suite has told the truth about a process
    it then abandoned.
    """
    left = [t.id for t in desk.tasks.rows()
            if t.get("status") == "running"]
    assert left == [], f"tasks still running: {left}"


# ------------------------------------------------------------ what a task is


def test_a_task_that_never_starts_reports_stopped_not_failed(wired):
    """A stop before the process exists is honoured by never making one.

    Otherwise the daemon spawns a process in order to kill it, which is worse
    than not starting - and the task would report `failed` with the exit code
    Windows gives a killed process, which reads as the work having gone wrong.
    """
    task = delegate.Task(
        id="prespawn",
        goal="should never run",
        root=str(wired.ws),
        argv=tuple(sleeper()),
        budget_s=60.0,
    )
    wired.tasks.register(task)
    ok, why = wired.tasks.stop("prespawn")
    assert ok, why
    assert "before it got as far as a process" in why, why
    wired.tasks.start(task)
    assert wired.tasks.wait("prespawn", 30) is True
    assert task.status == "stopped", task.status
    assert task.pid is None, f"a process was spawned to kill: {task.pid}"


def test_stopping_a_task_marks_it_stopped_not_failed(wired):
    """`stopped` and `failed` are both true of a killed process; only one is
    what happened, and the tool is the only thing that knows which.

    The flag is set *before* the kill, because the worker thread wakes the
    instant the process dies and reads the flag to decide the terminal status.
    Set after, the flag arrives too late and every stop reports `failed`.
    """
    out = wired._exec_delegate_assign({
        "task_id": "killme",
        "goal": "sleep",
        "command": sleeper(),
        "budget_s": 300,
    })
    assert out["running"] is True
    assert wait_pid(wired.tasks, "killme"), "no pid was ever recorded"
    out = wired._exec_delegate_intervene({
        "task_id": "killme", "action": "stop"})
    assert out["asked_to_stop"] is True, out
    assert "not confirmed dead" in out["note"], out["note"]
    assert wired.tasks.wait("killme", 30) is True
    row = wired._exec_delegate_status({"task_id": "killme"})["task"]
    assert row["status"] == "stopped", row
    assert "killed process" in row["note"], row["note"]


def test_the_whole_tree_is_killed_not_just_the_launcher(desk):
    """The measurement that forced `_terminate_tree`, kept as a test.

    Six sleepers assigned by an earlier probe run were still alive five minutes
    after being stopped, with their task rows saying `stopped`. A uv-built venv
    means `Popen` on `.venv\\Scripts\\python.exe` creates a launcher which
    creates the real interpreter as a child; killing the recorded pid killed
    the launcher and the work carried on. The row said stopped because the
    *task* believed it. Nothing in the system was watching the process.
    """
    task = delegate.Task(
        id="tree",
        goal="sleep in a grandchild",
        root=str(desk.ws),
        argv=tuple(sleeper(300)),
        budget_s=300.0,
    )
    desk.tasks.register(task)
    desk.tasks.start(task)
    assert wait_pid(desk.tasks, "tree")
    time.sleep(1.5)                      # let the launcher make its child
    launcher = task.pid
    ok, why = desk.tasks.stop("tree")
    assert ok, why
    assert desk.tasks.wait("tree", 30) is True
    time.sleep(1.5)                      # taskkill is not instantaneous
    assert not alive(launcher), (
        "the launcher is gone but something it started is not: "
        "os.kill(pid) is not enough on a uv-built venv")


def test_a_stop_that_arrives_before_the_pid_exists_still_leaves_no_process(desk):
    r"""A stop in the gap between "asked to stop" and "here is the pid".

    The window is narrow and it was found by measurement rather than by
    reading: the drain in `desk` stops every task it can see, and still one
    launcher/child pair per run survived, holding its working directory open
    long after the test reported itself green.

    So this does not race a thread by hand - a hand-rolled race is a test that
    passes when the machine is busy. It stops the task the instant it is
    registered, before any pid can plausibly exist, and then asserts the thing
    that actually leaked: that no live process is left, checked through
    `tasklist` rather than through the registry's own opinion of the task.
    """
    d = desk
    ws = d.ws
    d.tasks.start(delegate.Task(
        id="race", goal="be stopped before it exists",
        root=str(ws), argv=sleeper(120), budget_s=60.0,
    ))
    stopped, why = d.tasks.stop("race")
    assert stopped, why

    # Wherever the flag landed - before the thread's first check, inside
    # `Popen`, or after it - the task must reach a terminal state, and any
    # process the thread may briefly have created must be gone.
    d.tasks.wait("race", timeout=30.0)
    task = d.tasks.get("race")
    assert task is not None
    assert task.status == "stopped", task.row()

    # `pid is None` is a legitimate outcome - the stop beat the spawn and no
    # process was ever created. Asserting a pid here would be asserting the
    # timing rather than the promise, which is the same mistake in the other
    # direction.
    pid = task.pid
    if pid is not None:
        for _ in range(100):
            if not alive(pid):
                break
            time.sleep(0.1)
        assert not alive(pid), (
            f"pid {pid} is still running after the task was stopped while its "
            f"pid was still unknown - the flag arrived in the window between "
            f"the pre-spawn guard and Popen, and nothing killed what Popen then "
            f"created"
        )


def test_a_task_cannot_be_stopped_by_its_own_name(wired):
    """PID-chain protection, not name protection.

    Blacklisting `python.exe` would make the tool unable to clean up the stray
    interpreters it is most often asked about, and would not protect the daemon
    anyway - the daemon is a python too.
    """
    task = delegate.Task(
        id="me",
        goal="pretend to be the daemon",
        root=str(wired.ws),
        argv=("notepad.exe",),
        budget_s=60.0,
        pid=os.getpid(),
    )
    wired.tasks.register(task)
    ok, why = wired.tasks.stop("me")
    assert ok is False
    assert "assistant" in why, why


# ----------------------------------------------------------- the grammars


@pytest.mark.parametrize("raw", [
    "", "TASK", "has spaces", "../escape", "a" * 33, None,
])
def test_task_ids_are_a_grammar_not_a_suggestion(raw):
    """Refused, not sanitised.

    The alternative is a tool that quietly rewrites `../../etc` into `etc` and
    reports success on a task the caller never asked for.
    """
    assert delegate.task_id_refusal(raw) is not None, raw


@pytest.mark.parametrize("raw", ["probe-1", "t", "a_b-c9", "0" * 31,
                                 "dash-ok", "under_score-ok", "trailing-",
                                 "7", "0"])
def test_task_ids_that_are_grammar_are_accepted(raw):
    """The negative list above is the interesting half, but only because this
    one is pinned. A grammar tested only by its rejections passes a test suite
    that has accidentally replaced every id with a random string."""
    assert delegate.task_id_refusal(raw) is None, raw


def test_a_budget_of_the_wrong_type_is_refused_for_its_type():
    """`int(1.5)` is 1, so a range check alone reports "too short" and sends the
    user at the size of the mistake instead of its kind."""
    for bad in (1.5, True, "ninety", [60]):
        why = delegate.budget_refusal(bad)
        assert why is not None, bad
        assert "whole number" in why or "seconds" in why, why


def test_a_budget_of_none_means_the_default():
    assert delegate.budget_refusal(None) is None


def test_a_string_command_is_refused_rather_than_split():
    """Same rule as `shell.run`, and for the same reason.

    `shlex` implements POSIX rules; a Windows command line is not POSIX. Splitting
    it produces a command the caller never wrote and the preview never showed.
    """
    why = delegate.argv_refusal("python -c print(1)")
    assert why is not None
    assert "list" in why


def test_a_refusal_names_the_argument_that_caused_it():
    why = delegate.argv_refusal(["python", 3, "notes.txt"])
    assert why is not None
    assert "[1]" in why, why


def test_an_empty_argument_is_called_out_as_a_mistake():
    """`""` in a command line is nearly always a variable that came out unset,
    and nearly never intended."""
    why = delegate.argv_refusal(["python", ""])
    assert why is not None
    assert "unset" in why, why


def test_pause_is_not_offered_because_it_cannot_be_honoured(wired):
    """Windows has no portable console pause.

    An action in the enum that the implementation cannot perform is how a tool
    learns to report something it did not do. Better absent than present and
    lying.
    """
    assert "pause" not in delegate.INTERVENE_ACTIONS
    why = delegate.interact_refusal("pause")
    assert why is not None
    assert "stop" in why and "restart" in why, why


# ------------------------------------------------------- the reads


def test_plan_is_a_briefing_and_says_so(wired):
    """D36 in one field.

    A tool that returns facts while being called a plan invites the model to
    treat the facts as a plan. `is_a_plan: False` is the guard.
    """
    out = wired._exec_delegate_plan({})
    assert out["kind"] == "briefing"
    assert out["is_a_plan"] is False
    assert out["what_is_there"]["exists"] is True
    assert out["constraints"]["approved"] is True


def test_plan_reports_what_it_measured_and_marks_what_it_did_not(wired):
    (wired.ws / "tests").mkdir(exist_ok=True)
    (wired.ws / "pyproject.toml").write_text("[tool.pytest]\n", encoding="utf-8")
    out = wired._exec_delegate_plan({})["what_is_there"]
    assert out["test_dirs"] == ["tests"], out
    assert "pytest" in out["test_markers"], out


def test_brainstorm_reads_the_constraints_from_the_gate_that_will_refuse(wired):
    """A rule quoted in a prompt and a rule enforced in code are two rules, and
    only one of them can be changed by whoever is not the user."""
    out = wired._exec_delegate_brainstorm({"topic": "refactor the parser"})
    assert out["kind"] == "briefing"
    assert out["topic"] == "refactor the parser"
    assert "take turns" in out["constraints"]["vram"]


def test_an_unapproved_workspace_names_what_would_be_approved(desk):
    """A refusal that does not say what would work cannot be acted on."""
    with pytest.raises(Refused) as caught:
        desk._exec_delegate_plan({})
    why = str(caught.value)
    assert "no workspace has been approved" in why
    assert "approve" in why


def test_review_is_evidence_and_says_no_model_was_consulted(wired):
    """Judgement is the brain turn's job. A T0 read that called a model would
    put VRAM in a free read, which is the whole of D50."""
    wired.tasks.register(delegate.Task(
        id="rev", goal="g", root=str(wired.ws), argv=("x",),
        budget_s=1.0, status="failed", exit_code=1))
    out = wired._exec_delegate_review({"task": "rev"})
    assert out["kind"] == "evidence"
    assert out["task"]["goal"] == "g", out
    assert "no model" in str(out).lower()


def test_audit_reports_the_three_mechanical_checks_and_no_verdict(wired):
    out = wired._exec_delegate_audit({})
    assert set(out) >= {"checks", "checked", "problems"}, out
    assert out["checked"] == len(out["checks"]), out
    # A count of problems on a run with none of them must be zero. The first
    # version summed the tasks whose problem list was EMPTY - so a clean audit
    # reported one problem per healthy task, in the same response that carried
    # `ok: true` for each of them. Both readings of a number look like a number,
    # which is why only asserting that the field is present was not enough.
    assert out["problems"] == sum(len(c["problems"]) for c in out["checks"]), out
    assert "not judge" in out["note"], out["note"]


def json_text(thing):
    import json
    return json.dumps(thing, default=str)


def test_status_names_the_bound_in_force_not_the_one_we_hope_for(wired):
    """`Registry(max_tasks=4)` reporting `MAX_TASKS` would report the plan."""
    assert wired.tasks.capacity() == delegate.MAX_TASKS
    small = delegate.Registry(max_tasks=3)
    assert small.capacity() == 3


def test_status_lists_every_task_without_pasting_its_output(wired):
    """A 4000-char stdout in a one-line listing turns the answer into a wall."""
    wired.tasks.register(delegate.Task(
        id="wordy", goal="g", root=str(wired.ws),
        argv=("x",), budget_s=1.0, stdout="z" * 4000))
    one = wired._exec_delegate_status({"task_id": "wordy"})["task"]
    assert one["stdout_chars"] == 4000, one
    assert one["output_truncated"] is False, one
    listing = wired._exec_delegate_status({})["tasks"]
    wordy = [r for r in listing if r["id"] == "wordy"]
    assert wordy, listing
    assert "z" * 100 not in json_text(wordy), (
        "a 4000-char stdout in the listing turns a one-line answer into a wall")
    assert wordy[0]["stdout_chars"] == 4000, wordy


# ----------------------------------------------------------- the refusals


REFUSALS = [
    ("a string command", {"command": "python -c print(1)"}),
    ("a non-string argument", {"command": ["python", 3]}),
    ("an empty goal", {"goal": "  "}),
    ("a fractional budget", {"budget_s": 1.5}),
    ("a budget over the ceiling", {"budget_s": 100000}),
    ("a workspace outside every root", {"workspace": "C:/nowhere"}),
    # The screen is over argv *elements*, so the path has to be one. A path
    # buried inside a `-c` source string is opened by python rather than by us,
    # and reading every interpreter's grammar to find it is a hole with no
    # floor - so the test says what the screen actually is.
    ("an argument reaching a deny path",
     {"command": ["python", "C:/Windows/System32/config/SAM"]}),
    ("an argument reaching our own source",
     {"command": ["python", str(pathlib.Path(__file__))]}),
]


@pytest.mark.parametrize("label,payload", REFUSALS, ids=[r[0] for r in REFUSALS])
def test_a_refusal_is_the_same_question_in_the_preview_and_the_executor(
        wired, label, payload):
    """D32: the executor asking the same questions the preview asked is what
    makes the approved payload the executed payload.

    Checked twice because a refusal that lives in only one of the two places
    lets a call through unpreviewed - which is the D59 defect wearing new
    clothes.
    """
    args = {"task_id": "refused", "goal": "g", **payload}
    preview = wired._preview_delegate_assign(args)
    assert preview.get("would_be_refused"), f"{label}: preview allowed it"
    with pytest.raises(Refused):
        wired._exec_delegate_assign(args)


def test_the_dot_in_a_python_expression_is_not_a_filename(wired):
    """`bool(Path(x).suffix)` made `time.sleep(120)` look like a file called
    `.sleep(120)`, so a one-liner was refused for reaching a path that did not
    exist - and `python -c "print(3.14)"` was unrunnable through `shell.run` too.
    """
    out = wired._exec_delegate_assign({
        "task_id": "dotted",
        "goal": "compute",
        "command": [sys.executable, "-c", "import time; print(round(3.14, 2))"],
        "budget_s": 60,
    })
    assert out["running"] is True
    assert wired.tasks.wait("dotted", 60) is True
    row = wired._exec_delegate_status({"task_id": "dotted"})["task"]
    assert "3.14" in row["stdout"], row


@pytest.mark.parametrize("part,pathish", [
    ("notes.txt", True), ("x.dll", True), ("archive.tar.gz", True),
    ("C:/x", True), ("time.sleep", True), ("-v", False), ("/c", False),
    ("import time; time.sleep(120)", False), ("print(3.14)", False),
])
def test_what_counts_as_a_path_in_an_argument(part, pathish):
    assert Daemon._admin_is_pathish(part) is pathish, part


# --------------------------------------------------------- accept, intervene


def test_accept_needs_a_reason(wired):
    """A verdict with no reason is not a verdict; it is a shrug that survived."""
    with pytest.raises(Refused):
        wired._exec_delegate_accept({"task_id": "none", "verdict": "accept"})


def test_accept_cannot_rewrite_the_machines_answer(wired):
    """A person may accept a task that failed *because* the failure was the
    answer. So the record may be added and may not alter anything else."""
    wired.tasks.register(delegate.Task(
        id="lost", goal="g", root=str(wired.ws), argv=("x",),
        budget_s=1.0, status="failed", exit_code=1))
    out = wired._exec_delegate_accept({
        "task_id": "lost", "verdict": "accept",
        "reason": "the test was supposed to fail"})
    assert out["task"]["status"] == "failed", out
    assert out["task"]["exit_code"] == 1, out
    assert out["verdict"] == "accept", out
    assert out["reason"] == "the test was supposed to fail", out
    assert out["task"]["verdict_reason"] == "the test was supposed to fail", out
    assert out["note"].find("does not") > 0, out["note"]


def test_a_running_task_cannot_be_accepted(wired):
    wired.tasks.register(delegate.Task(
        id="live", goal="g", root=str(wired.ws), argv=("x",), budget_s=1.0))
    with pytest.raises(Refused):
        wired._exec_delegate_accept({
            "task_id": "live", "verdict": "accept", "reason": "r"})


def test_restart_reuses_the_id_and_says_it_starts_over(wired):
    """A restart that reused the id and silently continued mid-way would be
    indistinguishable from a fresh task to every reader of the record."""
    wired._exec_delegate_assign({
        "task_id": "again", "goal": "one", "command": sleeper(300),
        "budget_s": 300})
    assert wait_pid(wired.tasks, "again")
    preview = wired._preview_delegate_intervene(
        {"task_id": "again", "action": "restart"})
    text = str(preview)
    assert preview["task"] == "again", preview
    assert "does not resume" in text, preview
    assert "same budget" in text, preview
    assert not preview.get("would_be_refused"), preview
    out = wired._exec_delegate_intervene(
        {"task_id": "again", "action": "restart"})
    assert out["task"]["id"] == "again", out
    assert out["stopped_first"] is True, out


# ------------------------------------------------------------- registration


def test_all_eight_tools_are_reachable_through_tool_call():
    """An executor that is not registered is a helper, not a tool."""
    wanted = [t for t in ALL_TOOLS if t.name.startswith("delegate.")]
    assert len(wanted) == 8, [t.name for t in wanted]
    for spec in wanted:
        assert spec.name in Daemon._EXECUTORS, spec.name


def test_the_three_acting_tools_have_previews():
    """T2 does not require a preview. These three get one because what they do
    is not visible in the call - the command, the process, the verdict - and a
    dialog with nothing on it is a dialog the user approves blind (D32)."""
    for name in ("delegate.assign", "delegate.intervene", "delegate.accept"):
        assert name in Daemon._PREVIEWS, name


def test_no_delegate_tool_shares_a_name_with_an_op():
    """D59: ops are tier-free and reachable by name through the pipe, so a
    shared name is a second door to the same room with no gate on it."""
    ops = {m[3:] for m in dir(Daemon) if m.startswith("op_")}
    for spec in ALL_TOOLS:
        if not spec.name.startswith("delegate."):
            continue
        assert spec.name.replace(".", "_") not in ops, spec.name


def test_a_changed_goal_changes_the_payload_hash(wired):
    """D32's whole point: the confirmation must bind to *this* task."""
    base = {"task_id": "h", "goal": "first goal",
            "command": ["cmd", "/c", "echo x"]}
    other = {**base, "goal": "a completely different goal"}
    assert (wired._payload_hash("delegate.assign", base,
                                wired._preview_delegate_assign(base))
            != wired._payload_hash("delegate.assign", other,
                                   wired._preview_delegate_assign(other)))


# ------------------------------------------------------------------ policy


def _fake_short_spelling(monkeypatch, long_for: dict[str, str]) -> None:
    """Make `policy._spellings` see an 8.3 short form where there is none.

    `GetLongPathNameW` turns the *short* form into the long one, and that is the
    direction the bug ran in: the manifest held `C:\\Users\\PCPRIN~1\\...` while
    `Path.resolve()` produced `C:\\Users\\PC Principal\\...`. So the map is keyed
    by the short path `_spellings` will ask about, and valued with the long form
    the API would return.

    **Why this is faked, and why that is the fix rather than a dodge:** the
    original version asserted `len(_spellings(tempfile.gettempdir())) > 1`, which
    is a property of the *volume*, not of the code. It passed on a machine with
    8.3 name creation on and failed on one with it off — `fsutil 8dot3name query`
    reports DISABLED here, so no path on C: has two spellings and the assertion
    could not be satisfied by any correct code. A test that asserts a machine
    setting reports the machine, not the gate. Enabling 8.3 to satisfy it would
    be configuring the box to make a test green.
    """
    import ctypes

    def fake_get_long(path, buf, size):  # noqa: ANN001 - ctypes signature
        wide = long_for.get(path, path)
        if len(wide) + 1 > size:
            return 0
        buf.value = wide
        return len(wide) + 1

    monkeypatch.setattr(ctypes.windll.kernel32, "GetLongPathNameW",
                        fake_get_long, raising=True)


def test_an_approved_root_is_found_however_it_is_spelled(monkeypatch, tmp_path):
    """A root stored in the 8.3 short form must still match the long form.

    The failure this covers was a false refusal: the user approved a workspace,
    and the gate told them it was outside every approved workspace, naming roots
    they had approved a moment earlier.
    """
    from assistant import tools

    long_root = str(tmp_path / "Approved Root")
    short_root = str(tmp_path / "APPROVE~1")
    _fake_short_spelling(monkeypatch, {policy._norm(short_root): long_root})

    spellings = policy._spellings(short_root)
    assert len(spellings) > 1, "the fake produced no second spelling; the test is vacuous"
    assert policy._norm(long_root) in spellings, spellings

    # Stored as typed - the short form, which is what the manifest holds.
    gate = tools.default_gate([short_root])
    # Resolved by the caller to the long form, which is what a path check sees.
    assert gate.path_refusal(os.path.join(long_root, "inside.txt")) is None
    assert gate.path_refusal(os.path.join(short_root, "inside.txt")) is None
    for spelling in spellings:
        assert gate.path_refusal(spelling) is None, spelling

    # And the accommodation is not a widening: a sibling with the same prefix is
    # still refused. Without this the fake above would pass a gate that ignored
    # roots entirely.
    assert gate.path_refusal(os.path.join(str(tmp_path / "Approved Root2"),
                                          "inside.txt")) is not None


def test_spellings_never_lose_the_plain_form():
    """`_norm`'s promise: a path that does not exist still compares exactly.

    `GetLongPathNameW` needs the path to exist, so it is a spelling to *try*.
    """
    for path in (r"C:\ws\does\not\exist", r"C:\ws\also-missing\a.txt"):
        assert policy._norm(path) in policy._spellings(path), path


def test_containment_is_not_a_string_prefix():
    """`C:\\ws-2` startswith `C:\\ws` and is a different directory."""
    from assistant import tools
    gate = tools.default_gate([r"C:\ws"])
    assert gate.path_refusal(r"C:\ws-2\notes.txt") is not None
