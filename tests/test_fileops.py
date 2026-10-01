"""Tests for the file operations that change things.

Framed as an attacker with a legitimate tool call. Every test here is a way of
losing a user's file that the operation must refuse or survive.

The three that matter most:

* **nothing is overwritten silently.** `shutil.copy2` and `shutil.move` both
  overwrite silently by default. A copy aimed at an existing path loses the file
  there, and the only evidence is a file missing somewhere nobody looked.
* **overwriting a file keeps the old one.** `file.edit` is classified T1
  *reversible*, which is only true if the previous contents survive.
* **a rename cannot scatter a tree.** The pattern matches the file *name*, so it
  cannot rewrite directory components.

Everything runs on real temporary directories rather than a mocked filesystem. A
mock does not have the property that matters here - that the real filesystem
refuses some of these - and a test that passes against a mock can hide the exact
bug it was written to catch.
"""

import shutil
import zipfile
from pathlib import Path

import pytest

from assistant import fileops as F
from assistant.daemon import Daemon


@pytest.fixture
def tree(tmp_path):
    """A small real tree, plus a sibling that must stay unreachable."""
    root = tmp_path / "work"
    root.mkdir()
    (root / "a.txt").write_text("hello vram\nhello world\n", encoding="utf-8")
    (root / "b.txt").write_text("plain\n", encoding="utf-8")
    (root / "c.log").write_text("nothing here\n", encoding="utf-8")
    (tmp_path / "precious.txt").write_text("do not touch", encoding="utf-8")
    return tmp_path


# --- nothing is overwritten silently -----------------------------------------


def test_copy_will_not_overwrite_an_existing_file(tree):
    dest = tree / "work" / "b.txt"
    before = dest.read_text(encoding="utf-8")
    with pytest.raises(F.FileOpError, match="already exists"):
        F.copy(str(tree / "work" / "a.txt"), str(dest))
    assert dest.read_text(encoding="utf-8") == before, "the original was touched"


def test_move_will_not_overwrite_an_existing_file(tree):
    dest = tree / "work" / "b.txt"
    source = tree / "work" / "a.txt"
    with pytest.raises(F.FileOpError, match="already exists"):
        F.move(str(source), str(dest))
    assert dest.read_text(encoding="utf-8") == "plain\n"
    assert source.exists(), "the source must survive a refused move"


def test_archive_will_not_overwrite(tree):
    dest = tree / "work" / "out.zip"
    dest.write_bytes(b"existing archive")
    with pytest.raises(F.FileOpError, match="already exists"):
        F.archive(str(tree / "work"), str(dest))
    assert dest.read_bytes() == b"existing archive"


def test_a_refusal_names_what_is_there(tree):
    """A refusal that does not say what it found leaves the user guessing."""
    with pytest.raises(F.FileOpError) as exc:
        F.copy(str(tree / "work" / "a.txt"), str(tree / "work" / "b.txt"))
    assert "b.txt" in str(exc.value)
    assert "file" in str(exc.value)


# --- a rename cannot escape --------------------------------------------------


def test_a_bulk_rename_cannot_change_directory(tree):
    """The pattern matches the name, not the path.

    A replacement containing a separator would let a rename scatter a tree, so
    it is refused outright rather than sanitised. Both separators are checked:
    on Windows `os.sep` is a backslash, so "sub/x.md" walked straight past a
    check that only looked for the native one and then failed deep inside
    `with_name` with a bare ValueError.
    """
    with pytest.raises(F.FileOpError, match="not a usable file name"):
        F.bulk_rename(str(tree / "work"), r"\.txt$", "sub/x.md", dry_run=True)


def test_a_bulk_rename_cannot_change_drive(tree):
    with pytest.raises(F.FileOpError, match="not a usable file name"):
        F.bulk_rename(str(tree / "work"), r"\.txt$", "D:/x.md", dry_run=True)


