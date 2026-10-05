"""Delegation: a unit of work with a life, rather than a command that ran once.

`shell.run` already runs an argv in an approved workspace, returns its output
and forgets it. That is the right shape for a command and the wrong shape for a
*task*, because a task has a beginning and an end and needs to be *asked about*
afterwards: what is still running, what did that one produce, is its claim true.

So this module is the difference between the two. A task gets an id, a budget,
a status, and a place in a bounded registry, and it survives being asked about.

Three decisions worth stating, because each is a way to get this wrong in a way
that only shows up as "the assistant ate my VRAM" or "it killed my terminal":

**A worker cannot ask for VRAM, because there is no argument to ask with.**
D1 says models take turns, and the arbiter is the only thing that grants a lease.
An argument like `vram_gib=6` on this tool would be a promise with nothing behind
it: the value would be recorded in a task row and no lease would exist. The
arbiter already carries `Priority.WORKER` for exactly this role and nothing
acquires it yet, so when a worker does need a model the honest door is
`Arbiter.acquire(holder, need_bytes, Priority.WORKER)` - and it will be refused
if the card cannot spare the room. Absence of the argument is the guarantee; a
rule the caller can satisfy with a number is not a rule (D49).

**The own-PID chain is protected by PID, once, here.** The first version of this
walk lived in `daemon.py` for `app.close`, which matches by image name and on
this machine matches the daemon itself (`python.exe`, plus the shell that started
it). A worker adds a second way to reach that process - stop the task you are
standing in - so the walk moves here and `daemon._admin_own_chain` calls it. Two
copies of a rule is how one of them ends up not having it (D58).

**Refusals here return, they do not raise.** `_preview_for` in the daemon has no
exception handling, so a preview builder that raises takes the whole confirmation
down with it and the user sees a traceback instead of a dialog (D59). Every
`*_refusal` in this module returns `str | None` for exactly that reason, and the
executor asks the *same* question the preview asked - which is what makes the
approved payload and the executed payload the same thing (D32).

Nothing here calls a model, and nothing here reads the network.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from . import policy

#: A task that outlives this is not a task, it is a daemon the user did not ask
#: for. Bounded, and the oldest *finished* task is dropped first - a running task
#: is never dropped, because dropping it would leave a process nobody owns.
MAX_TASKS = 16

DEFAULT_BUDGET_S = 120.0
MAX_BUDGET_S = 900.0

#: Matches `shell.run`'s argv cap so the two tools cannot disagree about how big
#: a command line is allowed to be.
MAX_ARGV_CHARS = 4096
OUTPUT_CAP = 4000

#: Grammar, not sanitising. The id becomes a filename and a dict key; it is not
#: passed to a shell, so the risk is traversal and collision rather than
#: injection - and both are answered by refusing rather than by rewriting
#: (D49: a rule the model cannot express is not a rule).
TASK_ID_OK = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

#: `pause` is deliberately absent. Windows has no portable pause for a console
#: process, and offering an action that cannot be honoured is how a tool learns
#: to report that it did something it did not do.
INTERVENE_ACTIONS = ("stop", "restart")

TASK_STATUSES = ("running", "done", "failed", "stopped")


# --------------------------------------------------------------- refusals


def task_id_refusal(raw: Any) -> str | None:
    """Is this an id we can file a task under?"""
    text = str(raw or "").strip()
    if not text:
        return ("a task needs an id, so it can be asked about later. Use "
                "short lowercase: letters, digits, dash and underscore, up "
                "to 32 characters")
    if not TASK_ID_OK.match(text):
        return (f"{text!r} is not a usable task id. Use short lowercase: "
                "letters, digits, dash and underscore, up to 32 characters, "
                "starting with a letter or digit")
    return None


def goal_refusal(raw: Any) -> str | None:
    """A task with no stated goal cannot be reviewed, so it is refused."""
    text = str(raw or "").strip()
    if not text:
        return ("a task needs a goal in one line. It is what 'delegate.review' "
                "judges the output against, so a task without one produces "
                "output nobody can check")
    if len(text) > 500:
        return ("the goal is 500 characters or fewer - it is a statement of "
                f"aim, not the plan. This one is {len(text)}")
    return None


def budget_refusal(value: Any) -> str | None:
    """Seconds of wall clock. Bounds are the tool's, not the caller's.

    Checked for type before range, because `int(1.5)` is 1: a fractional budget
    would be reported as "too short" when the real fault is that it is not a
    number of seconds at all. That is D60's lesson one level down - send the user
    at the size of the mistake instead of its type.
    """
    if value is None:
        return None                      # the caller takes the default
    if isinstance(value, bool):
        return ("budget_s is a number of seconds, not true/false")
    if isinstance(value, float) and not value.is_integer():
        return (f"budget_s must be a whole number of seconds; {value} is not "
                f"one. It arrived as a float, so the caller meant something "
                "fractional")
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return f"budget_s must be a number of seconds, not {value!r}"
    if seconds < 1:
        return (f"a budget of {seconds}s cannot run a process that still has "
                "to start. One second is the floor")
    if seconds > MAX_BUDGET_S:
        return (f"{seconds}s is longer than this tool will hold a task, which "
                f"is {MAX_BUDGET_S:.0f}s. A longer job is a background service, "
                "and this is not one")
    return None


def argv_refusal(argv: Any) -> str | None:
    """Refuse anything that is not already a list of strings.

    A string is refused rather than split, exactly as `shell.run` refuses it:
    a command line that must be parsed to become a command line can become a
    different command line, and the user would be approving a guess.
    """
    if argv is None:
        return ("a task needs a command: a list, like "
                '["python", "-m", "pytest", "-q"]')
    if isinstance(argv, str):
        return ("give the command as a list, not a string: "
                f"{argv!r}. A string has to be split to be run, and what "
                "gets split can be a different command than the one written "
                "down. See 'shell.run' for why no shell is involved either")
    if not isinstance(argv, (list, tuple)) or not argv:
        return "the command must be a non-empty list"
    if not all(isinstance(part, str) for part in argv):
        bad = next(i for i, p in enumerate(argv) if not isinstance(p, str))
        return (f"command[{bad}] is not a string ({argv[bad]!r}). Arguments "
                "are passed to the program, not interpreted, so each one has "
                "to already be text")
    if any(not part for part in argv):
        bad = next(i for i, p in enumerate(argv) if not p)
        return (f"command[{bad}] is an empty string. An empty argument is "
                "usually a variable that came out unset, and passing it "
                "through hides that")
    if sum(len(p) for p in argv) > MAX_ARGV_CHARS:
        return (f"the command is {sum(len(p) for p in argv)} characters, over "
                f"the {MAX_ARGV_CHARS} limit. Write it as a script in the "
                "workspace and run that")
    return None


def interact_refusal(action: Any) -> str | None:
    """Only the two actions this tool can actually honour."""
    text = str(action or "").strip().lower()
    if not text:
        return (f"say what to do: one of {', '.join(INTERVENE_ACTIONS)}. "
                "'pause' is not offered because Windows has no portable pause "
                "for a console process, and an action that cannot be honoured "
                "is worse than one that is missing")
    if text not in INTERVENE_ACTIONS:
        return (f"{text!r} is not something this tool can do. It offers "
                f"{', '.join(INTERVENE_ACTIONS)}, and nothing else")
    return None


def _terminate_tree(pid: int) -> str | None:
    """Kill a pid and everything it started. Returns a reason, or None.

    Measured, and the measurement is the whole point: this project's venv is
    uv-built, so `subprocess.Popen` on `.../.venv/Scripts/python.exe` creates a
    *launcher* which in turn creates the real interpreter as a child. Six
    sleepers assigned by an earlier run of the probe were still alive, holding
    their working directories open, five minutes after they were stopped - and
    the task rows said `stopped`. `os.kill(pid)` reached the launcher, the
    launcher died, and the work it was doing carried on without it.

    So a stop that kills one PID and reports success is a lie with a status
    attached: the tool says it stopped the work, the work does not stop, and the
    only symptom is a directory that will not delete. `taskkill /T` is the tree
    version on Windows; elsewhere a process group, because the same shape
    appears whenever a command starts something of its own.
    """
    if os.name == "nt":
        try:
            done = subprocess.run(                        # noqa: S603
                ["taskkill", "/F", "/T", "/PID", str(pid)],   # noqa: S607
                capture_output=True, text=True, timeout=15,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError) as why:
            return f"taskkill failed for pid {pid}: {why}"
        if done.returncode != 0:
            detail = (done.stderr or done.stdout or "").strip()
            return (f"taskkill could not stop the tree rooted at pid {pid}: "
                    f"{detail or 'no reason given'}")
        return None
    try:
        os.killpg(os.getpgid(pid), 15)
    except (OSError, AttributeError, ProcessLookupError) as why:
        return f"could not stop the process group for pid {pid}: {why}"
    return None


def own_chain() -> set[int]:
    """This process and every ancestor of it, by PID.

    Twice the hazard `shell.run`'s sibling `app.close` already had, and the same
    walk: a task's process is a child of the daemon, so *stopping a task* is a
    command that can name the daemon itself. Measured on this machine: asking to
    close `python.exe` produced eight matches, the list including the daemon and
    the shell that started it. Force-closing those ends the session mid-call,
    with no audit entry and no way to ask why.

    PID rather than name, deliberately. Name-protection would have to blacklist
    `python.exe` and `pwsh.exe` wholesale, which would make this tool unable to
    clean up the strays it is most often asked about.
    """
    chain: set[int] = set()
    pid = os.getpid()
    for _ in range(64):                  # a cycle would otherwise hang here
        if pid <= 0 or pid in chain:
            break
        chain.add(pid)
        try:
            parent = os.getppid()
        except OSError:
            break
        if parent == pid:
            break
        pid = parent
    return chain


# --------------------------------------------------------------- the task


@dataclass
class Task:
    """One unit of delegated work, and everything that is true about it.

    Mutated in place by the worker thread, and read by whoever asks. The status
    is a small closed set on purpose: a status field with nine possible values
    and no reader is how "in_flight_but_also_failed" happens.
    """

    id: str
    goal: str
    root: str
    argv: tuple[str, ...]
    budget_s: float
    created_at: float = field(default_factory=time.time)
    status: str = "running"
    pid: int | None = None
    started_at: float | None = None
    finished_at: float | None = None
    exit_code: int | None = None
    timed_out: bool = False
    stopped: bool = False
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False
    seconds: float | None = None
    # What the user concluded about it, recorded by `delegate.accept`. Held on
    # the task rather than only in the audit log so that `delegate.review` can
    # show the verdict beside the evidence it was made on - a verdict stored
    # somewhere the reader has to go looking for is a verdict nobody reads. It
    # deliberately cannot alter `status` or `exit_code`: a person may well
    # accept a task that failed because the failure was the answer, and a
    # record that could overwrite the machine's own answer could not be trusted
    # to describe it.
    accepted: str | None = None
    accept_reason: str = ""

    def row(self) -> dict[str, Any]:
        """The public shape. Output is summarised here, not inlined.

        A 4000-character stdout in a status listing turns a one-line answer into
        a wall, and `delegate.review` is the tool that hands over the output in
        full. So the listing carries the measurement and the review carries the
        text - which is the same split as `preview_truncated` everywhere else.
        """
        return {
            "id": self.id,
            "goal": self.goal,
            "workspace": self.root,
            "command": list(self.argv),
            "command_line": subprocess.list2cmdline(self.argv),
            "budget_s": self.budget_s,
            "status": self.status,
            "pid": self.pid,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "seconds": self.seconds,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "stopped_by_us": self.stopped,
            "stdout_chars": len(self.stdout),
            "stderr_chars": len(self.stderr),
            "output_truncated": self.truncated,
            "finished": self.status in ("done", "failed", "stopped"),
            "verdict": self.accepted,
            "verdict_reason": self.accept_reason or None,
        }

    def result(self, *, output_chars: int = OUTPUT_CAP) -> dict[str, Any]:
        """The full row plus the output itself, for the tools that read it."""
        return {
            **self.row(),
            "stdout": self.stdout[:output_chars],
            "stderr": self.stderr[:output_chars],
            "ok": self.status == "done",
            "note": self._result_note(),
        }

    def _result_note(self) -> str:
        if self.status == "stopped":
            return ("the task was stopped before it finished, so its exit code "
                    "is the one the operating system gave a killed process and "
                    "says nothing about whether the work would have succeeded")
        if self.timed_out:
            return (f"the task ran past its {self.budget_s:.0f}s budget and was "
                    "killed. Raise 'budget_s' if it needed longer - a task that "
                    "is killed on a timer is not a task that failed")
        if self.status == "running":
            return ("still running. 'delegate.status' for the short row, or "
                    "ask again in a moment")
        if self.status == "failed":
            return (f"exit code {self.exit_code}. The output below is the "
                    "program's, not ours - read stderr first")
        return "finished"


# --------------------------------------------------------------- the registry


class Registry:
    """The in-flight work, and the only thing allowed to stop a process.

    Bounded by `MAX_TASKS`, and the eviction order is the load-bearing part:
    a *finished* task is dropped before a running one. Evicting a running task
    to make room would leave a process with no owner and no way to stop it,
    which is the same shape of problem as the presence monitor's `_held` bug in
    Phase 2 - a state that updates on the happy path only.

    Thread-safe, because workers finish on their own threads and the daemon
    serves reads from another.
    """

    def __init__(self, *, max_tasks: int = MAX_TASKS,
                 runner: Callable[[Task], None] | None = None) -> None:
        self._tasks: dict[str, Task] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._max = max_tasks
        self._runner = runner or self._run_blocking

    # -- the registry ------------------------------------------------------

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            return self._tasks.get(str(task_id))

    def ids(self) -> list[str]:
        with self._lock:
            return list(self._order)

    def capacity(self) -> int:
        """How many tasks this registry will hold.

        A method rather than a constant because the bound is per-registry: the
        daemon may be given a smaller one in a test or a larger one by whoever
        builds it, and a `status` that reported `MAX_TASKS` instead of the
        number actually in force would be reporting the plan rather than the
        machine.
        """
        return int(self._max)

    def rows(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._tasks[i].row() for i in self._order if i in self._tasks]

    def evicted(self) -> str | None:
        """The id dropped to make room, or None. Reported, never silent."""
        return getattr(self, "_last_evicted", None)

    def register(self, task: Task) -> None:
        with self._lock:
            if task.id not in self._tasks:
                self._order.append(task.id)
            self._tasks[task.id] = task
            self._evict_locked()

    def _evict_locked(self) -> None:
        self._last_evicted = None
        while len(self._order) > self._max:
            victim = next((i for i in self._order
                           if self._tasks[i].status != "running"), None)
            if victim is None:
                break
            self._order.remove(victim)
            self._tasks.pop(victim, None)
            self._last_evicted = victim

    # -- lifecycle ---------------------------------------------------------

    def start(self, task: Task) -> None:
        """Register and run on a thread. Returns immediately.

        The thread is what makes this delegation rather than `shell.run`: the
        daemon keeps serving, so the user can ask about this task while it is
        still going. A thread per task rather than a pool, because the count is
        already bounded by `MAX_TASKS` and a pool would add a second thing to
        reason about for no gain.
        """
        self.register(task)
        thread = threading.Thread(
            target=self._run_safely, args=(task,),
            name=f"delegate-{task.id}", daemon=True)
        thread.start()

    def _run_safely(self, task: Task) -> None:
        try:
            self._runner(task)
        except Exception as why:                     # noqa: BLE001
            # A worker's death must not be silent, and must not take the daemon
            # with it. The task records the failure rather than disappearing,
            # because a task that vanishes from the registry with no row is
            # indistinguishable from one that was never assigned.
            task.status = "failed"
            task.finished_at = time.time()
            task.stderr = f"the worker itself failed: {why!r}"

    def _run_blocking(self, task: Task) -> None:
        """Own runner. Popen rather than `sysinfo.run_capture`.

        Reimplementing a capture is the D58 mistake, so this is deliberate and
        the reason is a capability `run_capture` does not have: it cannot be
        stopped. `subprocess.run` hands back only after the process ends, so a
        task that has to be interruptible cannot be built on it, and
        `delegate.intervene` would be a tool that names an action it cannot
        perform. The *result shape* is kept identical to `run_capture`'s so a
        caller cannot tell which of the two produced it.
        """
        task.started_at = time.time()
        if task.stopped:
            # Asked to stop before it got here. Spawning anyway would mean a
            # process that exists and has to be killed, which is a worse thing
            # to do than not starting it: the whole point of a stop is that the
            # command does not run.
            task.status = "stopped"
            task.finished_at = time.time()
            task.seconds = 0.0
            return
        try:
            proc = subprocess.Popen(                 # noqa: S603
                list(task.argv), cwd=task.root,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL)
        except OSError as why:
            task.status = "failed"
            task.finished_at = time.time()
            task.stderr = f"could not start {task.argv[0]!r}: {why}"
            return
        task.pid = proc.pid
        # Recorded as soon as it is known, so a `delegate.status` during the
        # run reports the pid that exists rather than null.
        #
        # And then checked again, immediately, because the guard above has a
        # window in it: between that `if` and here, this thread was inside
        # `Popen`. A stop arriving in that window sets the flag, reports "asked
        # to stop before it got as far as a process", and leaves nobody to kill
        # the process that now exists. That was measured - one launcher/child
        # pair per test run survived a drain that correctly stopped every task
        # it could see, holding its working directory open long after the test
        # had reported itself green.
        #
        # Checking again here closes the window rather than narrowing it: the
        # flag may now land at any moment up to the pid existing, and wherever
        # it lands the process dies. "Stopped" stops meaning "we believe so".
        if task.stopped:
            _terminate_tree(task.pid)
            try:
                proc.communicate(timeout=10.0)
            except subprocess.TimeoutExpired:      # pragma: no cover - a
                pass                             # kill that did not take
            task.status = "stopped"
            task.finished_at = time.time()
            task.seconds = round(task.finished_at - task.started_at, 3)
            return
        try:
            out, err = proc.communicate(timeout=task.budget_s)
            task.exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            task.timed_out = True
            proc.kill()
            out, err = proc.communicate()
            task.exit_code = proc.returncode
        task.stdout = (out or b"").decode("utf-8", "replace")
        task.stderr = (err or b"").decode("utf-8", "replace")
        if len(task.stdout) > OUTPUT_CAP or len(task.stderr) > OUTPUT_CAP:
            task.truncated = True
            task.stdout = task.stdout[:OUTPUT_CAP]
            task.stderr = task.stderr[:OUTPUT_CAP]
        task.seconds = time.time() - task.started_at
        task.finished_at = time.time()
        if task.stopped:
            task.status = "stopped"
        elif task.timed_out:
            task.status = "failed"
        else:
            task.status = "done" if task.exit_code == 0 else "failed"

    # -- intervention ------------------------------------------------------

    def stop(self, task_id: str) -> tuple[bool, str]:
        """Kill a running task. Returns `(stopped, reason)`.

        Refuses the daemon's own process chain by PID, for the reason in
        `own_chain`. It reports the refusal rather than raising, because the
        caller is a preview builder on the T2 path and a raised exception there
        takes the confirmation down with it.
        """
        task = self.get(task_id)
        if task is None:
            return False, f"no task named {task_id!r}"
        if task.status != "running":
            return False, (f"task {task_id!r} is {task.status}, not running - "
                           "there is nothing to stop")
        if task.pid is None:
            # Not a refusal. There is a window between `start` returning and
            # `Popen` completing, and refusing here meant a task assigned one
            # millisecond ago could not be stopped at all - with a reason that
            # reads as though it always will be. Asking a task to stop before it
            # has a process is the same request, and it can be honoured by never
            # starting: `_run_blocking` checks this flag and exits without
            # spawning. Honoured means honoured, not refused with a reason.
            task.stopped = True
            return True, (f"task {task_id!r} was asked to stop before it got "
                          "as far as a process; nothing will be run")
        if task.pid in own_chain():
            return False, (f"task {task_id!r} is this assistant, or something it "
                           "was started from - not stopping it")
        # Set the flag *before* the kill, not after. This is a race, and it was
        # measured: the worker thread wakes the instant the process dies and
        # computes the terminal status from `task.stopped`, so a flag set after
        # `taskkill` returns arrives too late to be seen. The task then reports
        # `failed` with exit code 1 - the code Windows gives a process it
        # killed - which is the one reading that says the work broke rather
        # than that it was stopped. Both outcomes are true; only one of them is
        # what happened, and the tool is the only thing that knows which.
        task.stopped = True
        try:
            why = _terminate_tree(task.pid)
        except (OSError, AttributeError, ProcessLookupError) as exc:
            task.stopped = False
            return False, f"could not stop pid {task.pid}: {exc}"
        if why:
            task.stopped = False
            return False, why
        # The thread sets the terminal status once it reaps. Claiming `stopped`
        # here would be a lie about something not yet true - the process has
        # been asked, not confirmed dead.
        return True, (f"the process tree rooted at pid {task.pid} was killed; "
                      "its own children were killed with it, because a "
                      "launcher dying is not the work stopping")

    def wait(self, task_id: str, timeout: float) -> bool:
        """Block until the task is not running. Used by tests and `review`."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            task = self.get(task_id)
            if task is None or task.status != "running":
                return True
            time.sleep(0.02)
        task = self.get(task_id)
        return task is None or task.status != "running"

    def reset(self) -> None:
        """Test hook. Drops every task; kills nothing."""
        with self._lock:
            self._tasks.clear()
            self._order.clear()
            self._last_evicted = None


