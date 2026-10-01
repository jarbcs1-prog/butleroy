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
    # A JSON string is QUOTED in the output, so a string value must emit a quote,
    # then the content, then another quote. In GBNF that is three fragments:
    # a literal for the quote character (`"\""`), the character class, and the
    # literal again.
    #
    # Writing it as one literal, `"[^"\\]*"`, is the obvious-looking thing and it
    # is WRONG: inside double quotes a GBNF character class is just characters,
    # so that rule matches the seven-character string `[^"\\]*` and nothing else.
    # This was the third instance of the one bug - the tool name was unquoted,
    # then the argument key, then the value - and each was only found by running
    # the emitted text through json.loads, which is why the check has to be that
    # and not "does the grammar look right".
    # The class excludes control characters as well as the quote and backslash.
    # A raw newline inside a JSON string is not legal JSON, and excluding it is
    # not cosmetic: the model produced one, `json.loads` raised "Invalid control
    # character", and a perfectly good tool call was thrown away. Asking for
    # "what is eating my RAM" produced a multi-line answer as an argument.
    #
    # It is the fourth instance of the same shape - something the grammar permits
    # that the *consumer* cannot accept - and the reason every one of these is
    # now caught by feeding the emitted text to json.loads rather than by
    # inspecting the grammar.
    "str": r'"\"" [^"\\\x00-\x1f]* "\""',
    "string": r'"\"" [^"\\\x00-\x1f]* "\""',
    # The minus is QUOTED and it must stay that way. A bare `-` makes
    # llama.cpp's sampler die with an access violation, and it dies at *sample*
    # time, long after LlamaGrammar.from_string returned a perfectly good
    # object. Measured: `root ::= - [0-9]+` crashes the process, while
    # `root ::= "-"? [0-9]+` is fine. This was the only unquoted literal in the
    # whole generator, which is exactly how it survived review.
    #
    # A number is NOT quoted, unlike a string - which is why this one is left
    # emitting bare digits and the two above do not.
    #
    # Bounded to ten digits, and the bound is load-bearing. `+` is unbounded, so
    # asked how much RAM is in use the model emitted a five-hundred-digit number
    # for `limit`, filled the whole token budget with zeros, ran out mid-number
    # and produced a JSON document that would not parse. Every integer in this
    # registry is a limit or a count; ten digits is more than any of them needs
    # and stops one field from eating the turn.
    "int": r'"-"? [0-9]{1,10}',
    "integer": r'"-"? [0-9]{1,10}',
    # `true` and `false` must be QUOTED. Bare, they are rule *references*, and a
    # grammar containing one fails at parse time with "Undefined rule identifier
    # 'true'" - printed to stderr while `LlamaGrammar.from_string` returns a
    # perfectly good object, and the child dies with an access violation the
    # first time it samples a tool call that has a boolean in it.
    #
    # The reason this survived review is the reason D38 exists. The first
    # grammar module carried a `_NON_RULES = {"true", "false", ...}` exemption
    # for exactly this shape, with a comment calling them "terminals that appear
    # inside character classes". That was a check weakened to agree with the
    # broken output: the check could not fail, because it had been told the bug
    # was intended. The exemption is deleted rather than updated, because a
    # list of names this module promises not to check is a list of bugs it will
    # never report.
    #
    # It was found only because `system.diagnose` was the first registered tool
    # with a boolean argument, and it was found at the first real tool call.
    "bool": r'("true"|"false")',
    "boolean": r'("true"|"false")',
}

# Characters that are NEVER structural in the GBNF this generator emits, and so
# can only be a mistake if they appear bare.
#
# The list is one character long, and that is the point. Two earlier, broader
# versions of this check were both wrong, in opposite directions:
#   * flagging every metacharacter fired on `bool`, which legitimately emits
#     `(true|false)`, and on `[^"\\]*`, where the `*` is a repetition operator.
#     A check that rejects correct output gets deleted rather than fixed.
#   * flagging `-` and `*` missed that `*` is the quantifier in `[0-9]+`.
# So only `-` is flagged: it is the one character proven to crash the sampler,
# and this generator has no use for it bare anywhere.
_BARE_LITERALS = set("-")


