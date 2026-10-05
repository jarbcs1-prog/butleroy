r"""The last two file tools: durable notes, and emptying the recycle bin (D63).

Two tools with nothing in common except that each of them was nearly impossible
to build correctly for a reason nobody would have guessed from the spec.

**`file.write_notes` was unusable as specified.** Its argument was called
`subdir`, and the gate screens any string argument whose key contains `dir` as a
path. So `{"subdir": "journal"}` was refused as *"path 'journal' is outside every
approved workspace root"* - on a machine where the note was about to be written
*inside* an approved vault. The name was the defect; carving `subdir` out of the
screen was rejected because a hole in the rule is still a hole.

**`file.empty_recycle_bin` had to be built out of what Windows will tell us**,
because `C:\$Recycle.Bin` is on our own deny list. Walking that directory would
have worked perfectly well, and that is exactly why it is wrong: the path would be
buried inside a script string and never reach the check that refuses it. So the
count comes from the shell's own COM namespace, the target comes from
`Clear-RecycleBin -WhatIf`, and no byte total is reported at all because the COM
size column could not be verified on this machine and an unchecked number is worse
than no number.

What these tests hold down, in order of how quietly each one could be wrong:

* the vault file is **read back off disk**, because the executor returning a path
  it believes in proves nothing about what is there;
* `overwrote` is measured from the filename rather than reported from the flag
  the bridge was called with;
* the preview names the deny list, because a preview that says *how* it counted
  without saying *why not the obvious way* invites a question with no answer in
  the dialog;
* a **silent Windows is a refusal**, and the test asserts that `recycle_clear`
  was never called rather than that a certain word appeared;
* the journal gets a row with a **null** undo, because a journal that only
  records what it can undo cannot tell you which entries were the ones you cannot
  take back.
"""

import json

import pytest

from assistant import policy, sysinfo, tools
from assistant.daemon import Daemon, DaemonError


# --------------------------------------------------------------- fixtures


