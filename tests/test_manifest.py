"""Tests for the workspace manifest store.

These are the tests that make D10 mean something. They are written from the
attacker's side: what happens if a manifest lies, if one is malformed, if one is
copied from somewhere else, if the directory it names does not exist, or if the
someone editing the file tries to buy more authority than the session has.

Everything runs against real temporary directories on a real filesystem. A mock
filesystem would not catch the case that actually matters here, which is whether
Windows will let us write a file into a directory we chose.
"""

from __future__ import annotations

import textwrap
from datetime import date
from pathlib import Path

import pytest

from assistant.manifest import (
    DangerLevel,
    ManifestInvalid,
    ManifestStore,
    NotPermitted,
    Workspace,
    slugify,
)
from assistant.policy import Tier


def make_workspace(root: Path, **overrides) -> Workspace:
    defaults = dict(
        name=root.name,
        root=str(root),
        danger_level=DangerLevel.LOW,
        allow_tiers=(Tier.READ,),
    )
    defaults.update(overrides)
    return Workspace(**defaults)


def write_manifest(base: Path, name: str, body: str) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{name}.toml"
    path.write_text(textwrap.dedent(body).strip() + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# The default state is the whole point
# --------------------------------------------------------------------------

def test_a_fresh_store_authorises_nothing(tmp_path):
    store = ManifestStore(tmp_path / "state")
    assert len(store) == 0
    assert store.roots() == ()


def test_a_fresh_store_covers_nothing_on_this_machine(tmp_path):
    """D10, the negative case. This is the assertion that matters.

    The assistant has no built-in knowledge that F:\\theseas, F:\\crm or any
    other project exists. It must be unable to name one without a manifest.
    """
    store = ManifestStore(tmp_path / "state")
    for name in ("theseas", "crm", "crm2"):
        with pytest.raises(NotPermitted) as exc:
            store.resolve(name)
        assert "Approved so far: none" in str(exc.value)


def test_there_is_no_discovery_method(tmp_path):
    """A guard against a future well-meaning refactor.

    Any method that walks a filesystem looking for candidate projects would make
    "only what the user approved" into "whatever the assistant recognised". The
    test is deliberately blunt about naming.
    """
    store = ManifestStore(tmp_path / "state")
    dangerous = {"discover", "scan", "autodetect", "find", "walk", "search", "guess"}
    assert dangerous.isdisjoint(set(dir(store)))


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------

def test_add_registers_and_then_resolves(tmp_path):
    store = ManifestStore(tmp_path / "state")
    project = tmp_path / "theseas"
    project.mkdir()
    store.add(make_workspace(project, language="typescript", stack=("svelte",)))

    ws = store.resolve("theseas")
    assert ws.root == str(project.resolve()).lower() or ws.root == str(project)
    assert ws.language == "typescript"
    assert ws.stack == ("svelte",)
    assert store.covers(project / "src" / "deep" / "thing.ts") is not None


def test_add_persists_across_a_restart(tmp_path):
    """A daemon that is killed must not lose the user's approvals - or gain any."""
    base = tmp_path / "state"
    project = tmp_path / "proj"
    project.mkdir()
    ManifestStore(base).add(make_workspace(project))

    reborn = ManifestStore(base)
    assert len(reborn) == 1
    assert reborn.resolve("proj").root == str(project).lower() or reborn.resolve("proj").root == str(project)


def test_add_refuses_a_root_containing_our_own_state(tmp_path):
    """A workspace that swallowed the assistant would make its own code 'approved'."""
    base = tmp_path / "state"
    store = ManifestStore(base)
    with pytest.raises(NotPermitted) as exc:
        store.add(make_workspace(tmp_path))
    assert "contains the assistant's own state directory" in str(exc.value)


def test_add_refuses_an_overlapping_root(tmp_path):
    store = ManifestStore(tmp_path / "state")
    outer = tmp_path / "projects"
    inner = outer / "theseas"
    inner.mkdir(parents=True)

    store.add(make_workspace(outer, name="projects"))
    with pytest.raises(NotPermitted) as exc:
        store.add(make_workspace(inner, name="theseas"))
    assert "overlaps existing workspace" in str(exc.value)


def test_sibling_prefix_directories_are_not_overlapping(tmp_path):
    """F:\\app must not swallow F:\\app-two. The classic startswith bug."""
    store = ManifestStore(tmp_path / "state")
    one = tmp_path / "app"
    two = tmp_path / "app-two"
    one.mkdir()
    two.mkdir()

    store.add(make_workspace(one, name="app"))
    store.add(make_workspace(two, name="app-two"))  # must not raise

    assert store.covers(two / "x") is not None
    assert store.covers(one / "x").name == "app"


def test_remove_takes_authority_away(tmp_path):
    store = ManifestStore(tmp_path / "state")
    project = tmp_path / "proj"
    project.mkdir()
    store.add(make_workspace(project))

    store.remove("proj")
    with pytest.raises(NotPermitted):
        store.resolve("proj")
    assert ManifestStore(tmp_path / "state").roots() == ()


# --------------------------------------------------------------------------
# Validation - a lying manifest buys nothing
# --------------------------------------------------------------------------

def test_a_manifest_naming_a_missing_directory_is_refused(tmp_path):
    """A not-yet-mounted drive must not become a permission that later matches
    whatever gets mounted at that path."""
    base = tmp_path / "state"
    write_manifest(
        base,
        "ghost",
        f"""
        name = "ghost"
        root = '{tmp_path / "not-mounted" / "x"}'
        """,
    )
    with pytest.raises(ManifestInvalid) as exc:
        ManifestStore(base)
    assert "not an existing directory" in str(exc.value)


def test_an_unknown_key_is_refused_rather_than_ignored(tmp_path):
    """A typo in a security-relevant file must fail loudly."""
    base = tmp_path / "state"
    project = tmp_path / "proj"
    project.mkdir()
    write_manifest(
        base,
        "typo",
        f"""
        name = "typo"
        root = '{project}'
        allow_tier = ["T3"]
        """,
    )
    with pytest.raises(ManifestInvalid) as exc:
        ManifestStore(base)
    assert "unknown key" in str(exc.value)


def test_danger_level_caps_allow_tiers(tmp_path):
    """A 'high' workspace cannot quietly declare T3."""
    base = tmp_path / "state"
    project = tmp_path / "proj"
    project.mkdir()
    write_manifest(
        base,
        "prod",
        f"""
        name = "prod"
        root = '{project}'
        danger_level = "high"
        allow_tiers = ["T0", "T1", "T2", "T3"]
        """,
    )
    with pytest.raises(ManifestInvalid) as exc:
        ManifestStore(base)
    assert "permits at most T1" in str(exc.value)


def test_an_unknown_danger_level_is_refused(tmp_path):
    base = tmp_path / "state"
    project = tmp_path / "proj"
    project.mkdir()
    write_manifest(
        base,
        "weird",
        f"""
        name = "weird"
        root = '{project}'
        danger_level = "extreme"
        """,
    )
    with pytest.raises(ManifestInvalid) as exc:
        ManifestStore(base)
    assert "danger_level must be one of" in str(exc.value)


def test_an_unknown_tier_name_is_refused(tmp_path):
    base = tmp_path / "state"
    project = tmp_path / "proj"
    project.mkdir()
    write_manifest(
        base,
        "bad",
        f"""
        name = "bad"
        root = '{project}'
        allow_tiers = ["T0", "T4"]
        """,
    )
    with pytest.raises(ManifestInvalid):
        ManifestStore(base)


def test_missing_required_key_is_refused(tmp_path):
    base = tmp_path / "state"
    project = tmp_path / "proj"
    project.mkdir()
    write_manifest(base, "noroot", 'name = "noroot"')
    with pytest.raises(ManifestInvalid) as exc:
        ManifestStore(base)
    assert "missing required key 'root'" in str(exc.value)


def test_malformed_toml_is_refused_with_the_parser_message(tmp_path):
    base = tmp_path / "state"
    base.mkdir(parents=True)
    (base / "broken.toml").write_text("name = \nroot = [", encoding="utf-8")
    with pytest.raises(ManifestInvalid) as exc:
        ManifestStore(base)
    assert "not valid TOML" in str(exc.value)


# --------------------------------------------------------------------------
# Authority is a ceiling, never a grant
# --------------------------------------------------------------------------

def test_session_ceiling_narrows_but_never_widens(tmp_path):
    store = ManifestStore(tmp_path / "state")
    project = tmp_path / "proj"
    project.mkdir()
    store.add(
        make_workspace(
            project,
            danger_level=DangerLevel.MEDIUM,
            allow_tiers=(Tier.READ, Tier.REVERSIBLE_WRITE, Tier.CONSEQUENTIAL),
        )
    )

    # Manifest allows T2, session permits only T1 -> T1, not T2.
    narrow = store.resolve("proj", session_ceiling=Tier.REVERSIBLE_WRITE)
    assert narrow.effective_tiers(Tier.REVERSIBLE_WRITE) == (Tier.READ, Tier.REVERSIBLE_WRITE)
    assert not narrow.allows(Tier.CONSEQUENTIAL)

    # A more permissive session cannot exceed what the manifest declared.
    wide = store.resolve("proj", session_ceiling=Tier.IRREVERSIBLE)
    assert wide.effective_tiers(Tier.IRREVERSIBLE) == (
        Tier.READ, Tier.REVERSIBLE_WRITE, Tier.CONSEQUENTIAL,
    )
    assert not wide.allows(Tier.IRREVERSIBLE)


def test_a_session_ceiling_below_the_manifest_refuses_rather_than_half_works(tmp_path):
    store = ManifestStore(tmp_path / "state")
    project = tmp_path / "proj"
    project.mkdir()
    store.add(make_workspace(project, allow_tiers=(Tier.CONSEQUENTIAL,)))
    with pytest.raises(NotPermitted) as exc:
        store.resolve("proj", session_ceiling=Tier.READ)
    assert "permits nothing" in str(exc.value)


def test_store_level_ceiling_applies_to_every_workspace(tmp_path):
    """A daemon started in a cautious mode is cautious everywhere."""
    store = ManifestStore(tmp_path / "state", ceiling=Tier.REVERSIBLE_WRITE)
    project = tmp_path / "proj"
    project.mkdir()
    store.add(make_workspace(project, allow_tiers=(Tier.READ, Tier.CONSEQUENTIAL)))
    assert store.resolve("proj").allow_tiers == (Tier.READ,)


# --------------------------------------------------------------------------
# Agent definitions are copied, never referenced (D10 / D21)
# --------------------------------------------------------------------------

def test_agent_definitions_are_copied_not_referenced(tmp_path):
    source = tmp_path / "theseas-agents"
    source.mkdir()
    (source / "orchestrator.md").write_text("# role", encoding="utf-8")
    (source / "reviewer.md").write_text("# role", encoding="utf-8")

    store = ManifestStore(tmp_path / "state")
    store.add(make_workspace(tmp_path / "theseas", name="theseas", agent_definitions=str(source)))

    ours = tmp_path / "state" / "roles" / "theseas"
    result = store.import_agent_definitions("theseas", ours)

    assert result == ours
    assert (ours / "orchestrator.md").exists()
    # The source still exists and was not modified - we read it, we did not
    # claim it.
    assert (source / "orchestrator.md").read_text(encoding="utf-8") == "# role"


def test_importing_from_a_workspace_without_agents_is_refused(tmp_path):
    project = tmp_path / "plain"
    project.mkdir()
    store = ManifestStore(tmp_path / "state")
    store.add(make_workspace(project, name="plain"))
    with pytest.raises(NotPermitted) as exc:
        store.import_agent_definitions("plain", tmp_path / "out")
    assert "declares no agent_definitions" in str(exc.value)


def test_import_is_idempotent_by_default(tmp_path):
    source = tmp_path / "agents"
    source.mkdir()
    (source / "a.md").write_text("v1", encoding="utf-8")

    store = ManifestStore(tmp_path / "state")
    store.add(make_workspace(tmp_path / "p", name="p", agent_definitions=str(source)))

    out = tmp_path / "out"
    store.import_agent_definitions("p", out)
    (source / "a.md").write_text("v2", encoding="utf-8")
    store.import_agent_definitions("p", out)  # no overwrite
    assert (out / "a.md").read_text(encoding="utf-8") == "v1"

    store.import_agent_definitions("p", out, overwrite=True)
    assert (out / "a.md").read_text(encoding="utf-8") == "v2"


# --------------------------------------------------------------------------
# Round trip
# --------------------------------------------------------------------------

def test_a_manifest_written_by_the_store_reads_back_identically(tmp_path):
    store = ManifestStore(tmp_path / "state")
    project = tmp_path / "proj"
    project.mkdir()
    original = make_workspace(
        project,
        danger_level=DangerLevel.MEDIUM,
        allow_tiers=(Tier.READ, Tier.REVERSIBLE_WRITE),
        language="python",
        stack=("fastapi", "postgres"),
        test_command="pytest -q",
        build_command="uv build",
        notes='a note with "quotes" and a Windows path C:\\temp',
        added="2026-01-02",
    )
    store.add(original)

    reborn = ManifestStore(tmp_path / "state")
    assert reborn.resolve("proj") == original


def test_add_stamps_the_date_when_the_caller_omits_it(tmp_path):
    """`added` is filled in by the store, so it cannot round-trip from a blank."""
    store = ManifestStore(tmp_path / "state")
    project = tmp_path / "proj"
    project.mkdir()
    blank = make_workspace(project)
    assert blank.added == ""
    assert store.add(blank).added == date.today().isoformat()


def test_duplicate_root_in_two_files_keeps_the_first(tmp_path):
    """Authority must not depend on which file the loader happened to read first."""
    base = tmp_path / "state"
    project = tmp_path / "proj"
    project.mkdir()
    write_manifest(base, "a_first", f"name = 'a'\nroot = '{project}'\nallow_tiers = ['T0']")
    write_manifest(base, "b_second", f"name = 'b'\nroot = '{project}'\nallow_tiers = ['T3']")

    store = ManifestStore(base)
    assert len(store) == 1
    assert store.resolve("a").allow_tiers == (Tier.READ,)


def test_add_twice_without_overwrite_is_refused(tmp_path):
    store = ManifestStore(tmp_path / "state")
    project = tmp_path / "proj"
    project.mkdir()
    store.add(make_workspace(project))
    with pytest.raises(NotPermitted) as exc:
        store.add(make_workspace(project))
    assert "already registered" in str(exc.value)
    # overwrite is the deliberate path back in
    assert store.add(make_workspace(project), overwrite=True).name == "proj"
    assert len(store) == 1


# --------------------------------------------------------------------------
# Slugs
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [("theseas", "theseas"), ("My Project", "my-project"), ("a.b_c", "a.b_c")],
)
def test_slugify(raw, expected):
    assert slugify(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "!!!", ".."])
def test_slugify_refuses_junk(raw):
    with pytest.raises(ManifestInvalid):
        slugify(raw)


def test_slugify_trims_punctuation_rather_than_rejecting_it():
    """Leading/trailing noise is tidied, not treated as invalid input."""
    assert slugify("-x-") == "x"


# --------------------------------------------------------------------------
# Integration with the policy gate
# --------------------------------------------------------------------------

def test_manifest_roots_are_what_the_policy_gate_is_given(tmp_path):
    """The store's only job at runtime: produce the allowed_roots list."""
    from assistant.tools import default_gate

    store = ManifestStore(tmp_path / "state")
    store.add(make_workspace(tmp_path / "proj", name="proj"))

    gate = default_gate(store.roots())
    allowed = gate.evaluate("file.read", channel="pc", args={"path": str(tmp_path / "proj" / "a.txt")})
    assert allowed.allowed

    outside = gate.evaluate(
        "file.read", channel="pc", args={"path": str(tmp_path / "elsewhere" / "a.txt")}
    )
    assert not outside.allowed
    assert outside.reason
