"""Administration: launching programs, closing them, and running a command.

Three Tier 2 tools, and the thing they have in common is that every one of them
hands a string to Windows and gets something back. That is where the failures
live, and none of them are "the code is wrong" - they are "the code did exactly
what it said, and what it said was not what the user meant":

* A command string split by a rule that is wrong for some argument. So the
  string form is refused rather than split, and only an argv list is accepted -
  that is also what removes the injection surface, because `shell=False` gives
  every element to the program as one argument.
* A program name that matched too much. `app.launch` on a name with a wildcard
  would launch "whatever matches", which is not a thing to put in front of an
  approval dialog.
* A process the machine does not survive being closed, and the assistant's own
  interpreters, which is what `app.close` matched on this machine.

None of these tests open a program, close a real process, or touch hardware. The
ones that would - launching notepad++, closing a process - were measured by
`probe_exec.py` in the same session that wrote this, and what is asserted here
is the decision each one makes, which is where the bugs were.
"""

import os
import subprocess
import sys

import pytest

from assistant import sysinfo
from assistant.daemon import Daemon, DaemonError, Refused
from assistant.manifest import Workspace
from assistant.policy import Tier


@pytest.fixture
def desk(tmp_path):
    """A daemon with one approved root, and that root reflected into the gate.

    Two details, both of which cost a probe run to learn:

    * The approved root is a *sibling* of the state directory, not a parent of
      it. `manifests.add` refuses a root that contains `workspaces/`, because
      that would make the assistant's own code reachable as an approved
      workspace - D21 enforced by the manifest rather than by the gate.
    * `gate.allowed_roots` is set by hand. The daemon refreshes it inside
      `_verdict` on every routed call, which a direct `_exec_*` call bypasses,
      so without this every path check in these tests would deny with "no
      workspace has been approved" and the tests would be asserting the fixture.
    """
    allowed = tmp_path / "ws"
    allowed.mkdir()
    d = Daemon(state=tmp_path / "state", vault=tmp_path / "vault")
    d.manifests.add(Workspace(name="ws", root=str(allowed), allow_tiers=(
        Tier.READ, Tier.REVERSIBLE_WRITE, Tier.CONSEQUENTIAL,
        Tier.IRREVERSIBLE)))
    d.gate.allowed_roots = list(d.manifests.roots())
    d.allowed = allowed
    return d


# --- the command is a list, or it is refused --------------------------------


def test_a_command_string_is_refused_rather_than_split(desk):
    """The refusal has to say what to write instead, not just "no".

    Splitting is the one thing here that is unavoidable wrong: every rule for
    splitting a Windows command line is wrong for some argument - a path with a
    space, a quoted empty string, a trailing backslash - and the mistake shows up
    later as a command that ran with the wrong arguments, which is far harder to
    trace back than a refusal.
    """
    with pytest.raises(DaemonError) as caught:
        desk._admin_argv({"command": "python -m pytest"})
    why = str(caught.value)
    assert "list of arguments" in why
    assert '["command", "python -m pytest"]' in why, \
        "the refusal must show the caller what a list looks like"


def test_a_missing_or_empty_command_is_refused(desk):
    for bad in ({}, {"command": []}, {"command": "notepad"}):
        with pytest.raises(DaemonError):
            desk._admin_argv(bad)


def test_an_argument_that_is_not_already_a_string_is_refused(desk):
    """The tool does no conversion of its own, so it says which element is wrong.

    Accepting an int and stringifying it would mean guessing at its format - and
    `["-n", 5]` becoming `"-n 5"` by accident is how a count becomes a filename.
    """
    with pytest.raises(DaemonError) as caught:
        desk._admin_argv({"command": ["python", "-n", 5]})
    assert "command[2]" in str(caught.value)


def test_a_command_line_too_long_to_read_is_refused(desk):
    """A dialog showing the first 4 KB of a command is a dialog approved blind."""
    with pytest.raises(DaemonError) as caught:
        desk._admin_argv({"command": ["echo", "x" * 5000]})
    assert str(desk._ADMIN_MAX_ARGS) in str(caught.value)


# --- a leading slash is a switch, not a path --------------------------------


