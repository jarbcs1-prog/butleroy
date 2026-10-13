"""
The Obsidian bridge (D14).

E:\\Obsidian_Vault is the human-readable substrate for the episodic and reflective
memory tiers. Direct file I/O only - no UI automation, no plugin, no API. Obsidian
picks up the files because they are files.

Why this module is written as a jail rather than a convenience wrapper
-------------------------------------------------------------------
The vault is 7.5 GB and 53,492 markdown files. 53,064 of them are a training
corpus under `datasets/`, and `.smart-env/` is another 1.29 GB. Those are not
merely too big to index; they are *wrong* to index, and an index of them would
be actively harmful - the assistant would start answering questions about a
training corpus as though it were the user's knowledge.

More importantly: this is the only component in the project that writes to
something the user also writes to, by hand, in an editor that is probably open
right now. A bug here does not produce an exception, it produces a corrupted
note the user discovers tomorrow. So:

  1. Writes are confined to ONE directory, `Assistant/`, and the confinement is
     enforced by resolving every path and re-checking containment at the moment
     of the write - not by trusting the caller.
  2. Reads are an explicit allowlist of directories. There is no "read
     everything under the vault" path, and no way to pass a pattern.
  3. `datasets/` and `.smart-env/` are hard-excluded by name, in addition to
     simply not being on the allowlist. Defence in depth for the one mistake
     that would be unrecoverable.
  4. Writes are atomic (temp file + os.replace), because a daemon that is
     killed mid-write must not leave a half-written note in a folder the user
     is reading.
  5. Every write is reversible. `snapshot()` before a bulk operation,
     `rollback()` after. The vault has no community plugins installed, so
     there is no Obsidian Git safety net underneath us. We are the safety net.

The exclusion list is not a performance optimisation to be tuned. It is a
property of the system, and `tests/test_obsidian.py` asserts it from the
attacker's side.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

DEFAULT_VAULT = Path(r"E:\ButlerOY")


def _repo_state_dir() -> Path:
    """The state directory beside this checkout. Computed, not imported:
    `daemon` already imports this module, so importing `daemon.STATE` here
    would be circular."""
    return Path(__file__).resolve().parent.parent.parent / "state"


def user_vault_config(state_dir: Path | str | None = None) -> Path:
    """Where the settings Vault tab stages the choice."""
    return Path(state_dir) / "vault.json" if state_dir else _repo_state_dir() / "vault.json"


def resolve_vault_root(
    state_dir: Path | str | None = None,
    cli_vault: Path | str | None = None,
    default: Path | str | None = DEFAULT_VAULT,
) -> Path | None:
    """Where the vault lives: CLI wins, then `state/vault.json`, then default.

    Anything that is not an existing directory is skipped, and a broken
    vault.json is ignored rather than fatal: the bridge degrades to
    unavailable either way, and a diagnostics screen that dies because a
    JSON file has a typo is useless exactly when it is most wanted.
    """
    if cli_vault:
        return Path(cli_vault)
    try:
        raw = json.loads(user_vault_config(state_dir).read_text(encoding="utf-8"))
        root = Path(raw.get("root", ""))
        if root.is_dir():
            return root
    except Exception:  # noqa: BLE001 - broken config degrades, never stops
        pass
    if default is not None and Path(default).is_dir():
        return Path(default)
    return None

#: The one directory we are permitted to write to.
ASSISTANT_DIR = "Assistant"

#: Readable, never written. `evernote` and `recovery` are real user notes;
#: `.opencode` is read as role *templates* (D10 - copied, never referenced in
#: place); the rest are supporting material.
READABLE_DIRS: tuple[str, ...] = (
    "evernote",
    ".opencode",
    "recovery",
    "execution_plans",
    "copilot",
    "docs",
    "training",
)

#: Never read, never written, never indexed. Not a size judgement - a
#: correctness judgement. See the module docstring.
HARD_EXCLUDED: tuple[str, ...] = ("datasets", ".smart-env")

#: Subdirectories of Assistant/, created on demand.
SUBDIRS: dict[str, str] = {
    "journal": "journal.jsonl",
    "plans": "plan",
    "decisions": "decisions.jsonl",
    "capabilities": "capability",
    "reflection": "reflection",
}

#: Refuse to walk a read scope larger than this. A directory that grows past
#: it is a signal that the allowlist is wrong, not that we should try harder.
MAX_INDEX_FILES = 5000
MAX_READ_BYTES = 2_000_000
MAX_WRITE_BYTES = 5_000_000

_SLUG_RE = re.compile(r"[^a-z0-9._-]+")
_UNSAFE_NAME = re.compile(r'[<>:"|?*\x00-\x1f]')


class VaultError(RuntimeError):
    """Anything wrong with the vault or our relationship to it."""


class WriteRefused(VaultError):
    """A write was attempted outside `Assistant/`. Always a bug in us."""


class ReadRefused(VaultError):
    """A read was attempted from a directory that is not on the allowlist."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(text: str, max_len: int = 80) -> str:
    """Filesystem-safe slug. Windows forbids `<>:"/\\|?*` and control chars;
    several of those appear in ordinary prose, so this is not paranoia."""
    s = str(text).strip().lower()
    s = _UNSAFE_NAME.sub(" ", s)
    s = _SLUG_RE.sub("-", s).strip("-._")
    if not s:
        raise VaultError("cannot build a filename from an empty title")
    return s[:max_len].rstrip("-._") or "untitled"


