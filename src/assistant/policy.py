"""The policy gate - the only thing standing between a language model and
`rm -rf`.

D5 is the rule this module implements: **safety is deterministic code, never a
model, and never a prompt instruction.** Nothing in here consults a model, and
nothing in here can be overridden by anything a model says. The model's job is
to *propose* a tool call; this module's job is to decide whether it happens.

The threat model is specific. The model is not adversarial - it is fallible,
and it is confidently wrong in ways that look exactly like correct answers. It
will cheerfully construct a plausible `delete` call from a sentence the user
said offhand. A prompt saying "be careful" does not help, because the mistake
is in the same channel as the instruction. So the tier is looked up from a
table declared in code, keyed by tool name, and cross-checked against the tool's
own declaration. The model cannot influence the lookup, and if a tool somehow
appears with no declared tier, the answer is DENY.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path, PurePath
from typing import Any, Iterable, Mapping


class Tier(IntEnum):
    """Authority tiers. Ordered so comparisons are meaningful."""

    READ = 0            # enumerate, read, search, git status
    REVERSIBLE_WRITE = 1  # drafts, scratch, edits under git in a declared workspace
    CONSEQUENTIAL = 2    # move, archive, bulk rename, commit, run builds
    IRREVERSIBLE = 3    # delete, send email, install, admin change, send Telegram

    def __str__(self) -> str:
        return f"T{self.value}"


class Channel(str):
    """Where a request came from. Compared by value, not identity."""


# Channel values as module constants so a typo is a NameError, not a silent
# fall-through to the most permissive branch.
CHANNEL_PC = "pc"
CHANNEL_VOICE = "voice"
CHANNEL_TELEGRAM = "telegram"
CHANNEL_SCHEDULED = "scheduled"
CHANNEL_INTERNAL = "internal"


class Decision(str):
    ALLOW = "allow"
    QUEUE = "queue"
    DENY = "deny"
    CONFIRM = "confirm"


@dataclass(frozen=True)
class ToolSpec:
    """A tool's authority, declared in code.

    `destructive` is deliberately separate from the tier: it marks the tools
    where a bug is expensive rather than untidy, so they can be given extra
    scrutiny without inventing a fifth tier.
    """

    name: str
    tier: Tier
    description: str = ""
    destructive: bool = False
    # Paths this tool may touch. Empty means "no path constraint" (e.g. a
    # tool that only reads system metrics). A tool with paths must name at
    # least one, or the deny-path is all that stands between it and C:\.
    paths: tuple[str, ...] = ()
    # Remote/external side effects. A tool that can talk to the outside world
    # is a T3 candidate regardless of what it claims to do.
    external: bool = False
    # Effect outside the machine: sound in a room, or an open microphone. A third
    # reason for T3, added when `voice.speak` and `voice.listen` needed one.
    #
    # It is not `destructive` (nothing is deleted) and it is not `external`
    # (nothing is sent anywhere), so before this flag existed the import-time check
    # below correctly flagged both as mis-declarations - the tools were right and
    # the rule was incomplete. Forcing them to claim `external` to get past the
    # check would have put a lie in the registry, and a registry that has to lie to
    # be internally consistent is a registry nobody can trust.
    #
    # The distinction matters because the two are governed differently: `external`
    # is what D40 constrains to one host and one component, and `destructive` is
    # what the quarantine makes recoverable. A microphone is neither, and it is
    # still the one tool where being wrong is unrecoverable in a way the user
    # cannot undo by restoring a file.
    audible: bool = False
    # The tool's own arguments, so the registry can describe itself.
    #
    # D34 requires the decoding grammar to be generated from the live registry,
    # and a grammar needs each tool's argument names and types. The obvious
    # shortcut is a second table of schemas sitting beside the registry, and that
    # is exactly the drift D36 warns about: two tables that must agree, silently
    # disagreeing the first time someone adds a tool. So the knowledge lives on
    # the tool itself, where adding an argument and describing it are the same
    # edit.
    #
    # Deliberately tiny - {"path": {"type": "str"}, "max_bytes": {"type": "int"}}
    # with an optional "required": False. Not JSON Schema, because a grammar
    # generator needs three types and a required flag, and every feature of JSON
    # Schema beyond that is a way for the grammar and the executor to disagree.
    #
    # None and {} mean different things, and the difference is the whole point:
    #   None - nobody has described this tool's arguments yet. It is EXCLUDED from
    #          the generated grammar and reported as a gap, because a grammar that
    #          guessed its shape would let the model emit an argument the executor
    #          does not accept (D31).
    #   {}   - this tool provably takes no arguments. It is IN the grammar, with
    #          an empty argument object, and is not a gap.
    parameters: Mapping[str, Mapping[str, Any]] | None = None


@dataclass(frozen=True)
class Verdict:
    """The gate's answer. Carries the reason so the audit log is useful."""

    decision: str
    tier: Tier | None = None
    reason: str = ""
    # What the caller should do next. For CONFIRM this is the pending action.
    requires_confirmation_at: str | None = None

    @property
    def allowed(self) -> bool:
        return self.decision == Decision.ALLOW

    def __str__(self) -> str:
        tier = self.tier if self.tier is not None else "?"
        return f"{self.decision} ({tier}): {self.reason}"


