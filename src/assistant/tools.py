"""The tool registry - the assistant's actual capabilities, and their authority.

Every tool the assistant can reach is declared here, in code, with its tier
attached. The model never chooses a tier; it only names a tool and supplies
arguments. If a capability is not in this file, the policy gate denies it
(policy.py, unknown-tool path), so this file is the complete list of what the
assistant can do to the machine.

The tiers follow D13. The grouping below is deliberately arranged by what the
user asked for in the brief rather than by internal taxonomy, so that the
capability surface can be read against the requirements:

    knowledge · tech support · administration · coding · pedagogy · delegation

**Not present, by design:** anything that edits code inside a target project.
D16 makes delegation a capability restriction rather than an instruction -
"it must not start doing any work itself" - so the orchestration tools below
are the *only* way it can reach a workspace, and they can only dispatch
workers and supervise them.
"""

from __future__ import annotations

from .policy import ToolSpec, Tier

# --------------------------------------------------------------------------
# Knowledge of the system / local tech support  (D15: live introspection,
# never RAG over volatile state - these tools read the machine, not an index)
# --------------------------------------------------------------------------

SYSTEM_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec("system.info", Tier.READ, "CPU/GPU/RAM/disk facts, read live", parameters={}),
    ToolSpec("system.processes", Tier.READ, "List running processes",
             parameters={"limit": {"type": "int", "required": False}}),
    ToolSpec("system.service_status", Tier.READ, "Windows service status",
             parameters={"name": {"type": "str", "required": False}}),
    ToolSpec("system.disk_usage", Tier.READ, "Free space per volume", parameters={}),
    ToolSpec("system.event_log", Tier.READ, "Recent Windows event log entries",
             parameters={"hours": {"type": "int", "required": False},
                         "limit": {"type": "int", "required": False}}),
    ToolSpec("system.gpu_state", Tier.READ, "nvidia-smi VRAM/utilisation", parameters={}),
    ToolSpec("system.package_list", Tier.READ, "Installed software inventory",
             parameters={"query": {"type": "str", "required": False},
                         "limit": {"type": "int", "required": False}}),
    ToolSpec("system.diagnose", Tier.READ, "Rule-based health diagnosis",
             parameters={"event_log": {"type": "bool", "required": False}}),
)

# --------------------------------------------------------------------------
# Administration  (the brief: create, edit, move, copy, delete, archive;
# emails, Telegram messages, documents, media files, files in general)
# --------------------------------------------------------------------------

# Schemas for the file tools. Kept next to the tools, not in a table beside them
# (D34, D36): the registry describes itself, so adding an argument and describing
# it are the same edit. `file.delete` and `file.restore` are spelled out fully
# because they are the first tools a caller will actually reach for and the
# grammar is what stops the model inventing an argument for them.
_PATH = {"path": {"type": "str"}}