def _assert_single_line_alternations(gbnf: str) -> None:
    """Refuse an alternation spread over several lines.

    The second sampler crash found in this grammar, and the more surprising of
    the two because it depends only on formatting. Measured, one process per
    case:

        root ::= "a" | "b"                       PASS
        root ::=                                     PASS
          | "a"
        root ::=                                     CRASH
          | "a"
          | "b"

    `LlamaGrammar.from_string` accepts all three. So the grammar compiles, the
    model loads, `load()` reports the rule count, and the process dies with an
    access violation the first time a token is sampled against it -- which for
    the tool grammar is the first tool call the user makes.

    The tidy multi-line form with a leading bar reads better in a diff, which is
    exactly why it will be reintroduced by someone who has not seen this.
    """
    rules: list[tuple[int, list[str]]] = []
    current: list | None = None
    for i, raw in enumerate(gbnf.splitlines()):
        text = raw.split("#", 1)[0]
        if "::=" in text:
            if current is not None:
                rules.append(current)
            current = [i + 1, [text.split("::=", 1)[1]]]
        elif text.strip() and current is not None:
            current[1].append(text.strip())
    if current is not None:
        rules.append(current)

    for lineno, body in rules:
        joined = " ".join(body)
        if joined.lstrip().startswith("|"):
            raise GrammarError(
                f"line {lineno} starts an alternation with a leading '|', which "
                "makes the first alternative empty. Emit 'rule ::= \"a\" | \"b\"'."
            )
        if len(body) > 1 and "|" in joined:
            raise GrammarError(
                f"the alternation on line {lineno} spans {len(body)} lines. "
                "llama.cpp's sampler dies with an access violation on a "
                "multi-line alternation even though the grammar compiles. Emit "
                "every alternative on one line after '::='."
            )


def _assert_no_bare_metacharacters(gbnf: str) -> None:
    """Refuse generated GBNF containing a bare `-` or `*`.

    The generator quotes every literal it emits -- tool names, argument keys,
    braces -- and `_TYPE_RULES` is the one place a character can reach the
    output without passing through the quoter. That is exactly where the
    segfault lived, and `LlamaGrammar.from_string` accepted it without
    complaint: the grammar compiled, `Brain.load()` reported success, and the
    process died at the first sampled token.

    Character classes and string literals are skipped wholesale. `[^"\\]` is
    full of characters this check would reject if it looked inside one, and
    neither is a position where a bare `-` means anything.
    """
    for lineno, line in enumerate(gbnf.splitlines(), start=1):
        text = line.split("#", 1)[0]
        i = 0
        found: list[str] = []
        while i < len(text):
            ch = text[i]
            # character class: [^"\\], [0-9], [ \t\n]
            if ch == "[":
                i += 1
                while i < len(text) and text[i] != "]":
                    i += 1
                i += 1
                continue
            # string literal: "{", ":", "-"
            if ch == '"':
                i += 1
                while i < len(text):
                    if text[i] == "\\":
                        i += 2
                        continue
                    if text[i] == '"':
                        i += 1
                        break
                    i += 1
                continue
            # bare identifier: ws, args, num, args-file-read. These are rule
            # REFERENCES or rule names, not literals, and our own naming scheme
            # puts hyphens inside them -- `_rule_name` builds `args-file-read`
            # from `args` + `file.read`. Treating them as literals is what made
            # the first version of this check reject every multi-word tool.
            if ch.isalpha() or ch == "_":
                while i < len(text) and (text[i].isalnum() or text[i] in "_-"):
                    i += 1
                continue
            if ch in _BARE_LITERALS:
                found.append(ch)
            i += 1
        if found:
            where = ", ".join(repr(c) for c in sorted(set(found)))
            raise GrammarError(
                f"line {lineno} contains unquoted GBNF literal(s) {where}: "
                f"{line.strip()!r}. Quote them. A bare '-' crashes llama.cpp's "
                "sampler with an access violation after the grammar has already "
                "compiled cleanly, so nothing upstream of sampling would notice."
            )

_JSON_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"}


class GrammarError(ValueError):
    """The registry cannot be turned into a grammar.

    A missing description is a *reportable gap*, not fatal - the grammar is built
    from what IS described. But a description that is present and malformed cannot
    be worked around without inventing something, so it raises.
    """


def _gbnf_literal(s: str) -> str:
    """A GBNF string literal whose *content* is `s`.

    `repr()` was tempting and wrong: Python's repr uses single quotes and switches
    escaping scheme for embedded quotes, so a name containing an apostrophe would
    produce a literal that means something else. Tool names are dotted
    identifiers and safe by construction, but a name is data, and data surprises.

    Read `_json_quoted_literal` before concluding this is the quoter to reach for.
    """
    return '"%s"' % _json_body(s)


def _json_body(s: str) -> str:
    """`s` escaped as the inside of a JSON string."""
    out = []
    for ch in s:
        if ch in _JSON_ESCAPES:
            out.append(_JSON_ESCAPES[ch])
        elif ord(ch) < 0x20:
            out.append("\\u%04x" % ord(ch))
        else:
            out.append(ch)
    return "".join(out)


