"""
GBNF grammar generation, from the live tool registry.

Why this module exists
----------------------
D34 measured the failure precisely: under `tool_choice="auto"` a 4B narrates a
tool call in prose instead of making one. The prose looks like an answer, which
is the worst kind of failure - a caller cannot tell it from success. Constraining
the decode does not make the model *better at deciding* to call a tool. It removes
the option of not committing, which is the only thing that was actually broken.

The evidence for that is a raw grammar with no `tools=` at all producing a correct
call from a model that was never told a tool existed. Nothing in the prompt asked
for it. The grammar simply left nowhere else to go.

Why the grammar is GENERATED and never hand-written
----------------------------------------------------
A grammar maintained beside a tool registry is a grammar that will be wrong in six
months, and it will be wrong *quietly* - the model emits a call for a tool whose
arguments no longer match, and the executor rejects it at the worst moment. D36 is
the general rule: a constraint mechanism that silently does nothing is the worst
failure mode available, and a hand-maintained one is a mechanism that will
gradually stop applying.

So the single source of truth is `ToolSpec.parameters`. A tool that has not been
described is EXCLUDED and REPORTED, never guessed at. The gap is visible because
the gap is the D31 problem: a grammar that invented an argument shape would let
the model emit a call the executor cannot accept, and the assistant would report a
tool call it cannot actually make.

The distinction that matters
----------------------------
`parameters is None`  -> nobody has described this tool. Excluded, reported as a gap.
`parameters == {}`    -> this tool provably takes no arguments. Included, with an
                         empty argument object, and NOT a gap.

Without that distinction a zero-argument tool and an undescribed tool look
identical, and the gap report - the only thing keeping D31 honest - is worthless.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Sequence

__all__ = [
    "GrammarError",
    "Grammar",
    "DECISION_GBNF",
    "build_tool_grammar",
    "build_decision_grammar",
    "describe",
]

# Deliberately tiny. A grammar generator needs to know how to spell a type, and
# every extra type is another way for the grammar and the executor to disagree
# about what an argument means. `int` is the only non-string type the executors
# actually take (max_bytes, limit, hours).
_TYPE_RULES = {
    "str": r'[^"\\]*',
    "string": r'[^"\\]*',
    "int": r'-?[0-9]+',
    "integer": r'-?[0-9]+',
    "bool": r"(true|false)",
    "boolean": r"(true|false)",
}

_JSON_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"}

# Terminals that appear inside character classes and therefore look like rules to
# a naive scanner. Excluded from the "undefined rule" check below.
_NON_RULES = {"str", "int", "bool", "true", "false", "ws"}


class GrammarError(ValueError):
    """The registry cannot be turned into a grammar.

    A missing description is a *reportable gap*, not fatal - the grammar is built
    from what IS described. But a description that is present and malformed cannot
    be worked around without inventing something, so it raises.
    """


def _gbnf_literal(s: str) -> str:
    """A GBNF string literal for `s`.

    `repr()` was tempting and wrong: Python's repr uses single quotes and switches
    escaping scheme for embedded quotes, so a name containing an apostrophe would
    produce a literal that means something else. Tool names are dotted
    identifiers and safe by construction, but a name is data, and data surprises.
    """
    out = []
    for ch in s:
        if ch in _JSON_ESCAPES:
            out.append(_JSON_ESCAPES[ch])
        elif ord(ch) < 0x20:
            out.append("\\u%04x" % ord(ch))
        else:
            out.append(ch)
    return '"%s"' % "".join(out)


def _rule_name(*parts: str) -> str:
    """A GBNF-safe rule name derived from a tool name.

    GBNF rule names are `[a-zA-Z][a-zA-Z0-9-]*`. A dotted tool name has to become
    something legal, and two different tool names must not collide - a collision
    would let one tool's argument shape satisfy another tool's rule.
    """
    name = re.sub(r"[^a-zA-Z0-9]+", "-", "-".join(parts)).strip("-")
    if not name:
        name = "anon"
    if not name[0].isalpha():
        name = "t" + name
    return name


def _rule_name_is_unique(names: Sequence[str]) -> None:
    seen: dict[str, str] = {}
    for n in names:
        r = _rule_name("args", n)
        if r in seen and seen[r] != n:
            raise GrammarError(
                f"tool names {seen[r]!r} and {n!r} both map to GBNF rule {r!r}; "
                "they would share an argument shape"
            )
        seen[r] = n


@dataclass(frozen=True)
class Grammar:
    """A generated grammar, and the honest report of what it could not cover."""

    gbnf: str
    tools: tuple[str, ...]
    gaps: tuple[str, ...] = ()
    n_rules: int = 0

    def summary(self) -> str:
        return (
            f"{len(self.tools)} tools in the grammar, {len(self.gaps)} undescribed "
            f"and therefore excluded, {self.n_rules} rules"
        )

    def describe(self) -> dict[str, Any]:
        return {
            "tools": list(self.tools),
            "gaps": list(self.gaps),
            "n_rules": self.n_rules,
            "gbnf_bytes": len(self.gbnf),
        }


def _normalise_parameters(raw: Any, tool: str) -> dict[str, dict[str, Any]]:
    """Validate one tool's parameter block, or raise with a usable message."""
    if not isinstance(raw, dict):
        raise GrammarError(
            f"{tool}: parameters must be a mapping, got {type(raw).__name__}"
        )
    out: dict[str, dict[str, Any]] = {}
    for key, spec in raw.items():
        if not isinstance(key, str) or not key:
            raise GrammarError(f"{tool}: parameter name {key!r} is not a string")
        if not isinstance(spec, dict):
            raise GrammarError(
                f"{tool}.{key}: parameter spec must be a mapping, "
                f"got {type(spec).__name__}"
            )
        kind = str(spec.get("type", "str")).lower()
        if kind not in _TYPE_RULES:
            raise GrammarError(
                f"{tool}.{key}: unknown type {kind!r}; known types are "
                + ", ".join(sorted(set(_TYPE_RULES)))
            )
        out[key] = {
            "type": kind,
            "required": bool(spec.get("required", True)),
            "description": str(spec.get("description", "")),
        }
    return out