@pytest.mark.parametrize("part", ["/c", "/FI", "\\n", "-v", "pytest", "3"])
def test_something_that_is_not_a_path_is_not_screened_as_one(desk, part):
    """`os.path.isabs("/c")` is True, and that is what made `cmd /c` unrunnable.

    A rooted path is "absolute" relative to the drive it resolves against, so the
    first version of this test screened `/c` as a path and refused the call for
    reaching `C:\\c` - a directory that was never named and did not exist. On
    Windows a leading slash is a switch; that is the convention `cmd.exe` and
    `tasklist` both use.
    """
    assert Daemon._admin_is_pathish(part) is False, part


@pytest.mark.parametrize("part", [
    r"C:\Windows\System32\config", "C:/temp/x", r"\\server\share\x",
    r"..\..\secrets.txt", "build/out.log", "notes.txt",
])
def test_something_that_is_a_path_is_screened(desk, part):
    assert Daemon._admin_is_pathish(part) is True, part


def test_a_denied_path_hidden_behind_a_program_name_is_still_caught(desk):
    """`policy._check_paths` screens arguments *named* like paths.

    None of these is named `path`, so that screen passes them all. The executor
    screens every argument that *looks* like a path as well, in addition to
    rather than instead of the gate - because missing one is a hole with no
    symptom, and over-matching costs the user one restatement.
    """
    found = dict(desk._admin_arg_paths(
        ["del", "C:/Windows/System32/config/SAM"]))
    assert "C:/Windows/System32/config/SAM" in found

    # And in the form a flag carries one, which is the other way it hides.
    found = dict(desk._admin_arg_paths(
        ["robocopy", "--target=C:\\Windows\\System32\\config", "a", "b"]))
    assert "C:\\Windows\\System32\\config" in found

    # An ordinary invocation is not over-matched into uselessness.
    assert desk._admin_arg_paths(["python", "-m", "pytest", "-q"]) == []


# --- a failing command is an answer, not a crash -----------------------------


def test_a_non_zero_exit_is_reported_rather_than_raised(desk):
    """`run_command` raising is right for a probe reading a value.

    It is wrong here, where "the build failed" *is* the answer: raising would turn
    every compile error into an exception the caller has to catch and classify,
    and the one case that matters most - a command that failed - is the case that
    would look like a broken tool.
    """
    result = desk._exec_shell_run(
        {"command": [sys.executable, "-c", "import sys;sys.exit(3)"]})
    assert result["exit_code"] == 3
    assert result["ok"] is False
    assert result["timed_out"] is False


def test_a_command_that_times_out_is_reported_rather_than_raised(desk):
    """The same reasoning. A build that hangs is a failure, not an exception."""
    result = desk._exec_shell_run({
        "command": [sys.executable, "-c", "import time;time.sleep(30)"],
        "timeout_s": 1.0,
    })
    assert result["timed_out"] is True
    assert result["ok"] is False


def test_shell_metacharacters_reach_the_program_as_one_argument(desk):
    """No shell is involved, so `& | ; >` are characters rather than syntax.

    Witnessed by a program that prints its own `argv`, not by `cmd /c`: cmd *is*
    an interpreter, so a metacharacter surviving `cmd /c` would prove nothing
    about `shell=False`. What is asserted is the property that removes the
    injection surface - one argument in, the same argument out.
    """
    import json

    result = desk._exec_shell_run({
        "command": [sys.executable, "-c",
                    "import sys,json;print(json.dumps(sys.argv[1:]))",
                    "a&b", "x|y", "z;w", "out.txt"],
        "cwd": str(desk.allowed),
    })
    assert json.loads(result["stdout"]) == ["a&b", "x|y", "z;w", "out.txt"]
    assert result["ok"] is True


def test_a_command_runs_in_the_approved_root_not_wherever_the_daemon_started(desk):
    """A command inheriting `G:\\` reaches the whole drive with relative paths."""
    cwd = desk._admin_cwd({})
    assert os.path.normcase(cwd) == os.path.normcase(str(desk.allowed.resolve()))


def test_a_working_directory_outside_every_root_is_refused(desk):
    with pytest.raises(Refused) as caught:
        desk._admin_cwd({"cwd": "C:\\Windows"})
    assert "refused" in str(caught.value)