def _json_quoted_literal(s: str) -> str:
    """A GBNF literal that EMITS `s` as a JSON string, quotes included.

    This is the one that matters, and getting it wrong is invisible until a tool
    call comes back unparseable.

    In GBNF the quotes around a literal are *syntax*: they delimit it, and the
    model never emits them. So `toolname ::= "file.list"` makes the model emit
    `file.list` - which is not valid JSON, because a JSON string needs its own
    quotes in the output text. The first live run of the brain produced exactly
    that:

        {"tool":
        file.list,
        "arguments": ...

    `"tool":` came out quoted because `root` spells it `"\\"tool\\":"`, and
    `file.list` came out bare because `toolname` did not. It parses as far as
    character 9 and then stops, so `json.loads` raises and `parse_action`
    returns `tool=None` with a parse error - the call the grammar so carefully
    forced is thrown away at the last step.

    The grammar had to be quoted *twice over*: JSON-escape the content, add the
    JSON quotes, then GBNF-escape the whole thing so the quotes survive as
    content.
    """
    emitted = '"' + _json_body(s) + '"'
    # GBNF escaping, after the JSON escaping. Backslashes first: the JSON body may
    # already contain `\\`, and doubling those before touching the quotes is what
    # keeps the two layers distinguishable.
    body = emitted.replace("\\", "\\\\").replace('"', '\\"')
    return '"%s"' % body


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

    # The separator is a BARE comma, and it SEPARATES - it never leads. The
    # closing quote of the previous value is already emitted by the value rule
    # itself (`str` ends in its own `"\""`), so a separator that also supplied one
    # produced `""` between arguments.
    parts = []
    for i, key in enumerate(required):
        lead = "" if i == 0 else _gbnf_literal(",") + " "
        parts.append(
            f'{lead}{_json_quoted_literal(key)} ws ":" ws {_value_rule(params[key])}'
        )
    body = " ".join(parts)

    # The optional tail. Each optional argument is its own independent group, so
    # they may appear in any combination and in any order - which is correct,
    # since JSON does not care about key order and the parser checks names.
    #
    # The comma leads a group ONLY when something precedes it: a required
    # argument, or an earlier optional. A tool whose arguments are all optional
    # has nothing in front, and emitting a leading comma produced
    #
    #     {"tool": "system.processes", "arguments": { ,"limit": 100}}
    #
    # on the real model. Five of the twenty-three tools were affected, and every
    # shape assertion passed, because every one of them built a tool that
    # happened to have a required argument.
    tail = ""
    for index, key in enumerate(optional):
        entry = f'{_json_quoted_literal(key)} ws ":" ws {_value_rule(params[key])}'
        if index == 0 and not required:
            tail += f' ( ws {entry})?'
        else:
            tail += f' ( ws {_gbnf_literal(",")} ws {entry})?'

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
        toolname ::= "\\"file.list\\"" | "\\"file.read\\"" | ...
        args     ::= args-file-list | args-file-read | ...
        args-... ::= "{" ws "\\"path\\":" ws <str> "}"

    Within one shape the arguments appear in a fixed order, which is a real
    constraint and is the point: the model cannot invent a key, reorder the
    required ones, or slip prose in between. Across shapes it is free to pick the
    wrong one, and `parse_action` is what catches that - see the note on `args`
    below for why that trade is correct rather than a compromise.
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

    # Alternations are emitted on ONE line, without the leading `|` that reads
    # tidily in a diff.
    #
    # This is not a style choice. llama.cpp's sampler dies with an access
    # violation on the multi-line, leading-bar form as soon as a rule has more
    # than one alternative, and LlamaGrammar.from_string accepts it without
    # complaint -- the grammar compiles, load() reports success, and the process
    # dies at the first sampled token. Measured: `root ::=\n  | "a"\n  | "b"`
    # crashes while `root ::= "a" | "b"` does not; and a three-alternative rule
    # crashes either way unless it is a single line. With 23 tools this is the
    # difference between a working assistant and a child process that dies the
    # first time a tool is called.
    lines = [
        "# generated from the live tool registry - do not edit by hand",
        'root ::= "{" ws "\\"tool\\":" ws toolname "," ws "\\"arguments\\":" ws args "}"',
        # Tool names and argument keys are JSON strings, so they are emitted
        # quoted. `root`'s own braces and colons are not - those are JSON
        # punctuation, and GBNF's own quotes already supply them. That asymmetry
        # is the whole bug this module had, so it is worth stating.
        "toolname ::= " + " | ".join(_json_quoted_literal(n) for n in names),
        # A FLAT alternation of the argument shapes, not a dispatcher keyed on the
        # tool name.
        #
        # The dispatcher was `args ::= "file.list" args-file-list | ...`, on the
        # reasoning that `args` follows `toolname` so it could re-read the name.
        # That reasoning is wrong, and it produced invalid JSON in the first live
        # run:
        #
        #     {"tool": "file.list", "arguments": "file.list"{"path": "C:/x"}}
        #
        # GBNF is a forward CFG. It cannot look back at what `toolname` emitted,
        # so the name in the dispatcher is not a match against the name - it is
        # more text the model must produce. The "constraint" was a duplication
        # requirement, and duplication is not JSON. It also means the second
        # occurrence need not equal the first, so it constrained nothing at all.
        #
        # What is given up: the grammar no longer forces the argument shape to
        # match the named tool. That is a real weakening and it is the right
        # trade, because a shape mismatch is caught by `parse_action` against the
        # registry, where the tool's real schema lives - which is a stronger check
        # than the grammar ever was, since it sees the tool that was actually
        # named rather than only what the grammar would have permitted.
        "args ::= " + " | ".join(_rule_name("args", n) for n in names),
    ]
    for n, params in included:
        lines.append(_args_rule(n, params))
    lines.append(r"ws ::= [ \t\n]?")

    gbnf = "\n".join(lines)
    # The rules `root` names: the tool-name list, the args dispatcher, and the
    # whitespace rule. Passed in rather than re-parsed - see _validate.
    referenced = ["toolname", "args", "ws"] + [_rule_name("args", n) for n in names]
    _assert_no_bare_metacharacters(gbnf)
    _assert_single_line_alternations(gbnf)
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