def _args_rule(name: str, params: dict[str, dict[str, Any]]) -> str:
    """One tool's argument object as a GBNF rule.

    Required arguments come first in a fixed order, then an optional tail. The
    order is alphabetical rather than schema order so that adding an argument
    cannot silently reorder the others and change what the model tends to emit.
    """
    rule = _rule_name("args", name)
    if not params:
        return f'{rule} ::= "{{" "}}"'

    required = sorted(k for k, v in params.items() if v["required"])
    optional = sorted(k for k, v in params.items() if not v["required"])

    parts = []
    for i, key in enumerate(required):
        lead = "" if i == 0 else '"," '
        parts.append(
            f'{lead}{_gbnf_literal(key)} ws ":" ws {_value_rule(params[key])}'
        )
    body = " ".join(parts)

    tail = ""
    for key in reversed(optional):
        tail = f' ( ws "," ws {_gbnf_literal(key)} ws ":" ws {_value_rule(params[key])})?'

    # When every argument is optional there is no required part, so emitting the
    # separator unconditionally would leave `"{" ws  ( ... )? "}"` with a dangling
    # ws and a double space. Still valid GBNF, but a generated artefact that looks
    # like it has a bug trains the next reader to hunt for one.
    sep = "" if not required else " "
    return f'{rule} ::= "{{" ws{sep}{body}{tail} "}}"'


def _value_rule(spec: dict[str, Any]) -> str:
    """The GBNF fragment matching one argument's value."""
    return _TYPE_RULES[spec["type"]]