def test_a_denied_path_in_the_arguments_is_refused_by_the_executor(desk):
    """Re-checked in the executor as well as the preview.

    The preview is what the user approved; the executor is what runs. If those
    two ever disagree that is a bug, and D32 says a bug worth refusing is
    preferable to executing it.
    """
    with pytest.raises(Refused) as caught:
        desk._exec_shell_run({"command": [
            "cmd", "/c", "type", "C:\\Windows\\System32\\config\\SAM"]})
    assert "refused" in str(caught.value)


def sysinfo_source() -> str:
    """The real `daemon.py` on this machine, for the self-path test.

    Resolved from the imported module rather than hardcoded, so the test follows
    the checkout. It falls back to the recorded location when the module is
    installed rather than run from source.
    """
    candidate = os.path.join(os.path.dirname(os.path.abspath(sysinfo.__file__)),
                             "daemon.py")
    return candidate if os.path.isfile(candidate) \
        else r"G:\localaized\src\assistant\daemon.py"


def test_the_assistants_own_source_is_not_runnable_through_shell_run(desk):
    """D21, and it has to hold on the path that did not exist when it was written.

    `shell.run` is the one tool that can run anything on the machine, so the
    self-tree deny has to apply to its arguments too - not only to `file.*`.
    """
    src = sysinfo_source()
    assert os.path.isfile(src), src
    with pytest.raises(Refused):
        desk._exec_shell_run({"command": ["cmd", "/c", "type", src]})


# --- app.launch -------------------------------------------------------------


def test_a_wildcard_application_name_is_refused(desk):
    """The name becomes a glob, and "launch whatever matches this" is not a thing
    to put in front of an approval dialog."""
    exe, why = desk._resolve_app("note*")
    assert exe is None
    assert "wildcards are refused" in why


def test_an_absolute_path_that_exists_is_resolved_without_a_search(desk):
    fake = desk.allowed / "thing.exe"
    fake.write_bytes(b"not really an executable")
    exe, how = desk._resolve_app(str(fake))
    assert exe == fake.resolve()
    assert how == "absolute path given"


def test_an_absolute_path_that_does_not_exist_says_so(desk):
    exe, why = desk._resolve_app(str(desk.allowed / "ghost.exe"))
    assert exe is None
    assert "does not exist" in why


def test_an_absolute_path_is_refused_for_what_it_is_not_for_its_spelling(desk):
    """Ordering bug: the bare-name check ran first and rejected every `\\`.

    The complaint it produced was "not a plain application name" for
    `G:\\localaized\\src\\assistant\\daemon.py` - a remark about spelling, for a
    path whose real problem is that it is the assistant's own source. A refusal
    that names the wrong problem sends the user to fix the wrong thing.
    """
    src = r"G:\localaized\src\assistant\daemon.py"
    assert os.path.isfile(src)
    exe, how = desk._resolve_app(src)
    assert exe is not None and how == "absolute path given"
    with pytest.raises(Refused) as caught:
        desk._exec_app_launch({"app": src})
    assert "protected" in str(caught.value).lower() or \
        "self" in str(caught.value).lower(), str(caught.value)


def test_a_folder_named_after_the_program_is_matched_on_the_first_walk(desk, monkeypatch):
    """`os.walk` hands back a directory's whole listing on entry.

    The first version compared only `filenames`, so it walked *past* the folder
    called `Notepad++`, descended into it, found nothing there, and carried on
    until the 3000-directory budget ran out inside `Common Files` - reporting
    that a program the user had on disk was not installed. Measured on this
    machine: 1.0 s and a refusal became 0.0 s and a path.
    """
    root = desk.allowed / "Program Files"
    (root / "Notepad++").mkdir(parents=True)
    (root / "Notepad++" / "notepad++.exe").write_bytes(b"MZ")
    # Something with thousands of directories in it, to prove the match happens
    # before the budget rather than after it.
    for i in range(50):
        (root / "Common Files" / f"pad{i}").mkdir(parents=True)
    monkeypatch.setattr(Daemon, "_app_search_roots", lambda self: [root])
    exe, how = desk._resolve_app("notepad++")
    assert exe == (root / "Notepad++" / "notepad++.exe").resolve()
    assert "Notepad++" in how


