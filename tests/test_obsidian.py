"""
Tests for the Obsidian bridge, written from the attacker's side.

The question every test here asks is not "does the happy path work" but "what
happens when something tries to do the wrong thing". A bridge that can only
misbehave in the direction of writing outside `Assistant/` needs a test suite
that assumes the caller is hostile, because a model-chosen filename is, in
effect, exactly that.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from assistant.obsidian import (
    ASSISTANT_DIR,
    HARD_EXCLUDED,
    READABLE_DIRS,
    ObsidianBridge,
    ReadRefused,
    VaultError,
    WriteRefused,
    _is_under,
    slugify,
)


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """A stand-in vault with the same shape as the real one.

    The layout matters: the real vault's excluded directories are siblings of
    the readable ones, so the tests exercise containment rather than a
    convenient special case.
    """
    v = tmp_path / "Obsidian_Vault"
    for readable in READABLE_DIRS:
        (v / readable).mkdir(parents=True, exist_ok=True)
    for excluded in HARD_EXCLUDED:
        d = v / excluded
        d.mkdir(parents=True, exist_ok=True)
        (d / "corpus.md").write_text("training data, 6 GB of it", encoding="utf-8")
    (v / "evernote" / "note1.md").write_text("user note", encoding="utf-8")
    (v / "evernote" / "note2.md").write_text("another user note", encoding="utf-8")
    (v / ".opencode" / "agents").mkdir(parents=True, exist_ok=True)
    (v / ".opencode" / "agents" / "architect.md").write_text("# architect", encoding="utf-8")
    return v


@pytest.fixture
def bridge(vault: Path) -> ObsidianBridge:
    return ObsidianBridge(vault)


# ------------------------------------------------------------------ layout
def test_creates_the_assistant_layout(bridge):
    root = bridge.assistant_root
    assert root.is_dir()
    for sub in ("journal", "plans", "decisions", "capabilities", "reflection"):
        assert (root / sub).is_dir(), f"missing Assistant/{sub}"


def test_a_missing_vault_does_not_raise_on_construction(tmp_path):
    """The daemon must start even if the vault is unplugged.

    The live tier - tool calls against the machine - is the whole of "local
    tech support" and it does not depend on any of this. A missing vault is a
    degraded capability, not a startup failure.
    """
    b = ObsidianBridge(tmp_path / "not_here", create=False)
    assert b.available is False
    assert "MISSING" in b.describe()


def test_readable_and_excluded_cannot_overlap(vault):
    with pytest.raises(VaultError, match="both readable and excluded"):
        ObsidianBridge(vault, readable=("evernote", "datasets"), excluded=("datasets",))


# ------------------------------------------------------------------- writes
def test_write_note_lands_in_assistant(bridge):
    p = bridge.write_note("plans", "Refactor the CRM import", "Stage 1: read the schema.")
    assert p.is_file()
    assert bridge.assistant_root in p.parents
    assert p.read_text(encoding="utf-8").strip().endswith("read the schema.")


def test_write_note_with_front_matter(bridge):
    p = bridge.write_note(
        "decisions",
        "Chose two tiers",
        "The 9B cannot coexist with the 4B.",
        front_matter={"tier": "deep", "date": "2026-01-02"},
    )
    text = p.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert "tier: deep" in text


def test_writing_the_same_title_twice_does_not_overwrite(bridge):
    """A second write of the same title must not destroy the first.

    This is the single most likely way this module could lose something the
    user cares about: the same note title generated twice, the second
    generation wrong, the first silently gone.
    """
    first = bridge.write_note("plans", "Weekly review", "first version")
    second = bridge.write_note("plans", "Weekly review", "second version")
    assert first != second
    assert first.read_text(encoding="utf-8").strip() == "first version"
    assert second.read_text(encoding="utf-8").strip() == "second version"


def test_overwrite_is_possible_but_must_be_asked_for(bridge):
    p = bridge.write_note("plans", "Plan", "v1")
    again = bridge.write_note("plans", "Plan", "v2", overwrite=True)
    assert again == p
    assert p.read_text(encoding="utf-8").strip() == "v2"


def test_an_unknown_subdirectory_is_refused(bridge):
    with pytest.raises(VaultError, match="unknown Assistant subdirectory"):
        bridge.write_note("secrets", "oops", "body")


def test_a_title_with_windows_illegal_characters_still_writes(bridge):
    """Ordinary prose breaks naive filename construction. This must not raise."""
    p = bridge.write_note("plans", 'Plan: "Q3" / review <final>?', "body")
    assert p.is_file()
    assert p.suffix == ".md"
    assert not any(c in p.name for c in '<>:"/\\|?*')


# --------------------------------------------------------- write confinement
@pytest.mark.parametrize(
    "escape",
    [
        "../outside.md",
        "..\\outside.md",
        "../../Windows/System32/evil.md",
        "journal/../../escape.md",
    ],
)
def test_a_traversal_attempt_in_a_title_lands_inside_assistant(bridge, escape):
    """Defence 1: the slugifier neutralises the traversal before it can act.

    The property that matters is that nothing lands outside `Assistant/` - not
    that an exception is raised. The slugifier turns `../../evil.md` into
    `evil-md`, so the write succeeds and is harmless. Asserting an exception
    here would have tested the wrong layer.
    """
    p = bridge.write_note("plans", escape, "payload")
    assert _is_under(p, bridge.assistant_root)
    assert ".. " not in p.name
    assert not p.name.startswith(".")


def test_the_containment_layer_refuses_traversal_even_when_bypassing_the_slugifier(bridge):
    """Defence 2: `_write_path` holds on its own, with no sanitising in front.

    This is the layer that has to be correct, because it is the one that cannot
    assume its caller behaved. A test only of the slugifier would leave the
    containment check unverified.

    The escape depth is computed rather than written by hand. An earlier
    version of this test listed `"..\\outside.md"` and
    `"journal\\..\\..\\escape.md"` as escapes; both of those resolve back to a
    path *inside* `Assistant/` and were therefore testing nothing. Hand-counting
    `..` segments is not a thing to be trusted with.
    """
    base = bridge.assistant_root
    depth = len(base.parts) + 1          # one past the vault root itself
    escape_up = "\\".join([".."] * depth)

    escapes = [
        f"{escape_up}\\Windows\\System32\\evil.md",
        f"journal\\{escape_up}\\escape.md",
        f"{escape_up}\\outside.md",
    ]
    for hostile in escapes:
        resolved = Path(os.path.abspath(os.path.normpath(
            base / "plans" / hostile
        )))
        assert not _is_under(resolved, base), (
            f"test case {hostile!r} does not actually escape; it resolves to "
            f"{resolved}, which is still inside the root. Fix the case."
        )
        with pytest.raises(WriteRefused, match="outside Assistant"):
            bridge._write_path("plans", hostile)


def test_traversal_that_stays_inside_the_root_is_allowed(bridge):
    """The flip side, so the test above cannot pass by being over-strict.

    `plans/../x` resolves to `Assistant/x`, which is a legitimate target. A
    guard that refused it would be pushing callers to do their own path
    juggling - and would be wrong.
    """
    target = bridge._write_path("plans", "..\\still_inside.md")
    assert _is_under(target, bridge.assistant_root)
    assert target.parent == bridge.assistant_root


def test_a_sibling_prefix_directory_is_not_contained(tmp_path):
    """`Vault-old` must not satisfy containment for `Vault`.

    A string `startswith` test gets this exactly wrong, and it is the kind of
    error that stays invisible until the day someone creates the sibling.
    """
    from assistant.obsidian import _is_under

    vault = tmp_path / "Vault"
    sibling = tmp_path / "Vault-old"
    assert _is_under(vault / "note.md", vault) is True
    assert _is_under(sibling / "note.md", vault) is False
    assert _is_under(Path("D:/other/note.md"), vault) is False, "cross-drive"


def test_refuses_to_write_through_a_dangling_symlink(bridge, tmp_path):
    """The planted-symlink case, and specifically the *dangling* one.

    On Windows a symlink to a target that does not exist reports
    `exists() == False` while `is_symlink() == True`. A guard written as
    `if p.exists() and p.is_symlink()` silently skips exactly the case worth
    catching, because a symlink planted to redirect a write does not need its
    target to exist yet.
    """
    outside = tmp_path / "outside_target.md"   # deliberately never created
    link = bridge.assistant_root / "journal" / "link.md"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("symlinks unavailable on this filesystem")

    assert link.is_symlink() and not link.exists(), "expected a dangling symlink"
    with pytest.raises(WriteRefused, match="symlink"):
        bridge._write_path("journal", "link.md")


def test_refuses_to_write_through_a_junction_in_a_parent(bridge, tmp_path):
    """A symlinked *directory* inside Assistant/ must not redirect writes either.

    The leaf is an ordinary new file; the redirection is one level up, so a
    leaf-only check would miss it. This is why `_write_path` resolves before
    testing containment rather than testing the leaf.
    """
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    journal = bridge.assistant_root / "journal"
    # Move the real journal aside and put a link in its place.
    real = bridge.assistant_root / "journal_real"
    journal.rename(real)
    try:
        os.symlink(elsewhere, journal, target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError):
        journal.rename(real)
        real.rename(journal)
        pytest.skip("directory symlinks unavailable on this filesystem")

    try:
        with pytest.raises(WriteRefused):
            bridge._write_path("journal", "note.md")
    finally:
        try:
            os.unlink(journal)
        except OSError:
            pass
        real.rename(journal)


# -------------------------------------------------------------------- reads
def test_reading_an_excluded_directory_is_refused(bridge):
    for excluded in HARD_EXCLUDED:
        with pytest.raises(ReadRefused, match="hard-excluded"):
            bridge._read_path(excluded, "corpus.md")


def test_reading_a_non_allowlisted_directory_is_refused(bridge):
    with pytest.raises(ReadRefused, match="not on the read allowlist"):
        bridge._read_path("somewhere_else", "note.md")


def test_reading_assistant_through_raw_paths_is_refused(bridge):
    with pytest.raises(ReadRefused, match="through its own accessors"):
        bridge._read_path(ASSISTANT_DIR, "journal", "journal.jsonl")


def test_index_never_contains_an_excluded_note(bridge):
    """The load-bearing assertion. The corpus must not be reachable at all."""
    notes = bridge.index()
    assert notes, "expected the evernote and .opencode notes to be indexed"
    for note in notes:
        assert "datasets" not in note.path.parts
        assert ".smart-env" not in note.path.parts
    rels = " ".join(n.rel for n in notes)
    assert "corpus.md" not in rels
    assert "training data" not in rels


def test_index_stats_reports_counts_per_directory(bridge):
    stats = bridge.index_stats()
    assert stats["available"] is True
    assert stats["per_dir"]["evernote"]["files"] == 2
    assert stats["total_files"] >= 3
    assert set(stats["excluded"]) == set(HARD_EXCLUDED)


def test_iter_notes_prunes_excluded_dirs_even_under_an_allowed_parent(bridge):
    """Containment must hold locally, not only at the top level."""
    sneaky = bridge.vault / "evernote" / "datasets"
    sneaky.mkdir(parents=True, exist_ok=True)
    (sneaky / "hidden.md").write_text("should never be indexed", encoding="utf-8")
    for note in bridge.iter_notes("evernote"):
        assert "hidden.md" not in note.name


def test_iter_notes_on_a_missing_directory_yields_nothing(bridge):
    assert list(bridge.iter_notes("training")) == []


def test_refuses_to_index_more_files_than_the_limit(bridge, monkeypatch):
    import assistant.obsidian as ob

    monkeypatch.setattr(ob, "MAX_INDEX_FILES", 1)
    with pytest.raises(VaultError, match="read allowlist is wrong"):
        list(bridge.iter_notes("evernote"))


def test_note_read_refuses_a_file_over_the_limit(bridge, tmp_path):
    big = bridge.vault / "evernote" / "big.md"
    big.write_text("x" * 500, encoding="utf-8")
    note = next(n for n in bridge.iter_notes("evernote") if n.name == "big.md")
    with pytest.raises(VaultError, match="read limit"):
        note.read(max_bytes=100)


# ------------------------------------------------------------------ role md
def test_role_templates_are_offered_as_templates(bridge):
    templates = bridge.role_templates()
    assert [t.name for t in templates] == ["architect.md"]


# ------------------------------------------------------------------ journal
def test_append_jsonl_round_trips(bridge):
    for i in range(3):
        bridge.append_jsonl("journal", {"seq": i, "what": f"event {i}"})
    rows = bridge.read_journal("journal")
    assert [r["seq"] for r in rows] == [0, 1, 2]


def test_append_jsonl_survives_a_non_ascii_payload(bridge):
    """Windows text-mode translation would corrupt this.

    The same class of bug as the authkey text-mode corruption in ipc.py: a
    newline byte inside the payload. `\\n` in JSON is escaped so this is a
    narrow case, but a non-ASCII note is not.
    """
    payload = {"title": "café — naïve — 日本語", "emoji": "\U0001f600"}
    bridge.append_jsonl("journal", payload)
    assert bridge.read_journal("journal")[0]["title"] == payload["title"]


def test_read_journal_skips_a_torn_final_line(bridge):
    bridge.append_jsonl("journal", {"seq": 1})
    bridge.append_jsonl("journal", {"seq": 2})
    path = bridge.assistant_root / "journal" / "journal.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"seq": 3, "torn')  # killed mid-write
    rows = bridge.read_journal("journal")
    assert [r["seq"] for r in rows] == [1, 2], "a torn tail must not hide earlier records"


def test_append_jsonl_refuses_a_non_jsonl_filename(bridge):
    with pytest.raises(VaultError, match="only writes .jsonl"):
        bridge.append_jsonl("journal", {"a": 1}, filename="journal.md")


def test_read_journal_on_an_empty_file_returns_nothing(bridge):
    assert bridge.read_journal("journal") == []


# ----------------------------------------------------------------- rollback
def test_snapshot_then_rollback_removes_only_new_files(bridge):
    bridge.write_note("plans", "Kept plan", "keep me")
    snap = bridge.snapshot("plans")
    bridge.write_note("plans", "New plan A", "junk")
    bridge.write_note("plans", "New plan B", "junk")

    result = bridge.rollback(snap)
    assert result["removed"] == 2
    remaining = sorted(p.name for p in (bridge.assistant_root / "plans").iterdir())
    assert remaining == ["kept-plan.md"], "rollback must not touch pre-snapshot notes"


def test_rollback_refuses_a_snapshot_pointing_outside_assistant(bridge, tmp_path):
    from assistant.obsidian import Snapshot

    hostile = Snapshot(taken="now", directory="plans", root=str(tmp_path))
    with pytest.raises(WriteRefused):
        bridge.rollback(hostile)


def test_snapshot_of_a_missing_directory_is_empty(bridge):
    snap = bridge.snapshot("reflection")
    assert snap.count == 0
    assert bridge.rollback(snap)["removed"] == 0


# ------------------------------------------------------------------- slugs
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Simple", "simple"),
        ("With Spaces", "with-spaces"),
        ("punctuation!!! here", "punctuation-here"),
        ("CamelCaseName", "camelcasename"),
    ],
)
def test_slugify(raw, expected):
    assert slugify(raw) == expected


def test_slugify_refuses_an_empty_title():
    with pytest.raises(VaultError):
        slugify("   ")
    with pytest.raises(VaultError):
        slugify("///")


def test_slugify_truncates_long_titles():
    assert len(slugify("a" * 500)) <= 80


def test_slugs_are_unique_under_a_collision(bridge):
    """Two different titles that slugify the same must not collide silently."""
    a = bridge.write_note("plans", "Plan (draft)", "one")
    b = bridge.write_note("plans", "Plan draft", "two")
    assert a != b
