r"""The Tier 3 administration tools, tested without touching the machine.

These five - `registry.write`, `service.start_stop`, `package.install`,
`package.uninstall`, `power.shutdown` - are the highest-consequence tools in the
registry, so most of what is tested here is about *refusing*, and about the
promise that an action the tier calls irreversible is reversible in fact.

Two rules shape the tests.

**The registry half is real.** A registry write goes to
`HKCU\Software\ButleroyTest`, a key these tests create and delete, because the
only way to check that a value really landed *with the type it was asked for* is
to read it back with the same API that wrote it. A fake would be testing the
fake. Everything else - `sc`, `shutdown`, the package managers - is faked,
because the real thing there would stop a service or shut down this machine.

**The undo journal is the load-bearing claim.** It is checked by reading the
file back after the fact, and by making the action *fail* to prove a row is
still written: a journal that records only successes is empty exactly when it is
wanted.
"""

import json
import pathlib
import shlex
import subprocess
import tempfile
import winreg

import pytest

from assistant import sysinfo
from assistant.daemon import Daemon, DaemonError, Refused
from assistant.manifest import Workspace
from assistant.policy import Tier

TEST_HIVE = winreg.HKEY_CURRENT_USER
TEST_ROOT = r"Software\ButleroyTest"

TOOLS = ("registry.write", "service.start_stop", "package.install",
         "package.uninstall", "power.shutdown")

A_SERVICE = {"service": "Spooler", "exists": True, "state": "4",
             "state_name": "RUNNING", "running": True, "measured": True}


def scrub(*paths):
    """Delete a registry path, deepest first, ignoring what is not there."""
    for path in paths:
        try:
            with winreg.OpenKey(TEST_HIVE, path, 0,
                                winreg.KEY_ALL_ACCESS) as key:
                while True:
                    sub = winreg.EnumKey(key, 0)
                    scrub(path + "\\" + sub)
        except (FileNotFoundError, IndexError, OSError):
            pass
        try:
            winreg.DeleteKey(TEST_HIVE, path)
        except OSError:
            pass


# --------------------------------------------------------------- fixtures

@pytest.fixture
def desk():
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        # The approved root is a *sibling* of the state dir, not a child: a
        # root containing `workspaces/` is refused by manifests.add, because
        # that would make the daemon's own code reachable as a workspace.
        ws = root / "ws"
        ws.mkdir()
        d = Daemon(state=root / "state", vault=root / "vault")
        d.manifests.add(Workspace(
            name="ws", root=str(ws),
            allow_tiers=(Tier.READ, Tier.REVERSIBLE_WRITE, Tier.CONSEQUENTIAL,
                         Tier.IRREVERSIBLE)))
        # By hand: calling `_exec_*` directly bypasses `_verdict`, which is
        # where the daemon refreshes allowed_roots from the manifest.
        d.gate.allowed_roots = list(d.manifests.roots())
        yield d


@pytest.fixture(autouse=True)
def clean_registry():
    """Delete the test key before and after, so a failure cannot leak it."""
    scrub(TEST_ROOT)
    yield
    scrub(TEST_ROOT)