def test_a_bulk_rename_cannot_produce_a_dot_name(tree):
    with pytest.raises(F.FileOpError):
        F.bulk_rename(str(tree / "work"), r".*", "", dry_run=True)


def test_a_copy_cannot_nest_a_directory_inside_itself(tree):
    with pytest.raises(F.FileOpError, match="into itself"):
        F.copy(str(tree / "work"), str(tree / "work" / "inner"))


def test_a_move_cannot_nest_a_directory_inside_itself(tree):
    with pytest.raises(F.FileOpError, match="into itself"):
        F.move(str(tree / "work"), str(tree / "work" / "inner"))


def test_containment_is_by_parts_not_by_string(tree):
    """`C:\\work-old` is not inside `C:\\work`.

    A string prefix check says it is, and that is how an operation ends up
    somewhere the user never approved.
    """
    sibling = tree / "work-old"
    sibling.mkdir()
    assert not F._is_inside(sibling, tree / "work")
    assert F._is_inside(tree / "work" / "a.txt", tree / "work")


# --- both spellings are reported ---------------------------------------------


def test_every_result_reports_the_proposed_and_the_resolved_path(tree):
    """The D32 rule, applied to every file operation.

    A junction or a `..` segment can make the string the model gave point
    somewhere other than where it will act. Acting on one and displaying the
    other is the redirection bug.
    """
    r = F.metadata(str(tree / "work" / "a.txt"))
    assert "proposed" in r and "resolved" in r
    assert Path(r["resolved"]).name == "a.txt"


# --- edit keeps the old version ----------------------------------------------


def test_edit_keeps_the_previous_version(tree):
    quarantine = tree / "quarantine"
    original = (tree / "work" / "a.txt").read_text(encoding="utf-8")
    r = F.edit(str(tree / "work" / "a.txt"), "replaced", backup_dir=quarantine)
    assert (tree / "work" / "a.txt").read_text(encoding="utf-8") == "replaced"
    assert r["backup"], "a backup path must be reported"
    assert Path(r["backup"]).read_text(encoding="utf-8") == original
    assert r["reversible"] is True


def test_edit_leaves_no_partial_file_behind(tree):
    F.edit(str(tree / "work" / "a.txt"), "new", backup_dir=tree / "q")
    assert not (tree / "work" / "a.txt.partial").exists()


def test_editing_a_file_that_did_not_exist_creates_it_without_a_backup(tree):
    r = F.edit(str(tree / "work" / "new.txt"), "hello", backup_dir=tree / "q")
    assert (tree / "work" / "new.txt").read_text(encoding="utf-8") == "hello"
    assert r["existed"] is False
    assert r["backup"] is None
    assert r["reversible"] is True


def test_edit_refuses_a_directory(tree):
    with pytest.raises(F.FileOpError, match="is a directory"):
        F.edit(str(tree / "work"), "x", backup_dir=tree / "q")


# --- bulk rename is a dry run unless asked otherwise -------------------------


def test_bulk_rename_defaults_to_a_dry_run(tree):
    """A rename touching a hundred files should require asking for it."""
    r = F.bulk_rename(str(tree / "work"), r"\.txt$", ".md")
    assert r["dry_run"] is True
    assert r["would_rename"] == 2
    assert (tree / "work" / "a.txt").exists(), "a dry run must rename nothing"
    assert not (tree / "work" / "a.md").exists()


def test_bulk_rename_reports_its_plan(tree):
    r = F.bulk_rename(str(tree / "work"), r"\.txt$", ".md")
    mapping = {p["from"]: p["to"] for p in r["plan"]}
    assert mapping == {"a.txt": "a.md", "b.txt": "b.md"}


def test_bulk_rename_acts_when_asked(tree):
    r = F.bulk_rename(str(tree / "work"), r"\.txt$", ".md", dry_run=False)
    assert r["renamed"] == 2
    assert (tree / "work" / "a.md").exists()
    assert not (tree / "work" / "a.txt").exists()