@pytest.fixture
def desk(tmp_path):
    """A daemon with a *real* vault on a temporary disk.

    Real because `write_note`'s no-overwrite behaviour is a property of the
    bridge's filename handling, and a mocked bridge would return whatever the
    test told it to return - which is the belief under test.

    The vault's path is kept on the test rather than read back off the bridge:
    `ObsidianBridge` publishes `available`, `describe`, `write_note` and the
    rest, and deliberately not its root, so a test that reached in for one would
    be asserting on a private detail rather than on what was written.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    d = Daemon(state=tmp_path / "state", vault=vault)
    d.vault_path = vault
    return d


@pytest.fixture
def rooted(desk, tmp_path):
    """The same, with one approved workspace, so the gate has something to say.

    `add` takes a `Workspace`, not keyword pairs, and `Workspace`'s default
    `allow_tiers` is `(READ,)` alone - which would refuse a T1 write for a reason
    that has nothing to do with the thing under test. The tiers are widened
    explicitly so a refusal here means the path check fired, not the tier list.
    """
    from assistant.manifest import Workspace

    ws = tmp_path / "ws"
    ws.mkdir()
    desk.manifests.add(Workspace(
        name="ws", root=str(ws),
        allow_tiers=(policy.Tier.READ, policy.Tier.REVERSIBLE_WRITE,
                     policy.Tier.CONSEQUENTIAL, policy.Tier.IRREVERSIBLE)))
    # `_exec_*` bypasses `_verdict`, which is where the daemon refreshes this.
    # Setting it by hand is not a shortcut around the gate; it is the only way to
    # ask the same question the gate was asked.
    desk.gate.allowed_roots = list(desk.manifests.roots())
    desk.ws = ws
    return desk


def note_files(desk):
    return sorted(p.name for p in (desk.vault_path / "Assistant").rglob("*.md"))


def journal_rows(desk):
    path = desk.state / "admin_undo.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def fake_bin(monkeypatch, *, names_target=True, exit_code=0, count=2,
             available=True):
    """Stub the three recycle calls and record the order they were asked in.

    Stubbed rather than real because the real `Clear-RecycleBin` would empty the
    user's bin during the test suite, and because a test that lets a real
    destructive command run to find out what it does is the accident, not the
    measurement.
    """
    calls = []

    def listing():
        calls.append("listing")
        if not available:
            return {"available": False, "count": None, "items": [],
                    "source": "shell namespace (COM NameSpace 0xA)",
                    "denied_directory": sysinfo.RECYCLE_BIN_DIR,
                    "note": "the shell would not talk", "measured": True}
        return {"available": True, "count": count,
                "items": [{"name": f"item{i}.txt"} for i in range(count)],
                "source": "shell namespace (COM NameSpace 0xA)",
                "denied_directory": sysinfo.RECYCLE_BIN_DIR,
                "note": "", "measured": True}

    def whatif():
        calls.append("whatif")
        return {"command": "Clear-RecycleBin -WhatIf", "exit_code": 0,
                "stdout": ('What if: Performing the operation "Clear-RecycleBin"'
                           ' on target "All of the contents of the Recycle Bin".'),
                "stderr": "", "names_target": names_target,
                "note": "", "measured": True}

    def clear():
        calls.append("clear")
        return {"command": "Clear-RecycleBin -Force", "exit_code": exit_code,
                "ok": exit_code == 0, "stdout": "", "stderr": "",
                "timed_out": False, "seconds": 0.1, "measured": True}

    monkeypatch.setattr(sysinfo, "recycle_listing", listing)
    monkeypatch.setattr(sysinfo, "recycle_whatif", whatif)
    monkeypatch.setattr(sysinfo, "recycle_clear", clear)
    return calls


# ------------------------------------------------- the argument name itself


class TestTheParameterRename:
    """`subdir` is unusable as an argument name here, and that is a finding."""

    def test_the_spec_no_longer_offers_the_name_the_gate_refuses(self):
        spec = {t.name: t for t in tools.ALL_TOOLS}["file.write_notes"]
        assert "section" in spec.parameters
        assert "subdir" not in spec.parameters, (
            "the gate screens any key containing 'dir' as a path, so a "
            "'subdir' argument is refused as a path outside every root")

    def test_the_gate_refuses_the_old_name_and_allows_the_new_one(self, rooted):
        """Measured, not asserted from the implementation: this is the gate's
        own verdict on the two spellings, over a real approved root."""
        gate = rooted.gate
        base = {"title": "t", "body": "b"}
        old = gate.evaluate("file.write_notes", channel="pc",
                            args={**base, "subdir": "journal"}, actor="test")
        new = gate.evaluate("file.write_notes", channel="pc",
                            args={**base, "section": "journal"}, actor="test")
        assert old.decision == policy.Decision.DENY
        assert "outside every approved workspace root" in old.reason
        assert new.decision == policy.Decision.ALLOW

    def test_the_refusal_names_what_would_have_worked(self, rooted):
        """A refusal the user cannot act on is a refusal that wastes a round."""
        v = rooted.gate.evaluate(
            "file.write_notes", channel="pc",
            args={"title": "t", "body": "b", "subdir": "journal"}, actor="test")
        assert "ws" in v.reason.lower() or "approve" in v.reason.lower()

    def test_the_tool_takes_no_path_at_all(self):
        """A T1 write tool handed a path is an arbitrary-write primitive wearing
        a vocabulary name. The vault chooses the directory."""
        spec = {t.name: t for t in tools.ALL_TOOLS}["file.write_notes"]
        assert not any("path" in k for k in spec.parameters)


# --------------------------------------------------------------- the notes


class TestWritingANote:
    def test_the_fixture_writes_a_real_file(self, rooted):
        """The fixture asserts itself: an executor returning a path it believes
        in proves nothing about what is on disk."""
        out = rooted._exec_file_write_notes(
            {"section": "journal", "title": "First Note", "body": "hello"})
        assert out["path"], "no path reported"
        made = rooted.vault_path / "Assistant"
        assert list(made.rglob("first-note.md")), note_files(rooted)
        assert "hello" in (made / "journal" / "first-note.md").read_text(
            encoding="utf-8")

    def test_a_new_title_reports_what_the_filename_can_actually_tell_us(self,
                                                                        rooted):
        """`overwrote` is measured, not read back off the flag.

        A first write creates the plain-named file, so the name matches and the
        note is not a replacement of anything. Reporting `overwrote: False` from
        `write_note`'s default would be a lie the bridge could contradict, and
        the filename is the evidence.
        """
        out = rooted._exec_file_write_notes(
            {"section": "plans", "title": "A New Plan", "body": "x"})
        assert out["overwrote"] is True
        assert out["path"].endswith("a-new-plan.md"), out["path"]

    def test_a_repeat_writes_a_sibling_and_says_what_it_did_not_replace(self,
                                                                        rooted):
        """The bridge never overwrites by default; it timestamps a sibling. The
        tool has to report that, because the user asked to write a note and
        otherwise gets two."""
        first = rooted._exec_file_write_notes(
            {"section": "plans", "title": "Same Title", "body": "one"})
        second = rooted._exec_file_write_notes(
            {"section": "plans", "title": "Same Title", "body": "two"})
        assert second["overwrote"] is False
        assert second["path"] != first["path"]
        assert second["previous"], "the would-be path must be named"
        assert second["previous"] == first["path"]
        assert "Nothing you wrote was replaced" in second["note"]
        assert (rooted.vault_path / "Assistant" / "plans"
                / second["path"].rsplit("/", 1)[-1]).exists()

    def test_the_two_branches_say_different_things(self, rooted):
        first = rooted._exec_file_write_notes(
            {"section": "journal", "title": "Branch", "body": "a"})
        second = rooted._exec_file_write_notes(
            {"section": "journal", "title": "Branch", "body": "b"})
        assert first["note"] != second["note"]

    @pytest.mark.parametrize("section", sorted(
        ["journal", "plans", "decisions", "capabilities", "reflection"]))
    def test_every_section_the_vault_offers_is_reachable(self, rooted, section):
        out = rooted._exec_file_write_notes(
            {"section": section, "title": f"A {section} note", "body": "x"})
        assert section in out["path"]

    def test_a_missing_section_names_the_choices(self, rooted):
        with pytest.raises(DaemonError) as caught:
            rooted._exec_file_write_notes({"title": "t", "body": "b"})
        for name in ("journal", "plans", "decisions"):
            assert name in str(caught.value)

    def test_an_unknown_section_names_the_choices(self, rooted):
        with pytest.raises(DaemonError) as caught:
            rooted._exec_file_write_notes(
                {"section": "nonsense", "title": "t", "body": "b"})
        assert "journal" in str(caught.value)

    def test_no_title_is_refused(self, rooted):
        with pytest.raises(DaemonError) as caught:
            rooted._exec_file_write_notes(
                {"section": "journal", "title": "  ", "body": "b"})
        assert "title" in str(caught.value)

    @pytest.mark.parametrize("body", ["", "   ", None])
    def test_no_body_is_refused(self, rooted, body):
        with pytest.raises(DaemonError) as caught:
            rooted._exec_file_write_notes(
                {"section": "journal", "title": "t", "body": body})
        assert "body" in str(caught.value)

    def test_an_oversized_body_says_the_limit_and_a_way_out(self, rooted):
        from assistant import obsidian
        with pytest.raises(DaemonError) as caught:
            rooted._exec_file_write_notes({
                "section": "journal", "title": "t",
                "body": "x" * (obsidian.MAX_WRITE_BYTES + 1)})
        message = str(caught.value)
        assert str(obsidian.MAX_WRITE_BYTES) in message
        assert "split" in message.lower()

    def test_a_refused_note_writes_nothing(self, rooted):
        with pytest.raises(DaemonError):
            rooted._exec_file_write_notes(
                {"section": "nonsense", "title": "t", "body": "b"})
        assert note_files(rooted) == []

    def test_it_is_tier_one_because_it_moves_no_bytes(self):
        """T1 = reversible write. Obsidian keeps the file; a later note replaces
        it in place or sits beside it. Nothing here is irreversible."""
        spec = {t.name: t for t in tools.ALL_TOOLS}["file.write_notes"]
        assert spec.tier == policy.Tier.REVERSIBLE_WRITE
        assert not spec.destructive
        assert "file.write_notes" not in Daemon._PREVIEWS


# --------------------------------------------------------------- the bin


class TestTheRecycleBinPreview:
    """What a person is shown before the one thing here cannot be taken back."""

    def test_it_leads_with_the_action(self, rooted, monkeypatch):
        fake_bin(monkeypatch)
        out = rooted._preview_file_empty_recycle_bin({})
        assert list(out)[0] == "action"
        assert "no undo" in out["action"]

    def test_it_reports_what_windows_itself_says(self, rooted, monkeypatch):
        fake_bin(monkeypatch)
        out = rooted._preview_file_empty_recycle_bin({})
        assert "What if:" in out["windows_says"]
        assert "Recycle Bin" in out["windows_says"]
        assert out["windows_confirmed_the_target"] is True

    def test_it_says_the_denied_path_and_why_it_was_not_read(self, rooted,
                                                             monkeypatch):
        """A preview that says *how* it counted but not *why not the obvious
        way* invites a question the dialog cannot answer."""
        fake_bin(monkeypatch)
        out = rooted._preview_file_empty_recycle_bin({})
        text = out["how_we_counted"]
        assert sysinfo.RECYCLE_BIN_DIR in text
        assert "deny list" in text
        assert text.count("size") == 1, "the size caveat is said twice"

    def test_it_reports_no_byte_total(self, rooted, monkeypatch):
        """The COM size column could not be verified on this machine. A number
        nobody checked is worse than no number."""
        fake_bin(monkeypatch)
        out = rooted._preview_file_empty_recycle_bin({})
        assert not any("byte" in k for k in out)

    def test_undo_is_explicitly_null_and_not_absent(self, rooted, monkeypatch):
        fake_bin(monkeypatch)
        out = rooted._preview_file_empty_recycle_bin({})
        assert "undo" in out
        assert out["undo"] is None
        assert out["reversible"] is False
        assert out["undo_note"]

    def test_it_distinguishes_the_quarantine(self, rooted, monkeypatch):
        """Without this a user could believe emptying the bin makes quarantined
        files unrecoverable. It does not: `file.delete` never touches the bin."""
        fake_bin(monkeypatch)
        out = rooted._preview_file_empty_recycle_bin({})
        assert "quarantine" in out["not_the_quarantine"]
        assert "file.restore" in out["not_the_quarantine"]

    def test_an_unreadable_bin_is_unknown_not_empty(self, rooted, monkeypatch):
        """An empty list and an unreadable list look identical and mean opposite
        things."""
        fake_bin(monkeypatch, available=False)
        out = rooted._preview_file_empty_recycle_bin({})
        assert out["count"] is None
        assert "unknown" in out["count_unknown"]

    def test_a_silent_windows_is_said_out_loud(self, rooted, monkeypatch):
        fake_bin(monkeypatch, names_target=False)
        out = rooted._preview_file_empty_recycle_bin({})
        assert out["windows_confirmed_the_target"] is False
        assert out["windows_said_nothing_useful"]


class TestTheRecycleBinExecutor:
    def test_the_fixture_is_not_run_for_real(self, rooted, monkeypatch):
        """The bin must not be emptied by the test suite. This is the check that
        keeps that true, and it is why every test below stubs."""
        calls = fake_bin(monkeypatch)
        rooted._exec_file_empty_recycle_bin({})
        assert calls == ["whatif", "listing", "clear"]

    def test_the_result_carries_the_evidence_not_just_the_preview(self, rooted,
                                                                 monkeypatch):
        """A confirmation shown ten minutes ago is evidence about ten minutes
        ago."""
        fake_bin(monkeypatch)
        out = rooted._exec_file_empty_recycle_bin({})
        assert "What if:" in out["windows_says"]
        assert out["reversible"] is False
        assert out["undo"] is None

    def test_it_records_how_much_it_was_holding(self, rooted, monkeypatch):
        fake_bin(monkeypatch, count=4)
        out = rooted._exec_file_empty_recycle_bin({})
        assert out["was_holding"] == 4

    def test_a_silent_windows_refuses_and_does_nothing(self, rooted, monkeypatch):
        """Asserted on the call log, not on a word: what matters is that
        `clear` was never reached."""
        calls = fake_bin(monkeypatch, names_target=False)
        with pytest.raises(DaemonError) as caught:
            rooted._exec_file_empty_recycle_bin({})
        assert "clear" not in calls, "it emptied the bin on no evidence"
        assert "Nothing was done" in str(caught.value)

    def test_a_non_zero_exit_is_reported_not_raised_away(self, rooted,
                                                         monkeypatch):
        """`run_capture`'s rule, kept: the exit code is the answer to a question
        that was asked, so it is carried into the message and the journal rather
        than thrown away as an exception.

        Asserted on the two facts - the code reached the user, and the journal
        says the attempt failed - rather than on wording. An earlier version of
        this test asserted a reassuring phrase ("the bin is as it was") that no
        code ever produced: `Clear-RecycleBin` failing part-way through could
        have removed some items, so that reassurance would have been a guess.
        Whether a partial clear is possible was not measured, so the tool does
        not claim either way.
        """
        fake_bin(monkeypatch, exit_code=5)
        with pytest.raises(DaemonError) as caught:
            rooted._exec_file_empty_recycle_bin({})
        assert "5" in str(caught.value), "the exit code must reach the user"
        rows = [r for r in journal_rows(rooted)
                if r["tool"] == "file.empty_recycle_bin"]
        assert rows, "a failed attempt must still be recorded"
        assert rows[-1]["ok"] is False, "and recorded as failed, not as done"


class TestTheUndoJournalWithoutAnUndo:
    """The one T3 here has no inverse, which is the honest test of D60's
    journal: a journal that only records what it can undo cannot tell you which
    of its entries were the ones you cannot take back."""

    def _rows(self, desk):
        return [r for r in journal_rows(desk)
                if r["tool"] == "file.empty_recycle_bin"]

    def test_the_row_is_written_before_anything_is_emptied(self, rooted,
                                                           monkeypatch):
        fake_bin(monkeypatch)
        rooted._exec_file_empty_recycle_bin({})
        rows = self._rows(rooted)
        assert len(rows) == 2, "an open row and a closing row"
        assert rows[0]["ok"] is None, "the first row is open, not concluded"
        assert rows[1]["ok"] is True

    def test_the_undo_is_null_and_the_reason_is_stated(self, rooted, monkeypatch):
        fake_bin(monkeypatch)
        rooted._exec_file_empty_recycle_bin({})
        row = self._rows(rooted)[0]
        assert row.get("undo") is None
        assert row["note"], "a null undo needs a reason beside it"
        assert "no inverse" in row["note"]

    def test_a_refusal_writes_no_row(self, rooted, monkeypatch):
        """Nothing happened, so there is nothing to have happened."""
        fake_bin(monkeypatch, names_target=False)
        with pytest.raises(DaemonError):
            rooted._exec_file_empty_recycle_bin({})
        assert self._rows(rooted) == []


# ------------------------------------------------------------- registration


def test_the_deny_list_still_owns_the_directory_we_never_read():
    """The refusal has a name and the name is real. If someone removes it from
    the deny list, the code that refuses it stops being a refusal."""
    assert sysinfo.RECYCLE_BIN_DIR in policy.DENY_PATHS


def test_nothing_in_the_recycle_helpers_touches_the_denied_directory():
    """The constant is declared so the refusal has a name. If it ever became an
    operand, the refusal would be decoration."""
    for fn in (sysinfo.recycle_listing, sysinfo.recycle_whatif,
               sysinfo.recycle_clear):
        assert sysinfo.RECYCLE_BIN_DIR not in fn.__code__.co_names, (
            f"{fn.__name__} references the denied path")


def test_both_tools_are_registered_and_neither_is_an_op():
    for name in ("file.write_notes", "file.empty_recycle_bin"):
        assert name in Daemon._EXECUTORS
        assert name not in Daemon._OPS, f"{name} is a second door (D59)"


def test_only_the_bin_has_a_preview():
    """The preview is demanded by `_check_tables` because the bin is flagged
    destructive. `write_notes` is T1 and gets none, which is the right shape:
    a preview for a tool nobody is asked to bless is theatre."""
    assert "file.empty_recycle_bin" in Daemon._PREVIEWS
    assert "file.write_notes" not in Daemon._PREVIEWS


def test_the_two_names_are_not_an_op_method_either():
    """Checked on method names rather than source text: a docstring about the
    defect would contain the name."""
    ops = [m for m in dir(Daemon) if m.startswith("op_")
           and ("notes" in m or "recycle" in m)]
    assert ops == []