# ------------------------------------------------------------- workspace facts


def workspace_briefing(root: str, *, limit: int = 40) -> dict[str, Any]:
    """What the daemon can say about a workspace, read off the disk.

    Not a summary of the code - the brain reads the code. This is the set of
    facts that decide whether a delegated task is even possible, and that a
    model asked to "plan the work" would otherwise have to guess: does the
    directory exist, is it a git repository, is there a test suite to run, is
    there a README, how big is the thing.

    Every field is measured. A briefing that says "tests: unknown" where it
    could have said "tests: pytest.ini, tests/" is worse than no briefing,
    because the model fills the gap with training data and D10 is enforced by
    refusing, not by hoping.
    """
    import pathlib

    base = pathlib.Path(root)
    out: dict[str, Any] = {
        "workspace": str(base),
        "exists": base.is_dir(),
    }
    if not out["exists"]:
        out["note"] = "this workspace is not a directory, so there is nothing to brief on"
        return out
    try:
        entries = sorted(p.name for p in base.iterdir())
    except OSError as why:
        out["note"] = f"could not list it: {why}"
        return out
    directories = [n for n in entries if (base / n).is_dir()]
    files = [n for n in entries if (base / n).is_file()]
    out["top_level"] = {"directories": directories[:limit], "files": files[:limit]}
    out["truncated"] = len(directories) > limit or len(files) > limit
    out["is_git_repo"] = (base / ".git").exists()
    out["has_readme"] = any(
        f.lower().startswith("readme") for f in files)
    # The three test conventions worth naming, in the order a maintainer would.
    marks = {
        "pytest": (base / "pytest.ini").exists() or (base / "pyproject.toml").exists(),
        "tox": (base / "tox.ini").exists(),
        "npm": (base / "package.json").exists(),
    }
    out["test_markers"] = [k for k, v in marks.items() if v]
    out["test_dirs"] = [d for d in directories
                        if d.lower() in ("tests", "test", "spec", "specs")]
    out["measurements"] = {
        "entries": len(directories) + len(files),
        "directories": len(directories),
        "files": len(files),
    }
    return out