def test_a_bulk_rename_collision_renames_nothing_at_all(tree):
    """All or nothing. Half a rename is worse than none."""
    (tree / "work" / "a.md").write_text("in the way", encoding="utf-8")
    with pytest.raises(F.FileOpError, match="already exists"):
        F.bulk_rename(str(tree / "work"), r"\.txt$", ".md", dry_run=False)
    assert (tree / "work" / "a.txt").exists()
    assert (tree / "work" / "b.txt").exists()
    assert (tree / "work" / "a.md").read_text(encoding="utf-8") == "in the way"


def test_an_invalid_pattern_is_refused_before_anything_is_touched(tree):
    with pytest.raises(F.FileOpError, match="not a valid regular expression"):
        F.bulk_rename(str(tree / "work"), "([unclosed", "x", dry_run=True)


def test_a_bulk_rename_stops_at_the_cap(tree):
    for i in range(F.BULK_RENAME_CAP + 5):
        (tree / "work" / f"f{i}.txt").write_text("x", encoding="utf-8")
    with pytest.raises(F.FileOpError, match="cap"):
        F.bulk_rename(str(tree / "work"), r"\.txt$", ".md", dry_run=False)


# --- reading -----------------------------------------------------------------


def test_metadata_hashes_the_content(tree):
    a = F.metadata(str(tree / "work" / "a.txt"))
    b = F.metadata(str(tree / "work" / "b.txt"))
    assert len(a["sha256"]) == 64
    assert a["sha256"] != b["sha256"]


def test_metadata_counts_a_directory(tree):
    d = F.metadata(str(tree / "work"))
    assert d["kind"] == "dir"
    assert d["files"] == 3 and d["dirs"] == 0


def test_metadata_refuses_a_missing_file(tree):
    with pytest.raises(F.FileOpError, match="no such file"):
        F.metadata(str(tree / "work" / "nope.txt"))


def test_preview_says_when_a_file_is_binary_rather_than_emitting_mojibake(tree):
    (tree / "work" / "img.bin").write_bytes(b"\x89PNG\x00\x00\x01\x02" + b"\xff" * 200)
    r = F.preview(str(tree / "work" / "img.bin"))
    assert r["binary"] is True
    assert r["text"] == ""
    assert "binary" in r["note"]


def test_preview_truncates_and_says_so(tree):
    big = tree / "work" / "big.txt"
    big.write_text("x" * 10_000, encoding="utf-8")
    r = F.preview(str(big), max_bytes=100)
    assert r["truncated"] is True
    assert len(r["text"]) == 100


def test_search_finds_by_case_insensitive_substring(tree):
    r = F.search(str(tree / "work"), "VRAM")
    assert r["count"] == 1
    assert r["matches"][0]["line"] == 1
    assert "a.txt" in r["matches"][0]["path"]


def test_search_skips_binary_rather_than_reporting_junk(tree):
    (tree / "work" / "img.bin").write_bytes(b"\x00hello\x00" * 50)
    r = F.search(str(tree / "work"), "hello")
    assert r["skipped_binary"] >= 1


def test_search_refuses_an_empty_query(tree):
    with pytest.raises(F.FileOpError, match="something to look for"):
        F.search(str(tree / "work"), "")


def test_search_reports_truncation_rather_than_silently_cutting(tree):
    for i in range(F.SEARCH_MAX_MATCHES + 20):
        (tree / "work" / f"m{i}.txt").write_text("needle", encoding="utf-8")
    r = F.search(str(tree / "work"), "needle")
    assert r["truncated"] is True
    assert len(r["matches"]) <= F.SEARCH_MAX_MATCHES


# --- archive -----------------------------------------------------------------


def test_archive_produces_a_readable_zip(tree):
    r = F.archive(str(tree / "work"), str(tree / "out.zip"))
    with zipfile.ZipFile(r["destination"]) as zf:
        names = zf.namelist()
    assert "a.txt" in names and "c.log" in names