# Absolute deny-list. These are never readable, writable, or deletable by the
# assistant, regardless of what a manifest or a tool claims. Checked first, and
# checked against resolved paths so `..\` and short names cannot slip past.
#
# D21 forbids the assistant rewriting its own code or policy. That is enforced
# here rather than by instruction, which is the point of the whole design.
DENY_PATHS: tuple[str, ...] = (
    r"C:\Windows\System32\config",
    r"C:\Windows\System32\drivers",
    r"C:\Program Files\WindowsApps",
    r"C:\Program Files (x86)\WindowsApps",
    r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs\Startup",
    r"C:\$Recycle.Bin",
)

# The assistant's own tree. Self-modification is denied, not merely discouraged
# (D21). Spelled out here as well as being enforced by the manifest resolver so
# that the guarantee survives a mistake in either component.
SELF_PATHS: tuple[str, ...] = (
    r"G:\localaized\src",
    r"G:\localaized\DECISIONS.md",
    r"G:\localaized\ARCHITECTURE.md",
    r"G:\localaized\PLAN.md",
)


def _norm(path: str) -> str:
    """Normalise a path for comparison, best effort.

    `os.path.normcase` lowercases and unifies separators on Windows; the
    `abspath` collapses `..`. Resolving symlinks would be better still, but
    Windows reparse points need care and a missing target should not crash the
    gate - so this normalises without requiring the path to exist.
    """
    return os.path.normcase(os.path.abspath(os.path.expanduser(path)))


def _spellings(path: str) -> set[str]:
    r"""Every spelling of a path that means the same place on this machine.

    Found by running the thing, and it is the second time a path comparison has
    been wrong in a way no test could see by reading the code. The manifest
    stores an approved root in whatever spelling the caller typed, and on this
    machine that is the 8.3 short form - `C:\Users\PCPRIN~1\AppData\...` - while
    `Path.resolve()` hands back the long form, `C:\Users\PC Principal\...`.
    `os.path.normcase` does not touch either, so the two strings stayed
    different after normalisation and the gate reported an approved workspace
    as being outside every approved workspace. The user sees a refusal that is
    simply false, naming roots they approved a moment earlier.

    `GetLongPathNameW` is what turns one into the other, and it needs the path
    to exist - so it is a second spelling to *try*, never a replacement for the
    plain one. A path that does not exist still compares exactly as it always
    did, which is what keeps `_norm`'s promise of not requiring existence.
    """
    plain = _norm(path)
    out = {plain}
    try:
        import ctypes

        buf = ctypes.create_unicode_buffer(32768)
        n = ctypes.windll.kernel32.GetLongPathNameW(  # type: ignore[attr-defined]
            plain, buf, len(buf))
        if n and buf.value:
            wide = os.path.normcase(os.path.abspath(buf.value))
            if wide != plain:
                out.add(wide)
    except (AttributeError, OSError):
        pass
    return out


def contains(candidate: str | os.PathLike, root: str | os.PathLike) -> bool:
    """Is `candidate` inside `root`? Public, so there is one containment rule.

    Every part of the system that has to answer this - the gate, the delegation
    briefing, the tool previews - asks here. The delegation briefing had its own
    copy, and the copy did not know about the 8.3 short spelling: it reported an
    approved workspace as *not approved* while naming that same workspace as the
    approved root, one spelling apart. A briefing that misreports the rule it
    exists to state is worse than no briefing, and D58 says why it happened.
    """
    return _under(os.fspath(candidate), os.fspath(root))


