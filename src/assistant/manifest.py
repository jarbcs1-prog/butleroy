"""Workspace manifests: the only door to a directory the assistant did not create.

The isolation rule (D10) is not "be careful with F:". It is a *mechanism*, and
this module is that mechanism. Three properties make it one:

1. **Nothing is discovered.** There is no ``discover()`` that walks a drive, no
   config file that names a project, no registry of "known workspaces". A
   directory becomes reachable only when a human writes a manifest file and then
   asks for it to be added. The set of reachable directories is therefore a
   property of what the user has explicitly approved, and it is empty on a fresh
   install.
2. **Authority is a ceiling, not a grant.** A manifest may lower what the
   policy gate allows inside its own tree; it can never raise it. ``resolve()``
   intersects the manifest's ``allow_tiers`` with the session ceiling, so
   editing a file on disk cannot hand the assistant authority the session did
   not already have.
3. **Role definitions are copied, never referenced.** Pointing at another
   project's agent markdown is reading, and that is fine. *Running* it in place
   would couple us to their directory (D10). So ``resolve()`` materialises a
   copy under our own state directory and returns that path; the original is
   never returned to a caller that would execute it.

Note what this module deliberately does **not** do: it does not validate that
the workspace is a real project, does not run anything inside it, and does not
trust the project in any way. A manifest is a claim made by the *user* about a
directory. Everything the assistant does inside that directory is still judged
by the policy gate (see :mod:`assistant.policy`).

Format is TOML via the standard library's ``tomllib`` (Python 3.11+), not YAML.
Reason: a config file that guards a security boundary should not need a
third-party parser to be read, and TOML's subset is small enough to review by
eye. ``tomllib`` is read-only, so a manifest can never be rewritten behind the
user's back by this process.
"""

from __future__ import annotations

import os
import re
import shutil
import tomllib
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from .policy import Tier

__all__ = [
    "DangerLevel",
    "ManifestError",
    "ManifestInvalid",
    "NotPermitted",
    "Workspace",
    "ManifestStore",
    "slugify",
]


class ManifestError(Exception):
    """Base class for everything this module refuses to do."""


class ManifestInvalid(ManifestError):
    """The file exists but does not describe a workspace we will touch."""


class NotPermitted(ManifestError):
    """The request is well-formed but this manifest does not authorise it."""


class DangerLevel:
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    ALL = (LOW, MEDIUM, HIGH)
    #: A manifest may never authorise more than this. Stated here rather than
    #: left to each manifest so that "high" has one definition everywhere.
    TIER_CEILING = {
        LOW: Tier.IRREVERSIBLE,
        MEDIUM: Tier.CONSEQUENTIAL,
        HIGH: Tier.REVERSIBLE_WRITE,
    }


_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def slugify(text: str) -> str:
    """Lowercase, hyphen-separated, filesystem-safe. Empty input is an error.

    Deliberately strict rather than clever: the slug becomes a filename, and a
    slug that needs escaping to be a filename is a slug that will surprise
    someone eventually.
    """
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", text).strip("-.").lower()
    if not cleaned or not _SLUG_RE.match(cleaned):
        raise ManifestInvalid(f"cannot build a safe slug from {text!r}")
    return cleaned


def _norm(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.abspath(os.path.expanduser(str(path))))


def _is_under(child: str, parent: str) -> bool:
    """True when ``child`` is ``parent`` or lives inside it. Never raises.

    ``os.path.commonpath`` is used rather than ``str.startswith``, so
    ``F:\\project`` is not considered to contain ``F:\\project-two``. It raises
    ValueError across drives (``E:`` vs ``F:``); that is a different drive, so
    the answer is False.
    """
    try:
        common = os.path.commonpath([child, parent])
    except ValueError:
        return False
    return os.path.normcase(common) == os.path.normcase(parent)


#: Tier names as they appear in a manifest. ``Tier`` is an ``IntEnum`` whose
#: *member names* are ``READ``/``REVERSIBLE_WRITE``/... while its ``str()`` is
#: ``T0``..``T3``; manifests use the latter, so ``Tier["T0"]`` would raise.
#: Public because the daemon accepts the same spellings when a caller passes
#: ``allow_tiers`` over the transport - one table, one place to get it wrong.
TIER_BY_NAME: dict[str, Tier] = {str(t): t for t in Tier}
TIER_BY_NAME.update({t.name: t for t in Tier})


def _tier_list(names: Iterable[str]) -> tuple[Tier, ...]:
    out: list[Tier] = []
    for name in names:
        key = str(name).strip().upper()
        if key not in TIER_BY_NAME:
            raise ManifestInvalid(
                f"unknown authority tier {name!r}; expected one of "
                + ", ".join(sorted(str(t) for t in Tier))
            )
        out.append(TIER_BY_NAME[key])
    return tuple(sorted(set(out), key=lambda t: int(t)))