def build_tool_grammar(tools: Sequence[Any]) -> Grammar:
    """Generate the tool-call grammar from the live registry.

    `tools` is a sequence of `ToolSpec`. A tool whose `parameters` is None is
    EXCLUDED and reported in `gaps`; a tool whose `parameters` is `{}` is included
    with an empty argument object. See the module docstring for why that
    distinction is the whole ballgame.

    The shape:

        root     ::= "{" ws "\\"tool\\":" ws toolname "," ws "\\"arguments\\":" ws args "}"
        toolname ::= "file.list" | "file.read" | ...
        args     ::= "file.list" args-file-list | "file.read" args-file-read
        args-... ::= "{" ws "\\"path\\":" ws <str> "}"

    `args` re-reads the tool name to pick the shape, which works because `args`
    immediately follows `toolname` in `root` and GBNF is a plain CFG. That also
    means the arguments must appear in the generated order, which is a real
    constraint and is the point: the model cannot invent a key, reorder the
    required ones, or slip prose in between.
    """
    included: list[tuple[str, dict[str, dict[str, Any]]]] = []
    gaps: list[str] = []

    for spec in tools:
        name = getattr(spec, "name", None)
        if not isinstance(name, str) or not name:
            raise GrammarError(f"tool without a usable name: {spec!r}")
        if getattr(spec, "parameters", None) is None:
            gaps.append(name)
            continue
        included.append((name, _normalise_parameters(spec.parameters, name)))

    if not included:
        raise GrammarError(
            "no tool in the registry has described parameters, so there is no "
            "grammar to build. This is exactly the failure the gap report exists "
            "to surface early rather than at the first tool call."
        )

    names = [n for n, _ in included]
    if len(set(names)) != len(names):
        dupes = sorted({n for n in names if names.count(n) > 1})
        raise GrammarError(f"duplicate tool names in the registry: {dupes}")
    _rule_name_is_unique(names)

    # Sorted rather than registry order: adding a tool then produces a small diff
    # instead of moving every rule, which keeps a real change visible in review.
    included.sort(key=lambda pair: pair[0])
    names = [n for n, _ in included]

    lines = [
        "# generated from the live tool registry - do not edit by hand",
        'root ::= "{" ws "\\"tool\\":" ws toolname "," ws "\\"arguments\\":" ws args "}"',
        "toolname ::=",
    ]
    for n in names:
        lines.append(f"  | {_gbnf_literal(n)}")
    lines.append("args ::=")
    for n in names:
        lines.append(f'  | {_gbnf_literal(n)} {_rule_name("args", n)}')
    for n, params in included:
        lines.append(_args_rule(n, params))
    lines.append(r"ws ::= [ \t\n]?")

    gbnf = "\n".join(lines)
    # The rules `root` names: the tool-name list, the args dispatcher, and the
    # whitespace rule. Passed in rather than re-parsed - see _validate.
    referenced = ["toolname", "args", "ws"] + [_rule_name("args", n) for n in names]
    _validate(gbnf, names, referenced)

    return Grammar(
        gbnf=gbnf,
        tools=tuple(names),
        gaps=tuple(gaps),
        n_rules=len(lines),
    )


# --- the two-pass decision grammar ------------------------------------------
#
# Pass 1 exists because the failure is a DECISION failure, not a formatting
# failure. Under `auto` the model deliberates aloud and never commits. A
# two-alternative grammar leaves no room to deliberate: there is nowhere to put
# the deliberation.
DECISION_GBNF = (
    "# pass 1 of the two-pass turn: the decision itself\n"
    'root ::= "tool" | "answer"'
)


def build_decision_grammar() -> Grammar:
    return Grammar(gbnf=DECISION_GBNF, tools=(), gaps=(), n_rules=1)


def _validate(gbnf: str, names: Sequence[str], referenced: Sequence[str]) -> None:
    """Structural checks, because a broken grammar is quiet at build time.

    D36 in one sentence: a constraint that silently does nothing is the worst
    failure mode available. A grammar that fails to compile looks exactly like a
    grammar that was never applied, so catching it here - where the message can
    name the tool - is worth more than the checks cost.

    The dangling-reference check is driven by the caller, not by re-parsing the
    generated text. The obvious alternative - scan each rule body for bare
    identifiers - is wrong, and visibly so: the string type is spelled
    `[^"\\]*`, whose character class contains a quote character, so any regex
    that masks string literals by scanning for quotes misreads it and reports the
    argument names inside the literal as undefined rules. Validating the data we
    generated is both simpler and correct; the text itself is only checked for
    things that are cheap and unambiguous.
    """
    if "root ::=" not in gbnf:
        raise GrammarError("generated grammar has no root rule")

    defined = re.findall(r"^([a-zA-Z][a-zA-Z0-9-]*) ::=", gbnf, re.M)
    if not defined:
        raise GrammarError("generated grammar defines no rules")
    dupes = sorted({r for r in defined if defined.count(r) > 1})
    if dupes:
        raise GrammarError(f"generated grammar defines a rule twice: {dupes}")

    missing = sorted(set(referenced) - set(defined))
    if missing:
        raise GrammarError(
            f"generated grammar references undefined rules: {missing}. "
            "Defined: " + ", ".join(sorted(defined))
        )

    for n in names:
        lit = _gbnf_literal(n)
        # A tool name has to appear in BOTH places: `toolname` so the name itself
        # is legal, and `args` so the argument shape is selected. Appearing in only
        # one would mean the model can name a tool whose arguments it may not
        # supply, which is the D31 failure with extra steps.
        if gbnf.count(lit) < 2:
            raise GrammarError(
                f"{n} appears {gbnf.count(lit)} time(s) in the grammar; it must "
                "appear in both `toolname` and `args`"
            )


def describe(tools: Sequence[Any]) -> dict[str, Any]:
    """Build the grammar and return its report, for `status` and for tests."""
    return build_tool_grammar(tools).describe()