def _under(candidate: str, root: str) -> bool:
    r"""True if candidate is root or lives inside it.

    Uses `commonpath` rather than `startswith` because `C:\localaized-2` must
    not count as being inside `C:\localaized`.

    Tries every spelling of each side rather than one, for the 8.3 reason in
    `_spellings`: one side can arrive short and the other long, and a
    containment test that fails on that is not conservative - it is wrong, in
    the direction that refuses work the user already approved.

    Raw because of that backslash: a plain docstring turns `\l` into an invalid
    escape sequence and Python warns about it on every import.
    """
    for cand in _spellings(candidate):
        for rt in _spellings(root):
            try:
                if os.path.commonpath([cand, rt]) == rt:
                    return True
            except ValueError:
                # Different drives - cannot be nested, so not under.
                continue
    return False


@dataclass
class PolicyGate:
    """Decides whether a proposed tool call may execute.

    The gate is constructed with an explicit tool table and an explicit set of
    allowed roots. It holds no model state and makes no network calls, which
    is what makes it auditable: given the same inputs it returns the same
    verdict, always.
    """

    tools: dict[str, ToolSpec] = field(default_factory=dict)
    allowed_roots: list[str] = field(default_factory=list)
    deny_paths: tuple[str, ...] = DENY_PATHS
    self_paths: tuple[str, ...] = SELF_PATHS
    # True when a human is at the keyboard. Set by the daemon from the session
    # state, never by the model, and never inferred from a tool call.
    attended: bool = True
    # Highest tier that may execute with nobody watching.
    unattended_ceiling: Tier = Tier.REVERSIBLE_WRITE
    # Channels that are allowed to request, but never to authorise, T3.
    remote_channels: tuple[str, ...] = (CHANNEL_TELEGRAM,)

    def register(self, spec: ToolSpec) -> None:
        self.tools[spec.name] = spec

    def register_all(self, specs: Iterable[ToolSpec]) -> None:
        for spec in specs:
            self.register(spec)

    def tier_of(self, tool: str) -> Tier | None:
        """Look the tier up in the table. Never in the request."""
        spec = self.tools.get(tool)
        return spec.tier if spec else None

    def path_refusal(self, raw: str) -> str | None:
        """The path test, asked directly, for callers whose paths are not in args.

        `_check_paths` finds paths by looking at *argument names*, which cannot
        see inside a list. `shell.run` receives its paths as an argv list, so
        the heuristic walks straight past it and the gate would wave through
        `["del", "C:/Windows/System32/config/SAM"]`.

        The tempting fix is to re-check those strings here, in the executor.
        That is how two policies start disagreeing: the deny-list lives in one
        place for six months and then gets edited in one of them. This asks the
        one gate instead, so a rule added to `_check_candidates` is enforced on
        every surface at once.
        """
        return self._check_candidates([raw])

    def _check_paths(self, tool: str, args: dict[str, Any]) -> str | None:
        """Return a denial reason if any argument path is off-limits.

        Arguments are inspected by key name. This is a heuristic, and it is
        applied in addition to the tool's declared `paths` rather than instead
        of it - a defence in depth, so a tool that forgets to declare a path
        constraint is still covered for the obvious argument names.
        """
        spec = self.tools[tool]
        candidates: list[str] = []

        for key, value in args.items():
            if not isinstance(value, str):
                continue
            if any(
                token in key.lower()
                for token in ("path", "file", "dir", "src", "dest", "target", "folder")
            ):
                candidates.append(value)

        for declared in spec.paths:
            candidates.append(declared)

        return self._check_candidates(candidates)

    def protected_refusal(self, raw: str) -> str | None:
        """The protected-location rule alone, with no workspace-root rule.

        Two rules live in this class and they answer different questions.
        *Where may data be touched* (the deny-list, the self-paths, the
        approved roots) governs file access. *What may be executed* is a
        narrower question: `app.launch` on `C:/Program Files/.../notepad.exe`
        reads no user data and is denied by the roots rule purely because
        Windows installed the program somewhere else.

        So the protected half is available on its own. Not because it is
        laxer - it still refuses the assistant's own source and the registry
        hives - but because "outside every approved workspace" is a true and
        useless thing to say about a program the user asked to open.
        """
        return self._check_protected([raw])

    def _check_protected(self, candidates: list[str]) -> str | None:
        """The deny-list and self-paths, over paths someone else found."""
        for raw in candidates:
            resolved = _norm(raw)
            for denied in (*self.deny_paths, *self.self_paths):
                if _under(resolved, _norm(denied)):
                    return (
                        f"path {raw!r} resolves inside a protected location "
                        f"({denied}); the assistant may not read, write or delete "
                        f"its own code or policy (D21)"
                    )
        return None

    def _check_candidates(self, candidates: list[str]) -> str | None:
        """The policy itself, over paths someone else found."""
        why = self._check_protected(candidates)
        if why is not None:
            return why

        for raw in candidates:
            resolved = _norm(raw)

            # No `self.allowed_roots and` guard here. An earlier version had one,
            # on the reasoning that "no roots configured" should not constrain a
            # caller who is legitimately unrestricted. It does the opposite: with
            # zero approved roots the whole test was skipped, so a fresh install -
            # the state in which the user has approved nothing at all - was the
            # single most permissive configuration possible, reading anything
            # anywhere. Empty means nothing is approved, so empty denies (D10).
            if not any(_under(resolved, _norm(root)) for root in self.allowed_roots):
                if self.allowed_roots:
                    # Name what IS allowed. "Access requires a manifest" tells
                    # the caller that it was refused and nothing else - not what
                    # would have worked, so there is no way to act on it. The
                    # caller is either the model, which will guess again next
                    # turn, or the user, who cannot tell which of their folders
                    # they meant to approve.
                    #
                    # Measured: told only this, the 4B answered a copy request by
                    # reaching for `C:/Users/<name>/Desktop`, a folder it
                    # remembered from training data rather than one it had been
                    # given. The roots belong in the refusal.
                    shown = ", ".join(sorted(str(r) for r in self.allowed_roots)[:8])
                    more = ""
                    if len(self.allowed_roots) > 8:
                        more = f" (and {len(self.allowed_roots) - 8} more)"
                    reason = (
                        f"path {raw!r} is outside every approved workspace root. "
                        f"Approved: {shown}{more}. Use one of those, or ask the "
                        f"user to approve this path with a manifest (D10)"
                    )
                else:
                    reason = (
                        f"path {raw!r} cannot be allowed because no workspace has "
                        f"been approved. A fresh install authorises nothing; add a "
                        f"manifest to grant access (D10)"
                    )
                return reason

        return None

    def evaluate(
        self,
        tool: str,
        *,
        channel: str,
        args: dict[str, Any] | None = None,
        actor: str = "user",
    ) -> Verdict:
        """The single decision point. Every tool call passes through here."""
        args = args or {}

        spec = self.tools.get(tool)
        if spec is None:
            # Fail closed. An unknown tool is not a low-risk tool.
            return Verdict(
                decision=Decision.DENY,
                reason=(
                    f"tool {tool!r} is not registered; the gate denies by default "
                    f"rather than trusting an undeclared action (D5)"
                ),
            )

        path_problem = self._check_paths(tool, args)
        if path_problem is not None:
            return Verdict(
                decision=Decision.DENY,
                tier=spec.tier,
                reason=path_problem,
            )

        if spec.tier == Tier.IRREVERSIBLE:
            # Rule 2: a remote channel may request a T3 but never authorise it.
            # A hijacked Telegram session must not become a remote-delete
            # capability, so the answer is always "confirm at the PC".
            return Verdict(
                decision=Decision.CONFIRM,
                tier=spec.tier,
                reason=(
                    f"{spec.name} is {spec.tier} ({'destructive' if spec.destructive else 'external/irreversible'}); "
                    f"requires explicit confirmation at the PC regardless of channel"
                ),
                requires_confirmation_at=CHANNEL_PC,
            )

        if not self.attended and spec.tier > self.unattended_ceiling:
            # Rule 1: the unattended cap.
            return Verdict(
                decision=Decision.QUEUE,
                tier=spec.tier,
                reason=(
                    f"{spec.name} is {spec.tier} but no interactive session is "
                    f"present; unattended execution is capped at "
                    f"{self.unattended_ceiling} (D13). Queued for approval."
                ),
            )

        if spec.tier == Tier.CONSEQUENTIAL:
            return Verdict(
                decision=Decision.CONFIRM,
                tier=spec.tier,
                reason=(
                    f"{spec.name} is {spec.tier} (consequential); "
                    f"requires in-app confirmation"
                ),
                requires_confirmation_at=CHANNEL_PC,
            )

        # Defence in depth: a tool that claims to be T0 but is marked
        # destructive or external is not trusted to be harmless.
        if spec.destructive or spec.external:
            return Verdict(
                decision=Decision.CONFIRM,
                tier=spec.tier,
                reason=(
                    f"{spec.name} is declared {spec.tier} but is flagged "
                    f"{'destructive' if spec.destructive else 'external'}; "
                    f"treating the stricter classification"
                ),
                requires_confirmation_at=CHANNEL_PC,
            )

        return Verdict(
            decision=Decision.ALLOW,
            tier=spec.tier,
            reason=f"{spec.name} is {spec.tier} (read/reversible write) and every check passed",
        )