FILE_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec("file.list", Tier.READ, "List a directory", parameters=dict(_PATH)),
    ToolSpec("file.read", Tier.READ, "Read a text file",
             parameters={"path": {"type": "str"},
                         "max_bytes": {"type": "int", "required": False}}),
    ToolSpec("file.search", Tier.READ, "Search file contents",
             parameters={"path": {"type": "str"},
                         "query": {"type": "str"},
                         "max_bytes": {"type": "int", "required": False}}),
    ToolSpec("file.metadata", Tier.READ, "Size, dates, hash of a file", parameters=dict(_PATH)),
    ToolSpec("file.preview", Tier.READ, "Render a document/image to text", parameters=dict(_PATH)),
    ToolSpec("file.create", Tier.REVERSIBLE_WRITE, "Create a new file",
             parameters={"path": {"type": "str"}, "content": {"type": "str"}}),
    # T1 is only honest for this because the executor keeps the previous version:
    # overwriting a file that is not in a git repository is otherwise
    # irreversible, and describing it as "under version control" made an
    # irreversible act look like a reversible one on most of this machine.
    ToolSpec("file.edit", Tier.REVERSIBLE_WRITE,
             "Replace a file's contents; the previous version is kept",
             parameters={"path": {"type": "str"}, "content": {"type": "str"}}),
    ToolSpec("file.write_notes", Tier.REVERSIBLE_WRITE, "Write to the Obsidian Assistant/ folder",
             parameters={"subdir": {"type": "str"}, "title": {"type": "str"},
                         "body": {"type": "str"}}),
    ToolSpec("file.copy", Tier.CONSEQUENTIAL, "Copy files (may be large or many)",
             parameters={"path": {"type": "str"}, "destination": {"type": "str"}}),
    ToolSpec("file.move", Tier.CONSEQUENTIAL, "Move/rename files",
             parameters={"path": {"type": "str"}, "destination": {"type": "str"}}),
    ToolSpec("file.archive", Tier.CONSEQUENTIAL, "Create an archive",
             parameters={"path": {"type": "str"}, "destination": {"type": "str"}}),
    ToolSpec("file.bulk_rename", Tier.CONSEQUENTIAL, "Rename many files at once",
             parameters={"path": {"type": "str"}, "pattern": {"type": "str"},
                         "replacement": {"type": "str"},
                         "dry_run": {"type": "bool", "required": False}}),
    ToolSpec("file.delete", Tier.IRREVERSIBLE, "Delete files", destructive=True,
             parameters=dict(_PATH)),
    ToolSpec("file.empty_recycle_bin", Tier.IRREVERSIBLE, "Empty the recycle bin", destructive=True,
             parameters={}),
    # D33: because delete quarantines instead of unlinking, there has to be a way
    # back. This is the inverse of file.delete and it is deliberately NOT
    # destructive and only T2 - restoring something the user asked to be removed is
    # a correction, not a consequential act. It is a separate tool rather than a
    # flag on delete so that the restore path is itself gated and audited.
    ToolSpec("file.restore", Tier.CONSEQUENTIAL, "Restore files from quarantine",
             parameters={"batch": {"type": "str"}}),
)

# The assistant must not shred its own guarantees, and must not be able to
# install its way past the policy gate it runs behind.
SYSTEM_ADMIN_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec("app.launch", Tier.CONSEQUENTIAL, "Launch an application"),
    ToolSpec("app.close", Tier.CONSEQUENTIAL, "Close a process"),
    ToolSpec("shell.run", Tier.CONSEQUENTIAL, "Run a build, test or maintenance command"),
    ToolSpec("package.install", Tier.IRREVERSIBLE, "Install software", destructive=True),
    ToolSpec("package.uninstall", Tier.IRREVERSIBLE, "Uninstall software", destructive=True),
    ToolSpec("registry.write", Tier.IRREVERSIBLE, "Modify the Windows registry", destructive=True),
    ToolSpec("service.start_stop", Tier.IRREVERSIBLE, "Start/stop a Windows service", destructive=True),
    ToolSpec("power.shutdown", Tier.IRREVERSIBLE, "Shut down or restart the machine", destructive=True),
)

MESSAGING_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec("telegram.poll", Tier.READ, "Read incoming Telegram updates"),
    ToolSpec("telegram.send_draft", Tier.REVERSIBLE_WRITE, "Stage a Telegram message for review"),
    ToolSpec("telegram.send", Tier.IRREVERSIBLE, "Send a Telegram message", external=True),
    ToolSpec("email.list", Tier.READ, "List mail messages"),
    ToolSpec("email.read", Tier.READ, "Read a mail message"),
    ToolSpec("email.search", Tier.READ, "Search mail"),
    ToolSpec("email.draft", Tier.REVERSIBLE_WRITE, "Write a draft, never sends"),
    ToolSpec("email.send", Tier.IRREVERSIBLE, "Send an email", external=True),
    ToolSpec("email.delete", Tier.IRREVERSIBLE, "Delete a mail message", destructive=True),
)

# --------------------------------------------------------------------------
# Pedagogy (the brief calls this "last but not least")
#
# Reads and reasons. It cannot write outside its notes folder, so teaching can
# never become a way to change the system.
# --------------------------------------------------------------------------