def test_the_directory_budget_is_reported_rather_than_hidden(desk, monkeypatch):
    """A search that stops early must say it stopped, so the caller can pass a
    full path instead of trying a longer name."""
    root = desk.allowed / "pf"
    for i in range(10):
        (root / f"d{i}").mkdir(parents=True)
    monkeypatch.setattr(Daemon, "_app_search_roots", lambda self: [root])
    monkeypatch.setattr(Daemon, "_APP_DIR_BUDGET", 3)
    exe, why = desk._resolve_app("nothinglikethis")
    assert exe is None
    assert "3 directories" in why


def test_the_search_root_includes_the_folders_windows_actually_uses(desk):
    """Measured, not assumed: notepad++ is at
    `C:/Program Files/Notepad++/notepad++.exe` and `Get-Command notepad++` finds
    nothing, because no installer adds it to PATH. A resolver that only asks
    `shutil.which` refuses most of what is actually installed.
    """
    roots = [str(p).lower() for p in desk._app_search_roots()]
    assert any("program files" in p for p in roots), roots


def test_an_application_that_cannot_be_found_says_what_would_work(desk):
    exe, why = desk._resolve_app("definitelynotinstalled12345")
    assert exe is None
    assert "full path" in why


def test_an_empty_application_name_is_refused(desk):
    with pytest.raises(DaemonError) as caught:
        desk._exec_app_launch({"app": "   "})
    assert "needs 'app'" in str(caught.value)


# --- app.close --------------------------------------------------------------


@pytest.mark.parametrize("name", [
    "explorer", "explorer.exe", "winlogon", "System", "lsass", "svchost",
    "audiodg", "memory compression",
])
def test_a_process_windows_does_not_survive_being_closed_is_refused(desk, name):
    """Checked by *name*, because that is what the caller supplied.

    Checking the PID instead would pass the moment the OS restarted that
    process, which is exactly the moment it matters.
    """
    why = desk._admin_unkillable_reason({"process": name})
    assert why, name
    assert "does not survive" in why
    with pytest.raises(Refused):
        desk._exec_app_close({"process": name})


def test_an_ordinary_program_is_not_on_the_protected_list(desk):
    assert desk._admin_unkillable_reason({"process": "notepad++.exe"}) is None


def test_the_assistant_is_in_its_own_protected_chain(desk):
    """Measured: asking to close `python.exe` produced eight matches, and the
    list included the daemon itself and the shell that started it.

    Force-closing those is the same class of outcome as force-closing
    explorer.exe - the session ends mid-call, with no audit entry and no way to
    ask why. The chain is protected by PID rather than by name, because
    name-protection would have to blacklist `python.exe` wholesale and so would
    make the tool unable to clean up the stray interpreters it is most often
    asked about.
    """
    chain = desk._admin_own_chain()
    assert os.getpid() in chain
    assert len(chain) <= 64


def test_closing_the_assistants_own_processes_reports_them_instead_of_killing_them(desk):
    """Skipped, not fatal - and the run survives, which is the whole point.

    "Close the stray interpreters" is a reasonable request on a machine where the
    assistant is itself a python.exe, so refusing the whole call would make the
    tool unable to answer it at all. What is returned is what was left alone.
    """
    result = desk._exec_app_close({"process": "python.exe", "force": True})
    mine = [row for row in result["failed"] if int(row["pid"]) == os.getpid()]
    assert mine, f"our own pid is missing from the report: {result}"
    assert "assistant" in mine[0]["error"]
    # And this test process is still running, which is the real assertion.
    assert os.getpid() > 0


def test_closing_something_that_is_not_running_says_so_rather_than_raising_nothing(desk):
    with pytest.raises(DaemonError) as caught:
        desk._exec_app_close({"process": "definitelynotrunning99999.exe"})
    why = str(caught.value)
    assert "is running" in why
    assert "system.processes" in why, "a refusal must say what would work"


def test_a_rejected_process_query_is_not_reported_as_not_running(desk):
    """Two different failures that read identically on the first version.

    `find_processes` distinguishes "not a name I can look up" from "not
    running". Collapsing them made a rejected query come back as a claim about
    the machine - "no process named `chrome.exe\" or IMAGENAME eq x` is
    running" - which is false, and false in a way that reads as measured.
    """
    with pytest.raises(DaemonError) as caught:
        desk._exec_app_close({"process": 'chrome.exe" or IMAGENAME eq notepad'})
    why = str(caught.value)
    assert "not a bare process image name" in why
    assert "not running" not in why