def build_named_decision_grammar(tools: Sequence[Any]) -> Grammar:
    """Pass 1, deciding the tool AND naming it in one go.

    The original two-word grammar forced a *decision* but left the *name* to the
    second pass, which is why the second pass had to accept every tool's shape at
    once. Settling both here means pass 2 has exactly one tool in scope.

    Output is either the bare word `answer`, or `tool:<name>`:

        root ::= "answer" | "tool:" ws toolname
        toolname ::= "\\"system.info\\"" | "\\"file.read\\"" | ...

    One extra token of constraint, no extra pass - the decision and the name are
    the same choice.
    """
    names = sorted(
        t.name for t in tools if getattr(t, "parameters", None) is not None
    )
    if not names:
        raise GrammarError(
            "no tool has described arguments, so there is nothing to decide "
            "between; describe some in tools.py"
        )
    lines = [
        "# pass 1: the decision, and the name of the tool it is a decision about",
        'root ::= "answer" | "tool:" ws toolname',
        "toolname ::= " + " | ".join(_json_quoted_literal(n) for n in names),
        r"ws ::= [ \t\n]?",
    ]
    gbnf = "\n".join(lines)
    _assert_no_bare_metacharacters(gbnf)
    _assert_single_line_alternations(gbnf)
    _validate(gbnf, names, ["toolname", "ws"])
    return Grammar(gbnf=gbnf, tools=tuple(names), gaps=(), n_rules=len(lines))


def build_tool_call_grammar(tool: str, tools: Sequence[Any]) -> Grammar:
    """A call grammar for ONE named tool, so its shape cannot be another's.

    This is the coupling the flat `args` alternation gives up, recovered.

    GBNF is a forward CFG, so `args ::= "file.list" args-file-list` cannot mean
    "the file.list that toolname just produced" - it means "emit the text
    file.list", which the model then does, producing JSON that is not JSON. The
    consequence was measured: asked to run a health check the model answered

        {"tool": "system.diagnose",
         "arguments": {"content": "...", "path": ""}}

    - the file.create shape - and before `parse_action` learned to validate, the
    call silently "succeeded".

    So the name is settled in an earlier pass, and by the time the arguments are
    generated there is exactly one tool in scope. The model cannot choose a shape
    that belongs to something else, because nothing else is in the grammar.
    """
    spec = next((t for t in tools if getattr(t, "name", None) == tool), None)
    if spec is None:
        raise GrammarError(f"{tool!r} is not a registered tool")
    if getattr(spec, "parameters", None) is None:
        raise GrammarError(
            f"{tool} has no described arguments, so no call can be generated "
            "for it; describe it in tools.py or do not offer it"
        )
    g = build_tool_grammar([spec])
    if g.tools != (tool,):
        raise GrammarError(f"expected only {tool} in the grammar, got {g.tools}")
    return g


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
        # Quoted, because that is how the name appears in the emitted JSON. A
        # check written against the unquoted spelling would pass against a
        # grammar that produces unparseable output, which is the one thing this
        # check exists to catch.
        #
        # It must appear EXACTLY once. It used to have to appear twice, once in
        # `toolname` and once in the `args` dispatcher - but GBNF cannot match the
        # second against the first, so the second occurrence was text the model
        # had to emit, which is not JSON. Requiring two occurrences now would
        # re-introduce that bug in the name of validating it.
        lit = _json_quoted_literal(n)
        if gbnf.count(lit) != 1:
            raise GrammarError(
                f"{n} appears {gbnf.count(lit)} time(s) in the grammar; a tool "
                "name must appear exactly once, in `toolname`"
            )


def describe(tools: Sequence[Any]) -> dict[str, Any]:
    """Build the grammar and return its report, for `status` and for tests."""
    return build_tool_grammar(tools).describe()