def _is_under(child: Path, parent: Path) -> bool:
    """Containment test that is correct on Windows.

    `os.path.commonpath` is the right primitive here: a string prefix test
    would make `E:\\Obsidian_Vault-old` count as inside `E:\\Obsidian_Vault`, and
    `E:\\Obsidian_Vault\\datasets` would satisfy a naive "starts with" check on
    a path we are trying to refuse. Raises ValueError across drives, which we
    treat as "not under".
    """
    try:
        return os.path.commonpath([str(child), str(parent)]) == str(parent)
    except ValueError:
        return False


@dataclass(frozen=True)
class Note:
    """One indexed note. Read-only - the bridge never hands out a writable path."""

    path: Path
    rel: str
    size: int
    mtime: float

    @property
    def name(self) -> str:
        return self.path.name

    def read(self, max_bytes: int = MAX_READ_BYTES) -> str:
        n = self.path.stat().st_size
        if n > max_bytes:
            raise VaultError(
                f"{self.rel} is {n} bytes, over the {max_bytes} byte read limit. "
                "This is a signal the index scope is wrong, not a file to skip."
            )
        with self.path.open("r", encoding="utf-8", errors="replace") as fh:
            return fh.read()


@dataclass
class Snapshot:
    """A reversible record of one directory's contents."""

    taken: str
    directory: str
    files: list[str] = field(default_factory=list)
    root: str = ""
    count: int = 0

    def summary(self) -> str:
        return f"{self.count} file(s) in {self.directory} @ {self.taken}"