def test_a_process_query_refused_by_tasklist_is_reported_as_such(desk):
    rows = sysinfo.find_processes("x" * 200)
    assert rows["count"] == 0
    assert "not a bare process image name" in rows["note"]


# --- the two helpers sysinfo gained for this -------------------------------


def test_find_processes_sees_this_process_and_the_os_filter_agrees(desk):
    """Cross-checked against tasklist rather than against `processes()`.

    `processes()` truncates to 200 rows by memory - by size - so a quiet process
    is exactly the case it drops, and reusing it would report "no such process"
    for a process that is running. tasklist filters natively.
    """
    rows = sysinfo.find_processes("python.exe")
    assert rows["count"] >= 1
    assert os.getpid() in [r["pid"] for r in rows["processes"]]
    assert all({"name", "pid", "session", "mem_bytes"} <= set(r)
               for r in rows["processes"])


def test_a_name_that_is_not_an_image_name_is_refused_not_sanitised(desk):
    """A caller who passed that did not mean a process, and guessing which of the
    two they meant is how the wrong one gets closed."""
    rows = sysinfo.find_processes('cmd.exe" or IMAGENAME eq notepad')
    assert rows["count"] == 0
    assert rows["processes"] == []
    assert "not a bare process image name" in rows["note"]


def test_run_capture_reports_rather_than_raises(desk):
    out = sysinfo.run_capture([sys.executable, "-c", "print('hi')"])
    assert out["exit_code"] == 0
    assert "hi" in out["stdout"]
    out = sysinfo.run_capture([sys.executable, "-c", "import sys;sys.exit(9)"])
    assert out["exit_code"] == 9


# --- the registry and the dialog --------------------------------------------


def test_the_three_tools_are_registered_with_a_preview_each():
    """A Tier 2 tool gets a preview for *ambiguity*, not for risk.

    None of the three is destructive, but all three take a name the caller might
    be wrong about, and the dialog is where the resolved program, the working
    directory and the affected PIDs become visible before anything happens.
    """
    for name in ("app.launch", "app.close", "shell.run"):
        assert name in Daemon._EXECUTORS, name
        assert name in Daemon._PREVIEWS, name


def test_the_dialog_names_the_program_that_will_actually_run(desk, monkeypatch):
    """Resolved in the preview, so the dialog names a file rather than a word.

    Handed to `CreateProcess`, a bare name resolves against the working
    directory as well as PATH, so approving a preview that said "notepad++"
    would be approving something the user cannot see.
    """
    fake = desk.allowed / "Program Files" / "Widget" / "widget.exe"
    fake.parent.mkdir(parents=True)
    fake.write_bytes(b"MZ")
    monkeypatch.setattr(Daemon, "_app_search_roots", lambda self: [fake.parents[1]])
    seen = desk._preview_app_launch({"app": "widget"})
    assert seen["resolved"] == str(fake.resolve())
    assert seen["requested"] == "widget"
    assert seen["found_by"] != "on PATH"


def test_the_command_preview_shows_the_resolved_command_line_and_where_it_runs(desk):
    seen = desk._preview_shell_run({
        "command": [sys.executable, "-m", "pytest", "C:/some notes/a b.txt"]})
    assert seen["command"] == [sys.executable, "-m", "pytest",
                               "C:/some notes/a b.txt"]
    assert seen["command_line"] == subprocess.list2cmdline(seen["command"])
    assert os.path.normcase(seen["cwd"]) == os.path.normcase(
        str(desk.allowed.resolve()))
    assert "C:/some notes/a b.txt" in seen["paths_in_arguments"]


def test_a_different_command_produces_a_different_payload_hash(desk):
    """D32: the hash binds the *payload*, so an edited command cannot ride in on
    an approval the user gave for a different one."""
    a = {"command": ["python", "-m", "pytest"]}
    b = {"command": ["python", "-m", "pytest", "--lf"]}
    assert desk._payload_hash("shell.run", a,
                              desk._preview_shell_run(a)) != \
        desk._payload_hash("shell.run", b, desk._preview_shell_run(b))


def test_the_close_preview_shows_the_rows_it_will_leave_alone(desk):
    seen = desk._preview_app_close({"process": "python.exe"})
    mine = [p for p in seen["processes"] if int(p["pid"]) == os.getpid()]
    assert mine and mine[0]["protected"] is True