def test_archive_adds_an_extension_when_none_was_given(tree):
    r = F.archive(str(tree / "work" / "a.txt"), str(tree / "single"))
    assert r["destination"].endswith(".zip")
    assert Path(r["destination"]).exists()


def test_archive_refuses_something_over_the_limit(tree, monkeypatch):
    monkeypatch.setattr(F, "ARCHIVE_MAX_BYTES", 10)
    with pytest.raises(F.FileOpError, match="over the 2 GiB limit"):
        F.archive(str(tree / "work" / "a.txt"), str(tree / "big.zip"))


# --- the gate still governs all of it ----------------------------------------


@pytest.fixture
def daemon(tmp_path):
    from assistant.manifest import Workspace
    from assistant.policy import Tier

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    d = Daemon(state=tmp_path / "state", vault=tmp_path / "vault")
    d.manifests.add(Workspace(name="w", root=str(allowed), allow_tiers=(Tier.READ,
                                                                     Tier.REVERSIBLE_WRITE,
                                                                     Tier.CONSEQUENTIAL,
                                                                     Tier.IRREVERSIBLE)))
    return d, allowed


def call(d, tool, **args):
    from assistant import ipc
    from assistant.policy import CHANNEL_PC

    return d.handle(ipc.Request(op="tool_call",
                                args={"tool": tool, "channel": CHANNEL_PC,
                                      "args": args}, id=ipc.next_id()))


@pytest.mark.parametrize("tool", [
    "file.copy", "file.move", "file.archive", "file.bulk_rename",
])
def test_a_t2_file_action_asks_rather_than_acting(daemon, tool):
    """The first T2 executors with a real preview behind them.

    Absolute paths throughout: a relative one resolves against the process's
    working directory, not the workspace, and the gate refuses it - correctly.
    That refusal is the first thing this fixture proved by accident.
    """
    d, allowed = daemon
    (allowed / "a.txt").write_text("x", encoding="utf-8")
    (allowed / "b.txt").write_text("y", encoding="utf-8")
    r = call(d, tool, path=str(allowed / "a.txt"),
             destination=str(allowed / "out"),
             pattern=r"\.txt$", replacement=".md")
    body = r.get("result") or {}
    assert body.get("needs_confirmation") is True, r
    assert body.get("confirmation_id")
    assert (allowed / "b.txt").read_text(encoding="utf-8") == "y"
    assert (allowed / "a.txt").exists()
    assert not (allowed / "out").exists()


@pytest.mark.parametrize("tool", [
    "file.copy", "file.move", "file.edit", "file.search",
])
def test_an_outside_approved_root_is_refused(daemon, tool):
    d, allowed = daemon
    (allowed / "a.txt").write_text("x", encoding="utf-8")
    outside = str(d.state.parent / "escape.txt")
    r = call(d, tool, path=outside, destination=outside, content="x", query="x")
    assert r.get("ok") is False
    assert "outside every approved workspace root" in (r.get("error") or "")
    assert not (d.state.parent / "escape.txt").exists()


def test_the_assistant_cannot_edit_its_own_source(daemon):
    """D21, and the deny-path check, reaching a new executor."""
    d, allowed = daemon
    r = call(d, "file.edit", path=str(d.state.parent.parent / "src"), content="x")
    assert r.get("ok") is False


def test_every_file_tool_now_has_an_executor():
    """The gap is now exactly the two that are deliberately not built.

    Listed rather than asserted empty, so that adding a third unimplemented tool
    without noticing fails here, and so the list itself is the honest statement
    of what the assistant can and cannot do.
    """
    from assistant.tools import ALL_TOOLS

    deliberately_absent = {"file.write_notes", "file.empty_recycle_bin"}
    wanted = {t.name for t in ALL_TOOLS if t.name.startswith("file.")}
    missing = wanted - set(Daemon._EXECUTORS)
    assert missing == deliberately_absent, f"unexpectedly missing: {sorted(missing)}"