def constraints_for(root: str, allowed_roots: Sequence[str],
                    deny_paths: Sequence[str] = (),
                    self_paths: Sequence[str] = ()) -> dict[str, Any]:
    """The rules that will refuse a plan for this workspace, stated up front.

    `delegate.brainstorm` exists because a plan that the tool layer will refuse
    is worse than no plan: the work is designed, approved, and then stopped at
    the gate. So the briefing carries the constraints that bind *here*, read
    from the same gate the executor will ask, rather than from a copy in a
    prompt.
    """
    import pathlib

    here = pathlib.Path(root)
    base: dict[str, Any] = {
        "workspace": str(here),
        "vram": ("models take turns and there is a hard ceiling. A task that "
                 "needs a model takes its turn like anything else - it is not "
                 "given the card to itself"),
        "self_edit": ("the assistant's own source and its decision records are "
                      "protected. A task may not modify them, at any tier, by "
                      "any route - that is enforced by the path check, not by "
                      "asking nicely"),
    }
    inside = [r for r in allowed_roots if policy.contains(here, r)]
    base["approved"] = bool(inside)
    if inside:
        base["approved_by"] = inside
    else:
        base["approved_by"] = None
        base["note"] = (
            "this workspace is not among the approved roots, so every tool "
            "call that touches a path in it will be refused. Approved roots: "
            + (", ".join(str(r) for r in list(allowed_roots)[:8]) or "none"))
    denied = [d for d in deny_paths if policy.contains(here, d)]
    if denied:
        base["inside_a_protected_path"] = denied
    guarded = [s for s in self_paths if policy.contains(here, s)]
    if guarded:
        base["contains_protected_files"] = guarded
    return base


def _under(candidate: "pathlib.Path", root: "pathlib.Path") -> bool:
    """Kept because the tests reach for it, but it no longer decides anything.

    The containment rule now lives in `policy.contains`, once. When this copy
    was the live one, it compared one spelling per side, and the manifest
    stores the 8.3 short form of a path that `Path.resolve()` expands to the
    long one - so it answered "this approved workspace is not approved" while
    printing the workspace as the approved root, and every briefing the model
    reads before planning anything carried that lie.
    """
    return policy.contains(candidate, root)