def journal(d):
    """Every row in the undo journal, parsed."""
    path = d._admin_undo_path()
    if not path.exists():
        return []
    return [json.loads(line) for line
            in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def fake_run(calls=None, exit_code=0, stdout="", stderr=""):
    """A `run_capture` stand-in that records every argv it was handed.

    The recorder is passed in rather than created here, so the test can read
    back what was actually run instead of asserting on a string it built.
    """
    def run(argv, *, timeout=120.0):
        if calls is not None:
            calls.setdefault("argv", []).append(list(argv))
        return {"exit_code": exit_code, "stdout": stdout, "stderr": stderr,
                "timed_out": False, "seconds": 0.0, "truncated": False}
    return run


def stub_manager(monkeypatch, *, name="winget.exe", stdout="", exit_code=0):
    """Point `manager_exe` at a path that need not exist, and answer queries."""
    monkeypatch.setattr(sysinfo, "manager_exe", lambda n: name)
    monkeypatch.setattr(sysinfo, "run_capture",
                        fake_run(stdout=stdout, exit_code=exit_code))


def stub_service(monkeypatch, name="Spooler", state=A_SERVICE):
    monkeypatch.setattr(sysinfo, "service_state",
                        lambda n: dict(state, service=n))


# --------------------------------------------------------- registration

def test_all_five_are_registered():
    """D5: a tool in the registry has an executor, and Tier 3 has a preview.

    `_check_tables` enforces the preview at import, but only for tools it can
    see - this asserts the five are actually wired rather than trusting that.
    """
    for tool in TOOLS:
        assert tool in Daemon._EXECUTORS, tool
        assert tool in Daemon._PREVIEWS, tool


def test_a_preview_never_raises_for_empty_arguments(desk):
    """`_preview_for` calls the builder with no exception handling at all.

    So a preview that raises on a missing argument takes the whole confirmation
    down with it, and the user sees a crash where there should be a question.
    """
    for tool in TOOLS:
        assert isinstance(desk._PREVIEWS[tool](desk, {}), dict), tool


def test_the_preview_binds_the_payload(desk):
    """Two payloads must not share a confirmation (D32).

    The hash is the only thing standing between approving one action and
    running a different one.
    """
    args = {"key": r"HKCU\Software\A", "name": "x", "type": "REG_SZ",
            "data": "1"}
    other = dict(args, key=r"HKCU\Software\B")
    assert (desk._payload_hash("registry.write", args,
                               desk._preview_registry_write(args))
            != desk._payload_hash("registry.write", other,
                                  desk._preview_registry_write(other)))


# ------------------------------------------------------------- the grammars

class TestRegistryKeys:
    r"""A key is refused as a shape, never sanitised into one."""

    @pytest.mark.parametrize("key", [
        r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run",
        r"HKLM\Software\Microsoft\Windows\CurrentVersion\Run",
        r"HKCU\Software\Microsoft\Windows\CurrentVersion\RunOnce",
        r"HKCU\Software\Microsoft\Windows\CurrentVersion\RunServices",
        r"HKCU\Software\Microsoft\Windows NT\CurrentVersion\Winlogon\Shell",
        r"HKCU\Software\Microsoft\Windows NT\CurrentVersion\Winlogon\Userinit",
        r"HKCU\Software\Microsoft\Windows NT\CurrentVersion\Windows\AppInit_DLLs",
        r"HKCU\Software\Microsoft\Windows NT\CurrentVersion\Image File "
        r"Execution Options\cmd",
    ])
    def test_a_persistence_key_is_refused(self, key):
        """This tool may change a setting; it may not acquire persistence.

        The rule has to be in the grammar rather than in a warning because an
        undo journal *can* undo a `Run` write - it knows the previous value -
        but whatever it launched has already run. That is the one change whose
        record and whose consequence diverge.
        """
        refusal = sysinfo.registry_key_refusal(key)
        assert refusal, key
        assert "persistence" in refusal.lower()

    def test_a_key_beneath_a_persistence_key_is_also_refused(self):
        assert "persistence" in str(sysinfo.registry_key_refusal(
            r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run\Sub")).lower()

    def test_hku_is_refused_with_a_reason(self):
        """`HKU`'s subkeys are other people's profiles, not this machine."""
        refusal = str(sysinfo.registry_key_refusal(r"HKU\S-1-5-21-1\Software"))
        assert "profile" in refusal.lower()

    @pytest.mark.parametrize("key", [r"HKCU\Software\Vendor Name",
                                     r"HKCU\Software\Foo.Bar",
                                     r"HKCU\Software\Foo_Bar-1"])
    def test_an_ordinary_key_is_accepted(self, key):
        assert sysinfo.registry_key_refusal(key) is None, key

    @pytest.mark.parametrize("key", [
        "Software\\Foo",          # no hive named
        r"HKBOGUS\Foo",           # no such hive
        r"HKCU\\Software",        # doubled backslash: an empty key name
        r"HKCU\Software\..\Win",  # a path that walks upwards
        r"HKCU\Software\.",
        r"HKCU",                  # a hive and nothing under it
    ])
    def test_a_malformed_key_is_refused(self, key):
        assert sysinfo.registry_key_refusal(key), key

    def test_a_refusal_says_what_would_work(self):
        """A refusal the user cannot act on is half a refusal."""
        assert "HKCU" in str(sysinfo.registry_key_refusal("Software"))


class TestRegistryValues:

    def test_a_value_name_with_spaces_is_fine(self):
        """Measured: `reg add /v "a name with spaces"` works and Windows prints
        it back that way. Refusing spaces would refuse ordinary names."""
        assert sysinfo.registry_value_refusal("a name with spaces") is None

    @pytest.mark.parametrize("name", ["", "   ", r"has\backslash", "has/slash",
                                      "x" * 200])
    def test_a_malformed_value_name_is_refused(self, name):
        assert sysinfo.registry_value_refusal(name), name

    @pytest.mark.parametrize("kind,data", [
        ("REG_DWORD", 4.2),
        ("REG_DWORD", True),
        ("REG_DWORD", "not a number"),
        ("REG_DWORD", 2 ** 32),
        ("REG_QWORD", 2 ** 64),
        ("REG_MULTI_SZ", "a string, not a list"),
        ("REG_MULTI_SZ", [1, 2]),
        ("REG_BINARY", "text"),
        ("REG_SZ", 42),
    ])
    def test_data_that_does_not_match_its_type_is_refused(self, kind, data):
        """Otherwise the failure is a `TypeError` naming an internal type,
        which says nothing about which of the two arguments was wrong."""
        assert sysinfo.registry_data_refusal(kind, data), (kind, data)

    def test_a_number_written_as_a_string_is_accepted(self):
        """A model writing a DWORD as "42" in JSON is writing the right thing
        in the wrong type. Converting it is right; refusing would train the
        caller to quote its numbers, which is a worse habit."""
        assert sysinfo.registry_data_refusal("REG_DWORD", "42") is None
        assert sysinfo.registry_data_refusal("REG_DWORD", "0x2a") is None


# ------------------------------------------------- the registry, for real

class TestRegistryWrite:

    def test_a_value_really_lands_with_the_type_it_was_given(self, desk):
        out = desk._exec_registry_write({
            "key": rf"HKCU\{TEST_ROOT}\Direct", "name": "answer",
            "type": "REG_DWORD", "data": 42})
        assert out["ok"] is True
        with winreg.OpenKey(TEST_HIVE, rf"{TEST_ROOT}\Direct") as key:
            value, kind = winreg.QueryValueEx(key, "answer")
        assert value == 42
        assert kind == winreg.REG_DWORD
        assert out["value_now"]["type"] == "REG_DWORD"

    def test_text_and_lists_land_as_themselves(self, desk):
        desk._exec_registry_write({"key": rf"HKCU\{TEST_ROOT}\Mixed",
                                   "name": "greeting", "type": "REG_SZ",
                                   "data": "hello world"})
        desk._exec_registry_write({"key": rf"HKCU\{TEST_ROOT}\Mixed",
                                   "name": "lines", "type": "REG_MULTI_SZ",
                                   "data": ["one", "two"]})
        with winreg.OpenKey(TEST_HIVE, rf"{TEST_ROOT}\Mixed") as key:
            assert winreg.QueryValueEx(key, "greeting")[0] == "hello world"
            assert winreg.QueryValueEx(key, "lines")[0] == ["one", "two"]

    def test_the_key_is_exported_before_the_write(self, desk):
        """The claim behind `reversible: True`.

        Not a note saying this could be undone - an actual `.reg` file that
        Windows itself produced, holding the key as it was.
        """
        desk._exec_registry_write({"key": rf"HKCU\{TEST_ROOT}\Exported",
                                   "name": "v", "type": "REG_SZ",
                                   "data": "original"})
        out = desk._exec_registry_write({"key": rf"HKCU\{TEST_ROOT}\Exported",
                                         "name": "v", "type": "REG_SZ",
                                         "data": "changed"})
        backup = pathlib.Path(out["backup"])
        assert backup.exists(), out
        assert backup.suffix == ".reg"
        assert "original" in backup.read_text(encoding="utf-16-le",
                                             errors="replace")
        assert out["undo"]["command"] == f'reg import "{out["backup"]}"'

    def test_the_recorded_undo_carries_an_argv_not_just_a_string(self, desk):
        """A command string that has to be re-parsed to be run is a command
        that can be run differently than it was recorded - and the one thing an
        undo must not be is approximate.

        This replays the *argv*, with no quoting involved, and checks the
        value really goes back to what it was.
        """
        desk._exec_registry_write({"key": rf"HKCU\{TEST_ROOT}\Runnable",
                                   "name": "v", "type": "REG_SZ",
                                   "data": "first"})
        out = desk._exec_registry_write({"key": rf"HKCU\{TEST_ROOT}\Runnable",
                                         "name": "v", "type": "REG_SZ",
                                         "data": "second"})
        argv = out["undo"]["argv"]
        assert argv[0].lower().endswith("reg.exe") and argv[1] == "import"
        assert subprocess.run(argv, capture_output=True).returncode == 0
        with winreg.OpenKey(TEST_HIVE, rf"{TEST_ROOT}\Runnable") as key:
            assert winreg.QueryValueEx(key, "v")[0] == "first"

    def test_a_new_key_needs_no_backup_and_says_so(self, desk):
        out = desk._exec_registry_write({"key": rf"HKCU\{TEST_ROOT}\BrandNew",
                                         "name": "x", "type": "REG_SZ",
                                         "data": "y"})
        assert out["backup"] is None
        assert out["undo"]["kind"] == "remove_value"

    def test_a_value_can_be_removed(self, desk):
        desk._exec_registry_write({"key": rf"HKCU\{TEST_ROOT}\Del",
                                   "name": "gone", "type": "REG_SZ",
                                   "data": "here"})
        out = desk._exec_registry_write({"key": rf"HKCU\{TEST_ROOT}\Del",
                                         "name": "gone", "delete": True})
        assert out["removed"] is True
        with winreg.OpenKey(TEST_HIVE, rf"{TEST_ROOT}\Del") as key:
            assert winreg.QueryInfoKey(key)[1] == 0

    def test_removing_a_value_that_is_not_there_says_so(self, desk):
        desk._exec_registry_write({"key": rf"HKCU\{TEST_ROOT}\Present",
                                   "name": "a", "type": "REG_SZ", "data": "b"})
        with pytest.raises(DaemonError) as caught:
            desk._exec_registry_write({"key": rf"HKCU\{TEST_ROOT}\Present",
                                       "name": "nothere", "delete": True})
        assert "nothere" in str(caught.value)

    def test_a_persistence_key_never_reaches_the_disk(self, desk):
        """Refused before anything is written *and* before anything is
        journalled - a refusal that wrote a row would be recording an intent
        to do the thing it just said it would not do."""
        with pytest.raises(Refused):
            desk._exec_registry_write({
                "key": r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run",
                "name": "ButleroyTest", "type": "REG_SZ", "data": "x.exe"})
        assert journal(desk) == []
        run_key = r"Software\Microsoft\Windows\CurrentVersion\Run"
        try:
            with winreg.OpenKey(TEST_HIVE, run_key) as key:
                names = [winreg.EnumValue(key, i)[0] for i in
                         range(winreg.QueryInfoKey(key)[1])]
        except FileNotFoundError:
            return                      # the key does not exist at all
        assert "ButleroyTest" not in names

    def test_the_preview_shows_what_is_there_now(self, desk):
        """A preview showing only the proposed value makes it impossible to
        tell a new setting from a changed one."""
        desk._exec_registry_write({"key": rf"HKCU\{TEST_ROOT}\Preview",
                                   "name": "n", "type": "REG_SZ", "data": "old"})
        changed = desk._preview_registry_write({
            "key": rf"HKCU\{TEST_ROOT}\Preview", "name": "n",
            "type": "REG_SZ", "data": "new"})
        assert changed["current_value"]["data"] == "old"
        assert changed["changes_anything"] is True
        fresh = desk._preview_registry_write({
            "key": rf"HKCU\{TEST_ROOT}\Absent", "name": "n",
            "type": "REG_SZ", "data": "new"})
        assert fresh["current_value"] is None
        assert fresh["changes_anything"] is False


# ---------------------------------------------------------- the undo journal

class TestTheUndoJournal:
    r"""`IRREVERSIBLE` in the tier name, reversible in fact, by way of a row
    written *before* the change."""

    def test_the_row_is_written_before_the_action_and_closed_after(self, desk,
                                                                    monkeypatch):
        monkeypatch.setattr(sysinfo, "run_capture", fake_run())
        desk._exec_power_shutdown({"action": "shutdown", "delay_seconds": 120})
        rows = journal(desk)
        assert [r["ok"] for r in rows] == [None, True], rows
        assert rows[0]["undo"]["command"].lower().endswith("shutdown.exe /a")
        assert rows[1]["id"] == rows[0]["id"], "one action, one record"

    def test_a_failed_action_is_still_journalled(self, desk, monkeypatch):
        """A journal of successes only is empty exactly when it is wanted -
        which is after something went wrong."""
        monkeypatch.setattr(sysinfo, "run_capture",
                            fake_run(exit_code=5, stderr="access denied"))
        with pytest.raises(DaemonError):
            desk._exec_power_shutdown({"action": "shutdown",
                                       "delay_seconds": 60})
        rows = journal(desk)
        assert [r["ok"] for r in rows] == [None, False], rows
        assert "exit 5" in rows[1]["result"]

    def test_a_refused_action_leaves_no_row(self, desk):
        """The journal records what was *tried*; a refusal never reached the
        machine, so it must leave no row suggesting it might have."""
        with pytest.raises(Refused):
            desk._exec_power_shutdown({"action": "sleep"})
        assert journal(desk) == []

    def test_it_is_append_only_text(self, desk, monkeypatch):
        """Readable by a human with a text editor, after the thing has broken.

        A database that will not open is a poor place to keep the record of
        whatever made it not open.
        """
        monkeypatch.setattr(sysinfo, "run_capture", fake_run())
        desk._exec_power_shutdown({"action": "shutdown", "delay_seconds": 60,
                                  "reason": "test"})
        text = desk._admin_undo_path().read_text(encoding="utf-8")
        assert text.endswith("\n")
        assert all(json.loads(line) for line in text.splitlines() if line.strip())

    def test_the_undo_tip_names_the_file_and_the_command(self, desk,
                                                         monkeypatch):
        monkeypatch.setattr(sysinfo, "run_capture", fake_run())
        out = desk._exec_power_shutdown({"action": "shutdown",
                                         "delay_seconds": 60})
        assert "shutdown.exe /a" in out["undo_tip"]
        assert "admin_undo.jsonl" in out["undo_tip"]


# ----------------------------------------------------------------- services

class TestServiceNames:
    r"""A service name is refused as a shape, never trimmed into one."""

    @pytest.mark.parametrize("name", ["Spooler", r"RpcSs\RpcEptMapper",
                                      "Foo.Bar", "a_b-1"])
    def test_real_names_are_accepted(self, name):
        assert sysinfo.SERVICE_NAME_OK.match(name), name

    @pytest.mark.parametrize("name", ["", " ", "Print Spooler", "Spool*",
                                      "Spool/er", 'Spool"er', "Spool`er",
                                      "x" * 100])
    def test_a_name_that_could_mean_another_service_is_refused(self, name):
        """Trimming "Print Spooler" to "PrintSpooler" would turn a mistake into
        a service the user did not name."""
        assert not sysinfo.SERVICE_NAME_OK.match(name), name


class TestServiceActions:

    def test_a_service_that_does_not_exist_is_an_answer(self, desk):
        with pytest.raises(Refused) as caught:
            desk._exec_service_start_stop({"service": "ButleroyNoSuch",
                                           "action": "start"})
        assert "no service named" in str(caught.value)

    def test_stopping_a_boot_critical_service_is_refused_with_its_reason(
            self, desk):
        """`RpcSs` is not here because it is dangerous. It is here because
        stopping it reboots the machine, so the undo - "start it" - is not
        available from inside the machine that is going down."""
        with pytest.raises(Refused) as caught:
            desk._exec_service_start_stop({"service": "RpcSs",
                                           "action": "stop"})
        message = str(caught.value)
        assert "will not be stopped" in message
        assert "reboot" in message

    def test_stopping_a_security_service_says_what_stops_protecting(self, desk):
        with pytest.raises(Refused) as caught:
            desk._exec_service_start_stop({"service": "WinDefend",
                                           "action": "restart"})
        assert "Defender" in str(caught.value)

    def test_starting_a_refused_service_is_still_allowed(self, desk, monkeypatch):
        """The refusal is about *stopping*. Refusing `start` on WinDefend would
        mean the tool could not undo its own refusal."""
        stub_service(monkeypatch, state=dict(A_SERVICE, state="1",
                                             state_name="STOPPED",
                                             running=False))
        monkeypatch.setattr(sysinfo, "run_capture", fake_run())
        out = desk._exec_service_start_stop({"service": "WinDefend",
                                             "action": "start"})
        assert out["ok"] is True
        assert out["undo"]["command"] == "sc stop WinDefend"

    def test_an_action_outside_the_grammar_is_refused(self, desk):
        with pytest.raises(Refused) as caught:
            desk._exec_service_start_stop({"service": "Spooler",
                                           "action": "delete"})
        assert "start, stop, restart" in str(caught.value)

    def test_the_undo_is_the_opposite_action_and_records_the_prior_state(
            self, desk, monkeypatch):
        stub_service(monkeypatch)
        monkeypatch.setattr(sysinfo, "run_capture", fake_run())
        out = desk._exec_service_start_stop({"service": "Spooler",
                                             "action": "stop"})
        assert out["undo"] == {"kind": "sc", "command": "sc start Spooler",
                               "reason": "it was RUNNING before"}
        assert out["state_before"] == "RUNNING"

    def test_the_preview_names_the_command_and_the_undo(self, desk, monkeypatch):
        stub_service(monkeypatch)
        preview = desk._preview_service_start_stop({"service": "Spooler",
                                                    "action": "stop"})
        assert preview["command"] == "sc stop Spooler"
        assert preview["undo"] == "sc start Spooler"
        assert preview["current_state"] == "RUNNING"
        assert preview["would_be_refused"] is None

    def test_a_restart_admits_it_is_not_an_undo(self, desk):
        assert "not an undo" in desk._preview_service_start_stop(
            {"service": "Spooler", "action": "restart"})["undo"]


# -------------------------------------------------------------------- power

class TestPower:

    @pytest.mark.parametrize("action", ["shutdown", "restart"])
    def test_the_delay_is_always_emitted(self, action):
        """Without `/t` Windows acts at once, which makes the confirmation the
        user just read a formality attached to something already done."""
        argv = sysinfo.power_argv(action, 90)
        assert argv[argv.index("/t") + 1] == "90"

    def test_cancel_gets_no_delay(self):
        """`/t` means nothing to `/a`, and passing it anyway would suggest the
        cancel is itself a scheduled thing."""
        argv = sysinfo.power_argv("cancel")
        assert argv[0].lower().endswith("shutdown.exe")
        assert argv[1:] == ["/a"]

    def test_the_argv_names_the_program_before_the_flags(self):
        """Without this the list is a set of switches and nothing to run.

        `run_capture` is handed the list, so a missing program name is a
        `FileNotFoundError` at the moment of action - invisible to any check
        that only looks at the command *string* in the preview.
        """
        for action in ("shutdown", "restart", "cancel", "logoff"):
            argv = sysinfo.power_argv(action)
            assert argv[0].lower().endswith(".exe"), action
            assert pathlib.Path(argv[0]).exists(), action

    def test_a_delay_too_short_to_cancel_is_refused(self, desk):
        """A shutdown you cannot stop is not a tool, it is a surprise.

        And it shows the shortest allowed one, because a refusal that does not
        show the workable version is half a refusal.
        """
        with pytest.raises(Refused) as caught:
            desk._exec_power_shutdown({"action": "shutdown",
                                       "delay_seconds": 5})
        message = str(caught.value)
        assert "too short to cancel" in message
        assert "30" in message
        assert "shutdown /a" in message

    @pytest.mark.parametrize("bad", ["soon", 1.5, "ten"])
    def test_a_delay_that_is_not_a_number_is_refused(self, desk, bad):
        with pytest.raises(Refused) as caught:
            desk._exec_power_shutdown({"action": "shutdown",
                                       "delay_seconds": bad})
        assert "whole number" in str(caught.value)

    def test_a_delay_longer_than_the_maximum_is_refused(self, desk):
        with pytest.raises(Refused) as caught:
            desk._exec_power_shutdown({"action": "shutdown",
                                       "delay_seconds": 99_999})
        assert "maximum" in str(caught.value)

    def test_an_unknown_action_is_refused(self, desk):
        with pytest.raises(Refused) as caught:
            desk._exec_power_shutdown({"action": "hibernate"})
        assert "not one of" in str(caught.value)

    @pytest.mark.parametrize("action", ["shutdown", "restart"])
    def test_the_cancel_is_named_in_the_preview_and_the_result(self, desk,
                                                               monkeypatch,
                                                               action):
        monkeypatch.setattr(sysinfo, "run_capture", fake_run())
        preview = desk._preview_power_shutdown({"action": action,
                                                "delay_seconds": 120})
        result = desk._exec_power_shutdown({"action": action,
                                            "delay_seconds": 120})
        for text in (preview["cancel_with"], preview["undo"],
                     result["cancel_with"]):
            assert "shutdown /a" in text

    def test_the_approved_command_is_the_one_that_runs(self, desk, monkeypatch):
        """D32 for the most consequential tool in the registry."""
        calls = {}
        monkeypatch.setattr(sysinfo, "run_capture", fake_run(calls))
        preview = desk._preview_power_shutdown({"action": "restart",
                                                "delay_seconds": 120})
        result = desk._exec_power_shutdown({"action": "restart",
                                            "delay_seconds": 120})
        assert preview["command"] == result["command"]
        assert calls["argv"] == [sysinfo.power_argv("restart", 120)]

    def test_cancelling_with_nothing_pending_is_reported_plainly(self, desk):
        """1116 is "no shutdown was in progress". Reporting success for a
        machine that was never going to shut down is a lie with a cheerful
        shape, so this says what was actually true."""
        out = desk._exec_power_shutdown({"action": "cancel"})
        assert out["ok"] is False
        assert out["cancelled"] is False
        assert out["exit_code"] == 1116
        assert "nothing was scheduled" in out["detail"]

    def test_cancelling_a_real_shutdown_reports_the_cancellation(self, desk,
                                                                 monkeypatch):
        monkeypatch.setattr(sysinfo, "run_capture", fake_run())
        assert desk._exec_power_shutdown({"action": "cancel"})["cancelled"] \
            is True


# ----------------------------------------------------------------- packages

class TestPackageNames:

    def test_notepadpp_is_a_valid_package_id(self):
        """`+` is there because Notepad++ is the one package on this machine
        whose id is not all lowercase letters, and a grammar that refused it
        would refuse the most obvious request on the desk."""
        assert sysinfo.PACKAGE_NAME_OK.match("notepad++")
        assert sysinfo.PACKAGE_NAME_OK.match("Visual Studio Code")

    @pytest.mark.parametrize("bad", ["", "   ", "a&calc", "x|y", "a;b",
                                     'a"b', "a\nb", "a>b"])
    def test_a_shell_metacharacter_is_refused(self, bad):
        """Not because a shell would see it - argv lists mean it would not -
        but because no package manager has such an id, so it is a mistake."""
        assert not sysinfo.PACKAGE_NAME_OK.match(bad), bad

    def test_a_space_is_allowed_because_package_ids_have_them(self):
        """`rm -rf` matches the grammar, and that is correct.

        "Visual Studio Code" and "Notepad++" are the ids a user actually
        types. The grammar cannot refuse spaces and accept those, so it does
        not, and the safety here comes from the argv list rather than from the
        character set - which is why `shell=False` is not negotiable.
        """
        assert sysinfo.PACKAGE_NAME_OK.match("rm -rf")


class TestPackageOwnership:
    r"""Measured on this machine: `choco list <missing> --exact
    --limit-output` exits **0** with empty output, and so does `choco info` on
    the same package. An exit code cannot tell installed from not-installed."""

    def test_exit_zero_and_no_output_means_not_owned(self, monkeypatch):
        monkeypatch.setattr(sysinfo, "manager_exe", lambda n: "choco.exe")
        monkeypatch.setattr(sysinfo, "run_capture",
                            fake_run(exit_code=0, stdout=""))
        assert sysinfo.package_owned_by("choco", "ripgrep") is False
        assert sysinfo.package_installed_version("choco", "ripgrep") == ""

    def test_a_listing_containing_the_id_means_owned_with_its_version(
            self, monkeypatch):
        monkeypatch.setattr(sysinfo, "manager_exe", lambda n: "choco.exe")
        monkeypatch.setattr(sysinfo, "run_capture",
                            fake_run(stdout="adb|37.0.0\nchocolatey|2.7.3\n"))
        assert sysinfo.package_owned_by("choco", "chocolatey") is True
        assert sysinfo.package_installed_version("choco",
                                                 "chocolatey") == "2.7.3"
        # A substring is not a match: "note++" must not match "notepad++".
        assert sysinfo.package_owned_by("choco", "note++") is False

    def test_uninstalling_what_no_manager_claims_is_refused(self, desk,
                                                            monkeypatch):
        """An installed *program* and a manager-owned *package* are different
        facts, and the refusal has to keep them apart.

        Both halves are stubbed, because on this machine winget *does* claim
        Notepad++ - its `list` includes programs registered in Add/Remove
        Programs, not only the ones it installed. A test that let the real
        managers answer would be a test of the user's software inventory.
        """
        monkeypatch.setattr(desk, "_package_owners", lambda pkg: [])
        monkeypatch.setattr(sysinfo, "package_list",
                            lambda query="", limit=200: {
                                "count": 1, "returned": 1, "query": query,
                                "packages": [{"name": "Notepad++",
                                              "version": "8.6.9",
                                              "publisher": "Don Ho",
                                              "installed": ""}],
                                "measured": True})
        refusal = str(
            desk._preview_package_uninstall({"package": "notepad++"})
            ["would_be_refused"])
        assert "not a package any manager" in refusal
        assert "Windows lists 1 program" in refusal

    def test_the_refusal_says_it_knows_the_program_is_installed(self, desk,
                                                               monkeypatch):
        """Knowing the program is there and not claiming it can remove it is
        the whole content of the refusal."""
        monkeypatch.setattr(desk, "_package_owners", lambda pkg: [])
        monkeypatch.setattr(sysinfo, "package_list",
                            lambda query="", limit=200: {
                                "count": 1, "returned": 1, "query": query,
                                "packages": [{"name": "Notepad++",
                                              "version": "8.6.9",
                                              "publisher": "Don Ho",
                                              "installed": ""}],
                                "measured": True})
        refusal = str(
            desk._preview_package_uninstall({"package": "notepad++"})
            ["would_be_refused"])
        assert "Windows lists 1 program" in refusal

    def test_two_managers_claiming_it_is_the_users_decision(self, desk,
                                                            monkeypatch):
        monkeypatch.setattr(desk, "_package_owners",
                            lambda pkg: [("choco", "1.0"),
                                          ("winget", "1.0")])
        assert "more than one" in str(
            desk._preview_package_uninstall({"package": "ripgrep"})
            ["would_be_refused"])

    def test_a_manager_that_is_not_here_is_refused_with_what_is(self, desk,
                                                                monkeypatch):
        monkeypatch.setattr(sysinfo, "package_managers",
                            lambda: {"count": 1, "managers": [
                                {"name": "choco", "exe": "choco.exe",
                                 "on_path": True, "usable": True}],
                                "note": "", "measured": True})
        with pytest.raises(Refused) as caught:
            desk._exec_package_install({"package": "ripgrep",
                                        "manager": "scoop"})
        assert "scoop" in str(caught.value) and "choco" in str(caught.value)

    def test_no_manager_at_all_is_refused(self, desk, monkeypatch):
        monkeypatch.setattr(sysinfo, "package_managers",
                            lambda: {"count": 0, "managers": [],
                                     "note": "none", "measured": True})
        with pytest.raises(Refused) as caught:
            desk._exec_package_install({"package": "ripgrep"})
        assert "no package manager" in str(caught.value)


class TestPackageInstall:
    r"""`manager_exe` and `run_capture` are stubbed *together* in every one of
    these, and the pairing is not tidiness.

    Stub only the first and `package_managers()` really tries to execute the
    fake `"winget.exe"`, gets `FileNotFoundError`, marks all three managers
    unusable - and the preview correctly, silently, shows no command at all.
    """

    @staticmethod
    def stub(monkeypatch, calls=None, exit_code=0, stdout="", stderr=""):
        monkeypatch.setattr(sysinfo, "manager_exe", lambda n: "winget.exe")
        monkeypatch.setattr(sysinfo, "run_capture",
                            fake_run(calls, exit_code=exit_code,
                                     stdout=stdout, stderr=stderr))

    def test_the_preview_shows_the_argv_the_executor_runs(self, desk,
                                                          monkeypatch):
        """A preview showing one command while the executor runs another is D32
        with the confirmation attached to the wrong payload."""
        self.stub(monkeypatch)
        preview = desk._preview_package_install({"package": "ripgrep"})
        assert preview["manager"] == "winget"
        assert preview["command"] == subprocess.list2cmdline(
            sysinfo.manager_install_argv("winget", "ripgrep"))
        assert preview["would_be_refused"] is None

    def test_the_argv_really_passed_matches_the_preview(self, desk, monkeypatch):
        calls = {}
        self.stub(monkeypatch, calls)
        out = desk._exec_package_install({"package": "ripgrep"})
        # `package_managers` also runs `<exe> --version` while discovering, so
        # the install is one call among several - asserted by membership, not
        # by position.
        assert sysinfo.manager_install_argv("winget", "ripgrep") in calls["argv"]
        assert out["ok"] is True

    def test_the_manager_can_be_named_rather_than_defaulted(self, desk,
                                                           monkeypatch):
        self.stub(monkeypatch)
        preview = desk._preview_package_install({"package": "ripgrep",
                                                "manager": "choco"})
        assert preview["manager"] == "choco"
        assert preview["command"].startswith("winget.exe uninstall") is False
        assert "-y" in preview["command"]

    def test_installing_records_the_uninstall_as_the_undo(self, desk,
                                                         monkeypatch):
        self.stub(monkeypatch)
        out = desk._exec_package_install({"package": "ripgrep"})
        assert "uninstall" in out["undo"]["command"]
        assert "not installed before" in out["undo"]["reason"]

    def test_a_failed_install_is_a_result_and_is_still_journalled(self, desk,
                                                                  monkeypatch):
        # The `--version` probe must still succeed, or discovery reports no
        # managers and the refusal would be the *discovery* failing rather
        # than the install. Stubbing one exit code for every call would have
        # made this test pass for the wrong reason.
        def run(argv, *, timeout=120.0):
            if "--version" in argv:
                return {"exit_code": 0, "stdout": "v1.29.380", "stderr": "",
                        "timed_out": False, "seconds": 0.0, "truncated": False}
            return {"exit_code": 1, "stdout": "",
                    "stderr": "No package found matching ripgrep",
                    "timed_out": False, "seconds": 0.0, "truncated": False}
        monkeypatch.setattr(sysinfo, "manager_exe", lambda n: "winget.exe")
        monkeypatch.setattr(sysinfo, "run_capture", run)
        out = desk._exec_package_install({"package": "ripgrep"})
        assert out["ok"] is False, "a failed install is a result, not a raise"
        assert out["exit_code"] == 1
        assert [r["ok"] for r in journal(desk)] == [None, False]


class TestPackageUninstall:

    def test_the_recorded_version_is_what_would_be_put_back(self, desk,
                                                            monkeypatch):
        monkeypatch.setattr(desk, "_package_owners",
                            lambda pkg: [("choco", "13.0.0")])
        monkeypatch.setattr(sysinfo, "manager_exe", lambda n: "choco.exe")
        monkeypatch.setattr(sysinfo, "run_capture", fake_run())
        out = desk._exec_package_uninstall({"package": "ripgrep"})
        assert out["removed_version"] == "13.0.0"
        assert "13.0.0" in out["undo"]["command"]
        assert "it was 13.0.0" in out["undo"]["reason"]

    def test_an_unknown_version_says_reinstalling_may_differ(self, desk,
                                                             monkeypatch):
        """A manager can claim a package and decline to name its version.
        Guessing one would put back something that is not what came out."""
        monkeypatch.setattr(desk, "_package_owners",
                            lambda pkg: [("scoop", "")])
        monkeypatch.setattr(sysinfo, "manager_exe", lambda n: "scoop.exe")
        monkeypatch.setattr(sysinfo, "run_capture", fake_run())
        out = desk._exec_package_uninstall({"package": "ripgrep"})
        assert "newer" in out["undo"]["reason"]


# ----------------------------------------------------- manager discovery

class TestManagerDiscovery:

    def test_a_manager_off_path_is_still_found(self):
        """The notepad++ lesson, applied to winget: `shutil.which("winget")` is
        None on this machine while the binary answers `--version`. Reporting
        "not installed" because of PATH is a fact about PATH, not about winget.
        """
        exe = sysinfo.manager_exe("winget")
        if exe:
            assert pathlib.Path(exe).exists()
            assert "WindowsApps" in exe
        assert bool(exe) == (sysinfo._winget_path() is not None)

    def test_the_report_names_the_path_and_says_whether_it_is_on_path(self):
        """`"winget"` in a preview means nothing to someone who has just
        learned this machine has two managers and only one is on PATH."""
        for row in sysinfo.package_managers()["managers"]:
            assert row["exe"] and "on_path" in row
            assert row.get("version") or row.get("error")