class ObsidianBridge:
    """File I/O against the vault. Construct once, keep for the daemon's life.

    Construction is deliberately cheap and does not touch the filesystem, so a
    missing or unplugged vault does not stop the daemon from starting - the
    assistant has a live tier that does not depend on any of this.
    """

    def __init__(
        self,
        vault: Path | str = DEFAULT_VAULT,
        *,
        readable: Iterable[str] = READABLE_DIRS,
        excluded: Iterable[str] = HARD_EXCLUDED,
        create: bool = True,
    ) -> None:
        self.vault = Path(vault)
        self.readable = tuple(readable)
        self.excluded = tuple(excluded)

        overlap = set(self.readable) & set(self.excluded)
        if overlap:
            # A directory that is both readable and excluded is a contradiction
            # that would silently resolve in favour of whoever was listed last.
            raise VaultError(f"directories both readable and excluded: {sorted(overlap)}")

        self.assistant_root = self.vault / ASSISTANT_DIR
        if create:
            self._ensure_layout()

    # ------------------------------------------------------------------ layout
    def _ensure_layout(self) -> None:
        try:
            self.assistant_root.mkdir(parents=True, exist_ok=True)
            for sub in SUBDIRS:
                (self.assistant_root / sub).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise VaultError(
                f"cannot create {self.assistant_root}: {exc}. "
                "Is the vault on a drive that is mounted?"
            ) from exc

    @property
    def available(self) -> bool:
        """True if the vault is reachable right now. Cheap; safe to poll."""
        return self.vault.is_dir()

    def describe(self) -> str:
        lines = [
            f"vault     : {self.vault}  ({'present' if self.available else 'MISSING'})",
            f"write root: {self.assistant_root}",
            f"readable  : {', '.join(self.readable)}",
            f"excluded  : {', '.join(self.excluded)}",
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------- containment
    def _write_path(self, *parts: str) -> Path:
        """Resolve a write target and prove it is inside Assistant/.

        Every write goes through here. The check is repeated here rather than
        trusted from the caller because the caller is the component most likely
        to be wrong - a model-chosen filename, a slug built from a note title,
        a path assembled from three pieces in three different modules.

        Two independent defences, because the obvious one is not enough:

        1. The caller is expected to have slugified the name, which neutralises
           `..` and path separators outright. That is the first line, and it is
           in `write_note`, not here.
        2. This containment check is the second line, and it must hold even if
           the first is bypassed.

        The symlink guard deliberately does NOT test `.exists()` first. On
        Windows a symlink whose target does not exist reports
        `exists() == False` but `is_symlink() == True` - and a *dangling*
        symlink is precisely what one would plant to redirect a write, since
        the target need not exist at plant time. Guarding with `exists()`
        would skip exactly the case worth catching.
        """
        candidate = self.assistant_root.joinpath(*parts)
        lexical = Path(os.path.abspath(os.path.normpath(candidate)))

        if lexical.is_symlink():
            raise WriteRefused(f"refusing to write through a symlink: {lexical}")

        # `resolve()` also follows any symlink in the *parent* chain, so a
        # junction planted at Assistant/journal/ is caught here even though the
        # leaf itself is a plain file.
        resolved = lexical.resolve()
        if not _is_under(resolved, self.assistant_root.resolve()):
            raise WriteRefused(
                f"refusing to write outside {ASSISTANT_DIR}/: {candidate} "
                f"resolves to {resolved}"
            )
        return lexical

    def _read_path(self, *parts: str) -> Path:
        """Resolve a read target and prove it is on the allowlist."""
        rel = Path(*parts)
        top = rel.parts[0] if rel.parts else ""
        if top == ASSISTANT_DIR:
            # Checked before the allowlist so the message names the real
            # mistake. `Assistant/` is not readable via raw paths at all -
            # it has its own accessors, which enforce the write rules.
            raise ReadRefused(
                f"read Assistant/ through its own accessors, not raw paths"
            )
        if top in self.excluded:
            raise ReadRefused(
                f"{top}/ is hard-excluded (D14). It is not read, not indexed, "
                "and not written. This is not a size limit that can be raised."
            )
        if top not in self.readable:
            raise ReadRefused(
                f"{top}/ is not on the read allowlist. Readable: "
                f"{', '.join(self.readable)}"
            )
        resolved = Path(os.path.abspath(os.path.normpath(self.vault / rel)))
        if not _is_under(resolved, self.vault):
            raise ReadRefused(f"refusing to read outside the vault: {resolved}")
        if any(part in self.excluded for part in resolved.parts):
            raise ReadRefused(f"path traverses an excluded directory: {resolved}")
        return resolved

    # ------------------------------------------------------------------ writes
    def write_note(
        self,
        subdir: str,
        title: str,
        body: str,
        *,
        front_matter: dict | None = None,
        overwrite: bool = False,
    ) -> Path:
        """Write a note under `Assistant/<subdir>/`. Returns the path written.

        `overwrite=False` is the default because a note that already exists is
        usually a note the user or a previous session wrote deliberately, and
        silently replacing it is how an assistant destroys something.
        """
        if subdir not in SUBDIRS:
            raise VaultError(
                f"unknown Assistant subdirectory {subdir!r}. "
                f"Allowed: {', '.join(SUBDIRS)}"
            )
        fname = slugify(title)
        target = self._write_path(subdir, f"{fname}.md")
        if target.exists() and not overwrite:
            target = self._write_path(subdir, f"{fname}-{_stamp()}.md")

        if front_matter:
            fm = "\n".join(
                f"{k}: {json.dumps(v) if not isinstance(v, str) else v}"
                for k, v in front_matter.items()
            )
            text = f"---\n{fm}\n---\n\n{body.strip()}\n"
        else:
            text = f"{body.strip()}\n"

        self._atomic_write(target, text.encode("utf-8"))
        return target

    def append_jsonl(self, subdir: str, record: dict, *, filename: str | None = None) -> Path:
        """Append one JSON object as a line. Used by the episodic tier.

        Append-only, fsync'd. A 24/7 daemon that is hard-killed must not lose
        the tail of its journal - that tail is often the record of what went
        wrong on the way down.
        """
        if subdir not in SUBDIRS:
            raise VaultError(f"unknown Assistant subdirectory {subdir!r}")
        name = filename or SUBDIRS[subdir]
        if not name.endswith(".jsonl"):
            raise VaultError(f"append_jsonl only writes .jsonl, not {name!r}")
        target = self._write_path(subdir, name)
        target.parent.mkdir(parents=True, exist_ok=True)

        line = json.dumps(record, sort_keys=True, default=str) + "\n"
        data = line.encode("utf-8")
        if len(data) > MAX_WRITE_BYTES:
            raise VaultError(f"record is {len(data)} bytes, over the write limit")
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_BINARY, 0o600)
        try:
            written = 0
            while written < len(data):
                written += os.write(fd, data[written:])
            os.fsync(fd)
        finally:
            os.close(fd)
        return target

    def _atomic_write(self, target: Path, data: bytes) -> None:
        """Write via temp file + os.replace.

        A reader - Obsidian, a text editor, the user's own eyes - must never
        see a half-written note. os.replace is atomic within a volume on NTFS.
        """
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp-", suffix=".md")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        except BaseException:
            # Never leave debris in a folder the user is looking at.
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def read_journal(self, subdir: str = "journal", *, limit: int | None = None) -> list[dict]:
        """Read a JSONL file back, skipping unparseable lines.

        Skipping rather than raising is deliberate and is also why the audit
        chain in `audit.py` carries its own integrity check: a torn final line
        is a crash artefact, not corruption, and must not hide the records
        before it.
        """
        name = SUBDIRS.get(subdir, "journal.jsonl")
        target = self._write_path(subdir, name)
        if not target.exists():
            return []
        out: list[dict] = []
        with target.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        if limit is not None:
            return out[-limit:]
        return out

    # ------------------------------------------------------------------- reads
    def iter_notes(self, subdir: str, *, suffix: str = ".md") -> Iterator[Note]:
        """Walk one allowlisted directory. Yields Note objects, not paths."""
        root = self._read_path(subdir)
        if not root.is_dir():
            return
        count = 0
        for dirpath, dirnames, filenames in os.walk(root):
            # Prune excluded directories even if one somehow appears under an
            # allowlisted parent. Cheap, and it makes the guarantee local.
            dirnames[:] = [
                d for d in dirnames
                if d not in self.excluded and not d.startswith(".obsidian")
            ]
            for fn in sorted(filenames):
                if suffix and not fn.endswith(suffix):
                    continue
                p = Path(dirpath) / fn
                try:
                    st = p.stat()
                except OSError:
                    continue
                count += 1
                if count > MAX_INDEX_FILES:
                    raise VaultError(
                        f"{subdir}/ yields more than {MAX_INDEX_FILES} files. "
                        "The read allowlist is wrong; fix it rather than "
                        "raising the limit."
                    )
                yield Note(
                    path=p,
                    rel=str(p.relative_to(self.vault)),
                    size=st.st_size,
                    mtime=st.st_mtime,
                )

    def index(self) -> list[Note]:
        """Every readable note, sorted by mtime descending (newest first).

        This is the structural memory tier's candidate set. It is *not* an
        embedding index - that comes later with sqlite-vec. What matters now is
        that the candidate set is provably small, provably excludes the corpus,
        and provably does not include the system's volatile state.
        """
        notes: list[Note] = []
        for sub in self.readable:
            notes.extend(self.iter_notes(sub))
        notes.sort(key=lambda n: n.mtime, reverse=True)
        return notes

    def index_stats(self) -> dict:
        """Counts per directory, so a surprise is visible before it is a problem."""
        stats: dict[str, dict] = {}
        total = 0
        for sub in self.readable:
            try:
                notes = list(self.iter_notes(sub))
            except VaultError as exc:
                stats[sub] = {"error": str(exc)}
                continue
            stats[sub] = {
                "files": len(notes),
                "bytes": sum(n.size for n in notes),
            }
            total += len(notes)
        return {
            "vault": str(self.vault),
            "available": self.available,
            "per_dir": stats,
            "total_files": total,
            "excluded": list(self.excluded),
        }

    def role_templates(self) -> list[Note]:
        """`.opencode/agents/*.md` as *templates* (D10).

        The caller must copy what it wants into its own state directory via
        `manifest.import_agent_definitions`. Returning a path into someone
        else's project and then executing it in place would be exactly the
        runtime coupling D10 forbids.
        """
        return list(self.iter_notes(os.path.join(".opencode", "agents")))

    # ---------------------------------------------------------------- rollback
    def snapshot(self, subdir: str) -> Snapshot:
        """Record what is in an Assistant/ subdirectory, for later rollback.

        The vault has no community plugins installed, so there is no Obsidian
        Git underneath us. If a bulk operation goes wrong, this is the only way
        back. The snapshot is a manifest of names and sizes, not a copy - the
        notes are ours and regenerable, and a 7.5 GB vault should not grow a
        second copy of itself because of us.
        """
        if subdir not in SUBDIRS:
            raise VaultError(f"unknown Assistant subdirectory {subdir!r}")
        root = self._write_path(subdir)
        snap = Snapshot(taken=_now(), directory=subdir, root=str(root))
        if root.is_dir():
            for dirpath, _dirs, filenames in os.walk(root):
                for fn in filenames:
                    fp = Path(dirpath) / fn
                    try:
                        size = fp.stat().st_size
                    except OSError:
                        continue
                    snap.files.append(str(fp.relative_to(root)))
                    snap.count += 1
        return snap

    def rollback(self, snap: Snapshot) -> dict:
        """Delete files created since `snap`, restoring that exact file set.

        Only ever removes files under Assistant/. A file that existed before the
        snapshot is never touched, so rolling back a failed operation cannot
        destroy a note from a previous session.
        """
        root = Path(snap.root)
        if not _is_under(root, self.assistant_root):
            raise WriteRefused(f"refusing to roll back outside Assistant/: {root}")
        if not root.is_dir():
            return {"removed": 0, "kept": 0, "note": "directory is gone"}

        before = set()
        for dirpath, _dirs, filenames in os.walk(root):
            for fn in filenames:
                fp = Path(dirpath) / fn
                before.add(str(fp.relative_to(root)))

        keep = set(snap.files)
        removed = 0
        for rel in sorted(before - keep):
            fp = root / rel
            try:
                fp.unlink()
                removed += 1
            except OSError:
                pass
        return {
            "removed": removed,
            "kept": len(keep),
            "note": f"rolled back to snapshot taken {snap.taken}",
        }


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def today() -> str:
    return date.today().isoformat()