@dataclass(frozen=True)
class Workspace:
    """One user-approved directory, and the ceiling on what may happen there."""

    name: str
    root: str
    danger_level: str = DangerLevel.LOW
    #: Highest tier permitted in this tree *by default*. Always intersected with
    #: the session ceiling at resolve time; never a way to widen authority.
    allow_tiers: tuple[Tier, ...] = (Tier.READ,)
    language: str = ""
    stack: tuple[str, ...] = ()
    test_command: str = ""
    build_command: str = ""
    notes: str = ""
    added: str = ""
    #: Role-definition templates from the workspace. Read and copied on resolve;
    #: never executed in place. See the module docstring, property 3.
    agent_definitions: str = ""

    def allows(self, tier: Tier) -> bool:
        return tier in self.allow_tiers

    def effective_tiers(self, ceiling: Tier) -> tuple[Tier, ...]:
        """Manifest authority narrowed to the session's ceiling."""
        return tuple(t for t in self.allow_tiers if t <= ceiling)

    def describe(self) -> str:
        tiers = ", ".join(str(t) for t in self.allow_tiers) or "none"
        return (
            f"{self.name} [{self.danger_level}] {self.root}\n"
            f"  allowed here: {tiers}\n"
            f"  language: {self.language or 'unspecified'}  stack: "
            f"{', '.join(self.stack) or 'unspecified'}\n"
            f"  test: {self.test_command or 'none declared'}"
            + (f"  build: {self.build_command}" if self.build_command else "")
        )


def _parse(data: dict, *, source: Path, must_exist: bool = True) -> Workspace:
    """Turn parsed TOML into a validated Workspace, or explain why not."""
    if not isinstance(data, dict):
        raise ManifestInvalid(f"{source.name}: top level must be a table")

    unknown = set(data) - {
        "name", "root", "danger_level", "allow_tiers", "language", "stack",
        "test_command", "build_command", "notes", "added", "agent_definitions",
    }
    if unknown:
        raise ManifestInvalid(
            f"{source.name}: unknown key(s) {sorted(unknown)}; a typo in a "
            f"security-relevant file must not be silently ignored"
        )

    for key in ("name", "root"):
        if key not in data:
            raise ManifestInvalid(f"{source.name}: missing required key {key!r}")

    name = str(data["name"])
    try:
        slugify(name)
    except ManifestInvalid as exc:
        raise ManifestInvalid(f"{source.name}: {exc}") from exc

    # Keep the spelling the user typed. normcase would lowercase the whole path,
    # which is fine for comparing two paths but wrong for the one field the user
    # reads back in `describe()` and edits by hand. Every comparison in this
    # module normalises both sides itself, so nothing is lost.
    root = os.path.abspath(os.path.expanduser(str(data["root"])))
    if not os.path.isabs(root):  # pragma: no cover - _norm always absolutises
        raise ManifestInvalid(f"{source.name}: root must be absolute")
    if must_exist:
        if not os.path.isdir(root):
            raise ManifestInvalid(
                f"{source.name}: root {root!r} is not an existing directory. "
                f"If the drive is not mounted, the manifest stays unregistered "
                f"rather than becoming a permission that later matches a "
                f"different directory at the same path."
            )

    level = str(data.get("danger_level", DangerLevel.LOW)).strip().lower()
    if level not in DangerLevel.ALL:
        raise ManifestInvalid(
            f"{source.name}: danger_level must be one of "
            f"{list(DangerLevel.ALL)}, got {level!r}"
        )

    requested = _tier_list(data.get("allow_tiers", ["T0"]))
    ceiling = DangerLevel.TIER_CEILING[level]
    too_much = tuple(t for t in requested if t > ceiling)
    if too_much:
        raise ManifestInvalid(
            f"{source.name}: danger_level={level!r} permits at most "
            f"{ceiling}, but allow_tiers asks for "
            f"{', '.join(str(t) for t in too_much)}. Lower the tiers or accept "
            f"the risk knowingly by raising danger_level."
        )

    agents = str(data.get("agent_definitions", "") or "")
    if agents:
        agents = _norm(agents)
        if must_exist and not os.path.isdir(agents):
            raise ManifestInvalid(
                f"{source.name}: agent_definitions {agents!r} is not a directory"
            )

    def _str_tuple(key: str) -> tuple[str, ...]:
        raw = data.get(key, [])
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            raise ManifestInvalid(f"{source.name}: {key} must be a list of strings")
        return tuple(str(x) for x in raw)

    return Workspace(
        name=name,
        root=root,
        danger_level=level,
        allow_tiers=requested,
        language=str(data.get("language", "") or ""),
        stack=_str_tuple("stack"),
        test_command=str(data.get("test_command", "") or ""),
        build_command=str(data.get("build_command", "") or ""),
        notes=str(data.get("notes", "") or ""),
        added=str(data.get("added", "") or date.today().isoformat()),
        agent_definitions=agents,
    )