TEACHING_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec("teach.explain", Tier.READ, "Explain a concept, Socratic by default"),
    ToolSpec("teach.quiz", Tier.READ, "Generate a quiz on a topic"),
    ToolSpec("teach.recall", Tier.READ, "Test recall of something taught before"),
    ToolSpec("teach.summarise", Tier.READ, "Condense material the user points at"),
    ToolSpec("teach.plan_curriculum", Tier.READ, "Sequence a learning plan"),
    ToolSpec("teach.progress", Tier.READ, "Report what has been covered"),
)

# --------------------------------------------------------------------------
# Delegation  (the brief's requirement 3, and the *only* door to a workspace)
# --------------------------------------------------------------------------

DELEGATION_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec("delegate.plan", Tier.READ, "Draft a staged plan with a dev team"),
    ToolSpec("delegate.brainstorm", Tier.READ, "Interrogate the user's idea before planning"),
    ToolSpec("delegate.assign", Tier.CONSEQUENTIAL, "Dispatch a worker to a workspace"),
    ToolSpec("delegate.status", Tier.READ, "Report on in-flight work"),
    ToolSpec("delegate.intervene", Tier.CONSEQUENTIAL, "Pause/restart/replace a worker"),
    ToolSpec("delegate.review", Tier.READ, "Read a worker's output and judge it"),
    ToolSpec("delegate.accept", Tier.CONSEQUENTIAL, "Accept or reject a completed task"),
    ToolSpec("delegate.audit", Tier.READ, "Verify a worker's claims against evidence"),
)

# Self-reflection. Writes only to the assistant's own state directory, and the
# promotion gate in D21 disposes of every proposal deterministically.
REFLECTION_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec("reflect.log_friction", Tier.REVERSIBLE_WRITE, "Record a friction event"),
    ToolSpec("reflect.distil", Tier.READ, "Distil repeated events into a candidate lesson"),
    ToolSpec("reflect.propose", Tier.REVERSIBLE_WRITE, "Propose a one-change improvement"),
    ToolSpec("reflect.report", Tier.READ, "Report reflection and capability state"),
)

ALL_TOOLS: tuple[ToolSpec, ...] = (
    SYSTEM_TOOLS
    + FILE_TOOLS
    + SYSTEM_ADMIN_TOOLS
    + MESSAGING_TOOLS
    + TEACHING_TOOLS
    + DELEGATION_TOOLS
    + REFLECTION_TOOLS
)

# Sanity check at import time rather than in a test: a tool accidentally
# declared at the wrong tier should be caught on day one, not in production.
KNOWN_TIERS = set(Tier)
for _spec in ALL_TOOLS:
    if _spec.tier not in KNOWN_TIERS:
        raise ValueError(f"tool {_spec.name} has an invalid tier {_spec.tier!r}")

# Anything irreversible or external must be T3, no matter how it was declared.
# This is the invariant that stops a future edit from quietly demoting
# `email.send` to T1.
for _spec in ALL_TOOLS:
    if _spec.destructive or _spec.external:
        if _spec.tier is not Tier.IRREVERSIBLE:
            raise ValueError(
                f"tool {_spec.name} is flagged "
                f"{'destructive' if _spec.destructive else 'external'} "
                f"but is declared {_spec.tier}; it must be T3"
            )

# Conversely, a T3 tool that is neither destructive nor external is almost
# certainly a mis-declaration.
for _spec in ALL_TOOLS:
    if _spec.tier is Tier.IRREVERSIBLE and not (_spec.destructive or _spec.external):
        raise ValueError(
            f"tool {_spec.name} is declared T3 but is neither destructive nor "
            f"external; check the declaration"
        )


def by_tier() -> dict[str, list[str]]:
    """Group tool names by tier, for docs and for the overlay's capability list."""
    grouped: dict[str, list[str]] = {str(t): [] for t in Tier}
    for spec in ALL_TOOLS:
        grouped[str(spec.tier)].append(spec.name)
    return grouped


def default_gate(allowed_roots: list[str] | None = None) -> "object":
    """A policy gate preloaded with the full registry."""
    from .policy import PolicyGate

    gate = PolicyGate(allowed_roots=allowed_roots or [])
    gate.register_all(ALL_TOOLS)
    return gate
