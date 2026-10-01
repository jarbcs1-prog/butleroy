"""File operations that change things, and the rules that make them safe.

Why this is a module and not more methods on the Daemon
------------------------------------------------------
`daemon.py` is already about nineteen hundred lines, and these eight operations
carry real logic - glob expansion, collision handling, backups, tree walks. Logic
that only makes sense inside a Daemon cannot be tested without a Daemon, and a
file operation is exactly the kind of thing worth testing on its own.

The rules, all of them from something that went wrong or nearly did
------------------------------------------------------------------
**Nothing is ever overwritten silently.** `shutil.copy2` and `shutil.move` both
overwrite by default, without a word. A copy aimed at a path that already holds
something else is a lost file that the user believes was copied, and the only
evidence would be a missing file somewhere neither of us looked. So every
destination that exists is a refusal, naming what is there.

**Every path is resolved once, and both spellings are reported.** A junction, a
symlink, or a `..` segment can make the string the model gave point somewhere
other than where it will act. The caller gets `proposed` and `resolved` and can
see when they differ - which is the same rule D32 puts on the delete dialog, and
it is there for the same reason.

**Overwriting a file is backed up, not just written.** `file.edit` is classified
T1 *reversible write*, which is only honest if the previous contents survive. It
used to be described as editing "a file under version control", which was false
of most files on this machine and would have made an irreversible overwrite look
reversible. So the executor copies the old content into the quarantine first and
reports where it went. Now the tier is true.

**A dry run is a first-class answer, not an afterthought.** `bulk_rename` can
touch a hundred files at once, so `dry_run` defaults to true there - and returns
the complete list of renames without touching anything. Nothing irreversible
should require a separate code path to be safe.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import shutil
import time
import zipfile
from pathlib import Path
from typing import Any

__all__ = [
    "FileOpError",
    "BULK_RENAME_CAP",
    "COPY_TREE_CAP_ENTRIES",
    "ARCHIVE_MAX_BYTES",
    "metadata",
    "preview",
    "search",
    "edit",
    "copy",
    "move",
    "archive",
    "bulk_rename",
]

# How many files one bulk rename may touch before it stops being a rename and
# starts being a data migration. Reported rather than refused: the user is told
# the number and decides.
BULK_RENAME_CAP = 200

# Ceilings on the expensive ones, so a request cannot wedge the daemon.
COPY_TREE_CAP_ENTRIES = 50_000
ARCHIVE_MAX_BYTES = 2 * 1024 ** 3      # 2 GiB

PREVIEW_MAX_BYTES = 200_000
SEARCH_MAX_BYTES = 2_000_000
SEARCH_MAX_MATCHES = 200


class FileOpError(RuntimeError):
    """An operation refused. The message is what a human will read."""


def _resolve(path: str) -> Path:
    """Resolve once, and return both spellings.

    `resolve()` follows junctions and symlinks, which is exactly why the
    resolved form has to be the one acted on *and* the one shown. Acting on the
    unresolved form and displaying the resolved one is the D32 redirection bug.
    """
    raw = Path(str(path)).expanduser()
    try:
        return raw.resolve()
    except OSError:
        return raw.absolute()


def _both(proposed: str) -> dict[str, str]:
    resolved = _resolve(proposed)
    return {"proposed": str(proposed), "resolved": str(resolved)}


def _refuse_overwrite(dest: Path, verb: str) -> None:
    """The single most important rule in this module."""
    if dest.exists() or dest.is_symlink():
        kind = "directory" if dest.is_dir() else "file"
        raise FileOpError(
            f"{verb}: {dest} already exists as a {kind}, and this will not "
            "overwrite it. Choose another destination, or ask the user what "
            "should happen to what is already there."
        )


def _is_inside(child: Path, parent: Path) -> bool:
    """Containment, by path parts rather than by string prefix.

    String comparison says `C:\\work-old` is inside `C:\\work`, which is how a
    rename ends up somewhere the user never approved.
    """
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


# --- reading ----------------------------------------------------------------


def metadata(path: str) -> dict[str, Any]:
    """Size, dates, type and content hash. A read; nothing is opened for writing."""
    info = _both(path)
    target = Path(info["resolved"])
    if not target.exists():
        raise FileOpError(f"no such file or directory: {target}")
    stat = target.stat()
    out: dict[str, Any] = {
        **info,
        "kind": "dir" if target.is_dir() else "file",
        "size_bytes": stat.st_size,
        "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
        "created": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_ctime)),
    }
    if target.is_symlink():
        out["symlink"] = True
    if target.is_file():
        # Hashed in chunks: a multi-gigabyte file must not be read into memory to
        # be identified, and it must not be read at all if the caller only wanted
        # the size.
        digest = hashlib.sha256()
        read = 0
        with target.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
                read += len(chunk)
                if read > ARCHIVE_MAX_BYTES:
                    out["sha256"] = "(not hashed: larger than the 2 GiB limit)"
                    out["hashed_bytes"] = read
                    break
            else:
                out["sha256"] = digest.hexdigest()
                out["hashed_bytes"] = read
    if target.is_dir():
        entries = list(target.iterdir())
        out["entries"] = len(entries)
        out["dirs"] = sum(1 for e in entries if e.is_dir())
        out["files"] = out["entries"] - out["dirs"]
    return out


def preview(path: str, max_bytes: int = PREVIEW_MAX_BYTES) -> dict[str, Any]:
    """The first `max_bytes` of a text file, or an honest refusal.

    No PDF or image rendering: those are Phase 5 and pretending otherwise would
    hand back mojibake with a confident label.
    """
    info = _both(path)
    target = Path(info["resolved"])
    if not target.is_file():
        raise FileOpError(f"not a file: {target}")
    size = target.stat().st_size
    limit = max(1, min(int(max_bytes), PREVIEW_MAX_BYTES))
    data = target.open("rb").read(limit)
    text = data.decode("utf-8", errors="replace")
    binary = b"\x00" in data[:8192]
    return {
        **info,
        "size_bytes": size,
        "returned_bytes": len(data),
        "truncated": size > len(data),
        "binary": binary,
        "text": "" if binary else text,
        "note": ("looks binary - no text preview" if binary else ""),
    }


def search(path: str, query: str, max_bytes: int = SEARCH_MAX_BYTES) -> dict[str, Any]:
    """Find `query` in the text files under `path`.

    Bounded on every axis - files scanned, bytes read, matches returned - because
    an unbounded grep over `C:\\` is a denial of service against the user's own
    machine.
    """
    info = _both(path)
    root = Path(info["resolved"])
    if not root.exists():
        raise FileOpError(f"no such path: {root}")
    if not query:
        raise FileOpError("search needs something to look for")
    limit = max(1, min(int(max_bytes), SEARCH_MAX_BYTES))
    needle = query.lower()

    matches: list[dict[str, Any]] = []
    scanned = 0
    read_bytes = 0
    skipped_binary = 0
    truncated = False

    targets = [root] if root.is_file() else sorted(root.rglob("*"))
    for candidate in targets:
        if len(matches) >= SEARCH_MAX_MATCHES:
            truncated = True
            break
        if read_bytes >= limit:
            truncated = True
            break
        try:
            if not candidate.is_file():
                continue
            size = candidate.stat().st_size
            if size > limit:
                continue
            data = candidate.open("rb").read()
            read_bytes += len(data)
            scanned += 1
        except OSError:
            continue
        if b"\x00" in data[:8192]:
            skipped_binary += 1
            continue
        text = data.decode("utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), start=1):
            if needle in line.lower():
                matches.append({
                    "path": str(candidate), "line": number,
                    "text": line.strip()[:200],
                })
                if len(matches) >= SEARCH_MAX_MATCHES:
                    truncated = True
                    break

    return {
        **info, "query": query, "matches": matches, "count": len(matches),
        "files_scanned": scanned, "bytes_read": read_bytes,
        "skipped_binary": skipped_binary, "truncated": truncated,
        "measured": True,
    }


# --- writing ----------------------------------------------------------------


def edit(path: str, content: str, *, backup_dir: Path | None = None) -> dict[str, Any]:
    """Replace a file's contents, keeping the previous version.

    The backup is the whole reason this is honestly a T1. Without it, "replace
    the contents of a file" is an irreversible act wearing a reversible label.
    """
    info = _both(path)
    target = Path(info["resolved"])
    body = content if isinstance(content, str) else str(content)
    if target.is_dir():
        raise FileOpError(f"{target} is a directory, not a file")

    existed = target.exists()
    previous_size = target.stat().st_size if existed else 0
    backup = None
    if existed and backup_dir is not None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        folder = Path(backup_dir) / f"edit-{stamp}"
        folder.mkdir(parents=True, exist_ok=True)
        backup = folder / target.name
        # copy2, not move: the edit happens in place and the copy is the record.
        shutil.copy2(str(target), str(backup))

    target.parent.mkdir(parents=True, exist_ok=True)
    # Write to a sibling then replace, so a failure mid-write cannot leave a
    # half-written file where a whole one used to be.
    tmp = target.with_name(target.name + ".partial")
    tmp.write_text(body, encoding="utf-8")
    os.replace(str(tmp), str(target))

    return {
        **info, "existed": existed, "previous_size": previous_size,
        "new_size": len(body.encode("utf-8")), "backup": str(backup) if backup else None,
        "reversible": backup is not None or not existed,
    }


def copy(path: str, destination: str) -> dict[str, Any]:
    """Copy a file or a directory tree. Never overwrites."""
    info = _both(path)
    source = Path(info["resolved"])
    dest_info = _both(destination)
    dest = Path(dest_info["resolved"])
    if not source.exists():
        raise FileOpError(f"no such file or directory: {source}")

    if source.is_dir():
        if _is_inside(dest, source):
            raise FileOpError(
                f"cannot copy {source} into itself ({dest}) - that would nest a "
                "copy inside its own source and then copy that too"
            )
        count = sum(1 for _ in source.rglob("*"))
        if count > COPY_TREE_CAP_ENTRIES:
            raise FileOpError(
                f"{count:,} entries is more than the {COPY_TREE_CAP_ENTRIES:,} "
                "this will copy in one call. Copy the parts instead."
            )
    _refuse_overwrite(dest, "copy")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(str(source), str(dest))
    else:
        shutil.copy2(str(source), str(dest))
    return {**info, "destination": dest_info["resolved"],
            "copied": True, "entries": count if source.is_dir() else 1}


def move(path: str, destination: str) -> dict[str, Any]:
    """Move or rename. Never overwrites."""
    info = _both(path)
    source = Path(info["resolved"])
    dest_info = _both(destination)
    dest = Path(dest_info["resolved"])
    if not source.exists():
        raise FileOpError(f"no such file or directory: {source}")
    if _is_inside(dest, source):
        raise FileOpError(
            f"cannot move {source} into itself ({dest}) - that would nest the "
            "directory inside itself"
        )
    if source == dest:
        return {**info, "destination": str(dest), "moved": False,
                "note": "already there"}
    _refuse_overwrite(dest, "move")
    dest.parent.mkdir(parents=True, exist_ok=True)
    # shutil.move handles a cross-volume move by copying and deleting, which is
    # what we want and is invisible in the return value - so it is checked after.
    shutil.move(str(source), str(dest))
    return {**info, "destination": str(dest), "moved": True,
            "still_at_source": source.exists()}


def archive(path: str, destination: str) -> dict[str, Any]:
    """Zip a file or directory. Never overwrites."""
    info = _both(path)
    source = Path(info["resolved"])
    dest_info = _both(destination)
    dest = Path(dest_info["resolved"])
    if not source.exists():
        raise FileOpError(f"no such file or directory: {source}")

    total = 0
    if source.is_dir():
        for item in source.rglob("*"):
            try:
                if item.is_file():
                    total += item.stat().st_size
            except OSError:
                continue
    else:
        total = source.stat().st_size
    if total > ARCHIVE_MAX_BYTES:
        raise FileOpError(
            f"{total / 1024**3:.1f} GiB is over the 2 GiB limit for one archive"
        )

    _refuse_overwrite(dest, "archive")
    if not dest.suffix:
        dest = dest.with_name(dest.name + ".zip")
    dest.parent.mkdir(parents=True, exist_ok=True)

    written = 0
    with zipfile.ZipFile(str(dest), "w", zipfile.ZIP_DEFLATED) as zf:
        if source.is_dir():
            for item in sorted(source.rglob("*")):
                if item.is_file():
                    zf.write(str(item), str(item.relative_to(source)))
                    written += 1
        else:
            zf.write(str(source), source.name)
            written += 1
    return {**info, "destination": str(dest), "entries": written,
            "source_bytes": total, "archive_bytes": dest.stat().st_size}


def bulk_rename(path: str, pattern: str, replacement: str, *,
                dry_run: bool = True) -> dict[str, Any]:
    """Rename many files at once, by pattern.

    A dry run is the default, not an opt-in. A rename that touches a hundred
    files should require asking for it, and the plan should be visible before
    anything moves.

    `pattern` and `replacement` are regex, matched against the file *name*, not
    the path - renaming `a/b.txt` to `a/c.txt` is what anyone means by this, and
    a pattern that could rewrite directory components is a pattern that can
    scatter a tree.
    """
    info = _both(path)
    root = Path(info["resolved"])
    if not root.is_dir():
        raise FileOpError(f"not a directory: {root}")
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        raise FileOpError(f"pattern is not a valid regular expression: {exc}") from None

    planned: list[dict[str, str]] = []
    unchanged = 0
    for item in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not item.is_file():
            continue
        new_name = rx.sub(replacement, item.name)
        if new_name == item.name:
            unchanged += 1
            continue
        # Both separators, not just os.sep. On Windows os.sep is a backslash, so a
        # replacement of "sub/x.md" slipped past a check that only looked for the
        # native separator and then blew up inside `with_name` with a bare
        # ValueError - an escape attempt reported as an internal error.
        if (
            not new_name
            or new_name in (".", "..")
            or "/" in new_name
            or "\\" in new_name
            or ":" in new_name
        ):
            raise FileOpError(
                f"{item.name!r} would become {new_name!r}, which is not a "
                "usable file name. A rename cannot change directory or drive."
            )
        dest = item.with_name(new_name)
        if dest.exists():
            raise FileOpError(
                f"{item.name!r} would become {new_name!r}, which already exists. "
                "Nothing was renamed - resolve the collision first."
            )
        if len(planned) >= BULK_RENAME_CAP:
            break
        planned.append({"from": item.name, "to": new_name})

    if len(planned) >= BULK_RENAME_CAP:
        raise FileOpError(
            f"at least {len(planned):,} files match, which is the cap. "
            "Rename them in batches so the result can be checked."
        )

    if dry_run:
        return {
            **info, "pattern": pattern, "replacement": replacement,
            "dry_run": True, "would_rename": len(planned),
            "unchanged": unchanged, "plan": planned,
            "note": "nothing was renamed; pass dry_run=False to do it",
        }

    done = []
    for step in planned:
        source = root / step["from"]
        source.rename(root / step["to"])
        done.append(step)
    return {
        **info, "pattern": pattern, "replacement": replacement, "dry_run": False,
        "renamed": len(done), "unchanged": unchanged, "plan": done,
    }