def _dump(ws: Workspace) -> str:
    """Serialise to TOML by hand.

    Not a general TOML writer and does not try to be: it emits only the schema
    this module defines, with every string quoted and escaped, so the output
    round-trips through :func:`tomllib` exactly. ``tomllib`` cannot write, and
    pulling in a writer library to persist eleven fields would be a poor trade
    for a security boundary.

    Strings are emitted as TOML *literal* strings (single quotes) whenever they
    can be, so ``root = 'F:\\theseas'`` appears as written. In a TOML basic
    string a bare backslash starts an escape, which makes the natural way to
    type a Windows path -- ``root = "F:\\theseas"`` -- either silently wrong
    (``\\t``, ``\\r``, ``\\U`` are escapes) or a cryptic ``Invalid hex value``.
    A manifest is a file a human edits by hand, so the form on disk has to be
    the form a human would have written. Single quotes and control characters
    fall back to an escaped basic string.
    """
    def q(value: str) -> str:
        if "'" not in value and not any(c in value for c in "\n\r\t"):
            return f"'{value}'"
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        escaped = escaped.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
        return f'"{escaped}"'

    def arr(values: Sequence[str]) -> str:
        return "[" + ", ".join(q(v) for v in values) + "]"

    lines = [
        f"name = {q(ws.name)}",
        f"root = {q(ws.root)}",
        f"danger_level = {q(ws.danger_level)}",
        f"allow_tiers = {arr([str(t) for t in ws.allow_tiers])}",
    ]
    if ws.language:
        lines.append(f"language = {q(ws.language)}")
    if ws.stack:
        lines.append(f"stack = {arr(ws.stack)}")
    if ws.test_command:
        lines.append(f"test_command = {q(ws.test_command)}")
    if ws.build_command:
        lines.append(f"build_command = {q(ws.build_command)}")
    if ws.agent_definitions:
        lines.append(f"agent_definitions = {q(ws.agent_definitions)}")
    if ws.notes:
        lines.append(f"notes = {q(ws.notes)}")
    lines.append(f"added = {q(ws.added)}")
    return "\n".join(lines) + "\n"


class ManifestStore:
    """The user's approved workspaces. Empty until the user says otherwise.

    Construction does not read the directory. :meth:`add` does not scan for
    candidates. There is deliberately no method that walks a drive looking for
    projects, because the moment one exists, "the assistant may only touch what
    you approved" becomes "the assistant may touch whatever it recognises".
    """

    def __init__(self, base: str | os.PathLike[str], *, ceiling: Tier = Tier.IRREVERSIBLE):
        self.base = Path(base)
        self.base.mkdir(parents=True, exist_ok=True)
        self.ceiling = ceiling
        self._workspaces: dict[str, Workspace] = {}
        self._load_all()

    # -- reading ---------------------------------------------------------
    def _load_all(self) -> None:
        for path in sorted(self.base.glob("*.toml")):
            ws = self._read(path)
            if ws is not None:
                # First file wins on duplicate roots. A shadowing manifest would
                # let a later file quietly widen or narrow an earlier one.
                self._workspaces.setdefault(ws.root, ws)

    def _read(self, path: Path) -> Workspace | None:
        try:
            with path.open("rb") as handle:
                data = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise ManifestInvalid(
                f"{path.name}: not valid TOML: {exc}\n"
                "  If a path is at fault, the backslashes are the problem: in a "
                'double-quoted TOML string "\\p" starts an escape. Write Windows '
                "paths in single quotes, e.g. root = 'F:\\theseas', or use "
                'forward slashes, e.g. root = "F:/theseas".'
            ) from exc
        except OSError as exc:
            raise ManifestInvalid(f"{path.name}: cannot be read: {exc}") from exc
        return _parse(data, source=path)

    def __len__(self) -> int:
        return len(self._workspaces)

    def __iter__(self) -> Iterator[Workspace]:
        return iter(self._workspaces.values())

    def __contains__(self, name: object) -> bool:
        return any(ws.name == name for ws in self._workspaces.values())

    def get(self, name: str) -> Workspace | None:
        for ws in self._workspaces.values():
            if ws.name == name:
                return ws
        return None

    def roots(self) -> tuple[str, ...]:
        """Directories the policy gate should treat as approved. Empty by default."""
        return tuple(ws.root for ws in self._workspaces.values())

    def covers(self, path: str | os.PathLike[str]) -> Workspace | None:
        """The workspace containing ``path``, or None if it is not approved."""
        target = _norm(path)
        best: Workspace | None = None
        for ws in self._workspaces.values():
            if _is_under(target, ws.root):
                if best is None or len(ws.root) > len(best.root):
                    best = ws  # most specific match wins
        return best

    # -- the one mutation -------------------------------------------------
    def add(self, ws: Workspace, *, overwrite: bool = False) -> Workspace:
        """Register a workspace the user has approved. Writes the file.

        Refuses two things by default: a root that contains the assistant's own
        installation (a workspace that swallowed us would make our own code
        reachable as "approved"), and a second workspace with an overlapping
        root (which would make authority depend on iteration order).
        """
        if _is_under(self.base, ws.root):
            raise NotPermitted(
                f"{ws.name}: root {ws.root!r} contains the assistant's own "
                f"state directory {str(self.base)!r}. Refusing, because that "
                f"would make our own code reachable as an approved workspace."
            )
        identical = next(
            (w for w in self._workspaces.values() if _norm(w.root) == _norm(ws.root)),
            None,
        )
        if identical is not None and not overwrite:
            # Re-adding the same directory. Distinct from an overlap: the right
            # verb is "already", not "overlaps", and the name tells the user
            # which manifest to edit. overwrite=True is the deliberate way in.
            raise NotPermitted(
                f"{ws.name}: root {ws.root!r} is already registered as "
                f"{identical.name!r}. Pass overwrite=True to replace it, or "
                f"remove() it first."
            )
        for existing in self._workspaces.values():
            if existing is identical:
                continue  # the identical root is handled above, not as an overlap
            if _is_under(ws.root, existing.root) or _is_under(existing.root, ws.root):
                raise NotPermitted(
                    f"{ws.name}: root {ws.root!r} overlaps existing workspace "
                    f"{existing.name!r} ({existing.root!r}). Overlapping roots "
                    f"make authority depend on which file is read first."
                )

        path = self.base / f"{slugify(ws.name)}.toml"
        if path.exists() and not overwrite:
            raise ManifestInvalid(
                f"{ws.name} already registered at {path.name}; pass "
                f"overwrite=True to replace it deliberately"
            )
        if not ws.added:
            ws = replace(ws, added=date.today().isoformat())
        path.write_text(_dump(ws), encoding="utf-8")
        self._workspaces[ws.root] = ws
        return ws

    def remove(self, name: str) -> None:
        ws = self.get(name)
        if ws is None:
            raise NotPermitted(f"no workspace named {name!r} is registered")
        path = self.base / f"{slugify(name)}.toml"
        if path.exists():
            path.unlink()
        self._workspaces.pop(ws.root, None)

    # -- resolution -------------------------------------------------------
    def resolve(self, name: str, *, session_ceiling: Tier | None = None) -> Workspace:
        """The approved workspace called ``name``, or raise.

        This is the only supported way for the rest of the program to obtain a
        workspace. It exists as a function so that "I need a project" always
        goes through a place where refusal is possible.
        """
        ws = self.get(name)
        if ws is None:
            known = ", ".join(sorted(w.name for w in self._workspaces)) or "none"
            raise NotPermitted(
                f"no workspace named {name!r} is approved. Approved so far: "
                f"{known}. The assistant does not search for projects; add one "
                f"explicitly."
            )
        ceiling = self.ceiling if session_ceiling is None else session_ceiling
        effective = ws.effective_tiers(min(ceiling, DangerLevel.TIER_CEILING[ws.danger_level]))
        if not effective:
            raise NotPermitted(
                f"{ws.name}: the session ceiling {ceiling} permits nothing this "
                f"workspace's manifest allows ({', '.join(str(t) for t in ws.allow_tiers)})"
            )
        return replace(ws, allow_tiers=effective)

    def import_agent_definitions(
        self, name: str, destination: str | os.PathLike[str], *, overwrite: bool = False
    ) -> Path:
        """Copy a workspace's role definitions into our own tree.

        Reading another project's agent markdown is legitimate. *Running* it in
        place would be a runtime dependency on their directory, which D10
        forbids. So the copy is the thing that is returned, and the source is
        never handed back to a caller that would use it as a live path.
        """
        ws = self.resolve(name)
        if not ws.agent_definitions:
            raise NotPermitted(
                f"{ws.name}: manifest declares no agent_definitions directory"
            )
        dest = Path(destination)
        dest.mkdir(parents=True, exist_ok=True)
        copied = 0
        skipped = 0
        for item in sorted(Path(ws.agent_definitions).glob("*.md")):
            target = dest / item.name
            if target.exists() and not overwrite:
                skipped += 1
                continue
            shutil.copy2(item, target)
            copied += 1
        if copied == 0 and skipped == 0:
            # Only an empty source is an error. Re-importing without overwrite is
            # a successful no-op, not a failure: conflating the two makes the
            # idempotent case impossible to distinguish from a broken manifest.
            raise NotPermitted(
                f"{ws.name}: no .md files under {ws.agent_definitions}"
            )
        return dest
