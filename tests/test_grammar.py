"""Tests for the GBNF generator.

Two of these tests exist because of crashes that a compiling grammar hid.

`LlamaGrammar.from_string` accepts both a grammar containing a bare `-` and a
grammar whose alternation is spread over several lines. Both then kill
llama.cpp's sampler with an access violation at the first sampled token, which
is the first tool call the user makes. The grammar compiles. `Brain.load()`
reports success. The child process dies.

So the generator now refuses both shapes, and these tests pin the refusals.
Compiling the grammar is NOT the test: every broken grammar below compiles
cleanly. That is the whole reason the guards exist, and the reason they cannot
be replaced by a round-trip through the real llama.cpp.

There is deliberately no test that samples a real grammar here. That needs the
GPU, and 0.3GB of VRAM is too much to spend per test run; `probe_grammar.py`
at the repo root does it on demand. What is cheap and deterministic is the
shape of the text, which is what actually went wrong both times.
"""

import json

import pytest

from assistant import grammar as G
from assistant.policy import ToolSpec, Tier
from assistant.tools import ALL_TOOLS

PATHY = {"path": {"type": "str"}}


def _gbnf_comma(gbnf: str) -> bool:
    """Does the grammar emit a bare comma between arguments?"""
    return G._gbnf_literal(",") in gbnf


def unescape_gbnf(literal: str) -> str:
    """The text llama.cpp will emit for a GBNF string literal.

    Not a general GBNF engine - just the one job these tests need, and doing it
    here rather than in the module keeps the module free of a parser whose only
    user is a test.
    """
    assert literal.startswith('"') and literal.endswith('"'), literal
    body = literal[1:-1]
    out = []
    i = 0
    while i < len(body):
        if body[i] == "\\":
            out.append(body[i + 1])
            i += 2
            continue
        out.append(body[i])
        i += 1
    return "".join(out)


def spec(name, parameters, *, tier=Tier.READ):
    return ToolSpec(name, tier, f"{name} description", parameters=parameters)


# --- what the generator produces ---------------------------------------------


def test_the_live_registry_builds_a_grammar():
    g = G.build_tool_grammar(ALL_TOOLS)
    assert g.tools, "no tools made it into the grammar"
    assert g.gbnf.lstrip().startswith("#")
    assert "root ::=" in g.gbnf


def test_only_described_tools_are_included_and_the_rest_are_named_as_gaps():
    """D31: a tool we cannot describe is a gap we must be able to see.

    `parameters is None` means nobody has described the tool. It must be absent
    from the grammar -- a grammar that guessed the shape would let the model
    emit an argument the executor does not accept -- and it must appear in
    `gaps`. Dropping it silently is the failure mode worth avoiding.
    """
    undescribed = [t.name for t in ALL_TOOLS if t.parameters is None]
    assert undescribed, "expected some undescribed tools in the real registry"
    g = G.build_tool_grammar(ALL_TOOLS)
    assert not (set(g.tools) & set(undescribed))
    assert set(g.gaps) == set(undescribed)


# --- what the model will actually EMIT ---------------------------------------
#
# The bug these three were written for: in GBNF the quotes around a literal are
# syntax, so `toolname ::= "file.list"` makes the model emit `file.list` with no
# JSON quotes around it. The first live tool call came back as
#
#     {"tool":
#     file.list,
#     "arguments": ...
#
# `"tool":` was quoted because `root` spells it `"\"tool\":"`, and the name was
# bare because `toolname` did not. json.loads stopped at character 9, and the
# call the grammar had so carefully forced was discarded at the last step.
#
# So these assert on the text the sampler will emit, not on how the grammar
# happens to be spelled. Asserting the spelling is what let the bug through.


def test_json_output_literals_are_quoted():
    """A JSON string in the output must carry its own quotes."""
    g = G.build_tool_grammar([spec("file.list", PATHY)])
    assert '"\\"file.list\\""' in g.gbnf, (
        "the tool name must be emitted as a quoted JSON string"
    )
    assert '"\\"path\\""' in g.gbnf, "the argument key must be quoted too"

    # And what the sampler emits for those literals is what json.loads wants.
    assert unescape_gbnf(G._json_quoted_literal("file.list")) == '"file.list"'
    assert json.loads(unescape_gbnf(G._json_quoted_literal("file.list"))) == "file.list"
    assert json.loads(unescape_gbnf(G._json_quoted_literal("path"))) == "path"


def test_json_punctuation_literals_are_emitted_bare():
    """Braces, colons and commas are JSON structure, not JSON strings.

    The asymmetry is deliberate, so it gets a test of its own. Quoting any of
    these turns a valid object into text that reads plausibly and parses never.
    """
    g = G.build_tool_grammar([spec("t.mixed", {"a": {"type": "str"},
                                               "b": {"type": "str"}})])
    assert '"{"' in g.gbnf and '"}"' in g.gbnf and '":"' in g.gbnf
    # The comma between arguments is bare: the closing quote of the previous value
    # comes from the value rule, which ends in its own `"\""`.
    assert _gbnf_comma(g.gbnf), "the argument separator must emit a bare ','"
    assert G._json_quoted_literal(",") not in g.gbnf, (
        "the separator must not emit a quote as well - the value already closed"
    )


def _render(rule_body: str, classes: dict[str, str]) -> str:
    """Reduce one generated rule to the text the sampler would emit.

    String literals become their content, each character class becomes the
    stand-in given for *that class*, `ws` disappears, and optional groups are
    taken as present with their leading optional literal omitted.

    Three things here were wrong before they were right, and each one produced a
    test failure that looked like a product bug:

    * substituting the whole value rule instead of the class inside it threw away
      the `"\""` that makes a string value valid JSON;
    * one stand-in for every class put `C:/x` where a number belonged, which is
      how a correct grammar appeared to emit an unquoted value;
    * emitting an optional literal unconditionally prefixed every optional
      argument with `-`, which is a syntax error in JSON.

    Per-class stand-ins and a skip-optional-literals flag remove all three. Not a
    GBNF engine, and not pretending to be one: it understands only what this
    generator emits, so if the generator starts emitting something else it fails
    loudly rather than quietly agreeing.
    """
    out, _ = _walk(rule_body, 0, classes, in_group=False)
    return out


def _walk(text: str, i: int, classes: dict[str, str],
          in_group: bool) -> tuple[str, int]:
    out: list[str] = []
    while i < len(text):
        ch = text[i]
        # A character class is checked FIRST. `[^"\\]` contains a quote
        # character, so a scan for the closing quote of a string literal that
        # starts before one will run straight past it. This is the same trap that
        # made `_validate` in grammar.py report argument names as undefined rules,
        # and it is why the text is never scanned for structure anywhere in this
        # codebase.
        if ch == "[":
            start = i
            i = text.index("]", i) + 1
            while i < len(text) and text[i] in "?+*":
                i += 1
            # Keyed on the class WITHOUT its quantifier. The bracket scan stops at
            # `]`, which is before the `*`, so a key spelled with the quantifier
            # silently never matches - which looks exactly like the generator
            # having emitted a class this walk does not understand.
            out.append(classes.get(text[start:i].rstrip("?+*"), "VALUE"))
            continue
        if ch == '"':
            j = i + 1
            while True:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == '"':
                    break
                j += 1
            literal = text[i : j + 1]
            quantified = j + 1 < len(text) and text[j + 1] == "?"
            # An optional literal is always omitted by this walk. Inside a group
            # the group's own `?` already makes the whole tail optional, so
            # honouring the literal's `?` separately would double-count it - and
            # for `"-"` that emits a leading minus, which is a syntax error in
            # JSON. The walk only ever has to produce one legal document.
            if quantified:
                i = j + 2
                continue
            out.append(unescape_gbnf(literal))
            i = j + 1
            continue
        if ch == "?" and in_group:
            # The group is written `( ... )?`, so the `?` is what closes it.
            # Anything past it belongs to the enclosing rule and must not be
            # consumed here - which is how the closing brace of the argument
            # object got swallowed into the optional tail.
            return "".join(out), i
        if text.startswith("ws", i):
            i += 2
            continue
        if ch == "(":
            # An optional group: take it. This generator nests at most one.
            inner, i = _walk(text, i + 1, classes, in_group=True)
            out.append(inner)
            continue
        if ch in "?+*()":
            i += 1
            continue
        i += 1
    return "".join(out), i


def test_a_generated_call_is_valid_json_once_the_grammars_quotes_are_removed():
    """End-to-end on the text, with no model involved.

    Every string literal in the generated grammar is reduced to what the sampler
    would emit and the result is handed to `json.loads`. This is the check the
    previous suite lacked: it asks the only question that matters - is the output
    a JSON document - and it cannot be satisfied by a grammar that merely looks
    right. The unquoted-name bug compiled, passed every shape assertion, and
    produced text that stops parsing at character 9.
    """
    g = G.build_tool_grammar(
        [
            spec(
                "t.mixed",
                {
                    "path": {"type": "str"},
                    "max_bytes": {"type": "int", "required": False},
                },
            )
        ]
    )
    rule = [ln for ln in g.gbnf.splitlines() if ln.startswith("args-t-mixed ::=")][0]
    body = rule.split("::=", 1)[1]
    # Per-type stand-ins: a string arrives quoted, a number bare.
    classes = {'[^"\\\\]': "C:/x", "[0-9]": "200"}
    parsed = json.loads(_render(body, classes))
    assert parsed == {"path": "C:/x", "max_bytes": 200}, _render(body, classes)

    # A tool with no arguments must also produce a parseable object, because
    # `{}` in the registry is the case most likely to be described wrongly and is
    # the one with no argument rule to check it against.
    bare = G.build_tool_grammar([spec("t.bare", {})])
    bare_rule = [ln for ln in bare.gbnf.splitlines() if ln.startswith("args-")][0]
    assert json.loads(_render(bare_rule.split("::=", 1)[1], classes)) == {}


def test_a_provably_argumentless_tool_is_included_and_is_not_a_gap():
    """`{}` and `None` mean different things and must not look alike.

    None  = nobody has described it      -> excluded, reported as a gap
    {}    = it provably takes no args   -> included, with an empty object
    """
    tools = [
        spec("t.args", PATHY),
        spec("t.bare", {}),
        spec("t.unknown", None),
    ]
    g = G.build_tool_grammar(tools)
    assert set(g.tools) == {"t.args", "t.bare"}
    assert g.gaps == ("t.unknown",)
    assert 'args-t-bare ::= "{" "}"' in g.gbnf


def test_a_tool_name_is_emitted_once_and_its_shape_is_reachable():
    """The name goes in `toolname` exactly once, and its rule is in `args`.

    It used to have to appear twice, because `args` was a dispatcher keyed on the
    tool name. GBNF is a forward CFG, so that second occurrence was not a match
    against the first - it was text the model had to emit, and the first live call
    came back as

        {"tool": "file.list", "arguments": "file.list"{"path": "C:/x"}}

    which is not JSON. Requiring two occurrences in the name of validating it
    would have re-introduced the bug the assertion was written to prevent.

    The spelling is the *quoted* one: the first version of this test counted the
    unquoted spelling, which is why the unquoted spelling could ship. See
    `test_json_output_literals_are_quoted`.
    """
    g = G.build_tool_grammar([spec("file.list", PATHY)])
    lit = G._json_quoted_literal("file.list")
    assert g.gbnf.count(lit) == 1, "the tool name must be emitted exactly once"
    assert "args-file-list" in g.gbnf, "its argument shape must still be reachable"


def test_required_then_optional_arguments_are_emitted_in_order():
    g = G.build_tool_grammar(
        [
            spec(
                "t.mixed",
                {
                    "path": {"type": "str"},
                    "max_bytes": {"type": "int", "required": False},
                },
            )
        ]
    )
    assert "path" in g.gbnf and "max_bytes" in g.gbnf
    assert g.gbnf.index(G._json_quoted_literal("path")) < g.gbnf.index(
        G._json_quoted_literal("max_bytes")
    )


def test_an_all_optional_tool_has_no_dangling_whitespace():
    """`"{" ws ( ... )? "}"` with a doubled space is still valid GBNF.

    A generated artefact that looks like it has a bug trains the next reader to
    go looking for one, so the cosmetic defect is worth a test.
    """
    g = G.build_tool_grammar([spec("t.opt", {"limit": {"type": "int", "required": False}})])
    assert "ws  (" not in g.gbnf


def test_unknown_types_are_refused_rather_than_guessed():
    tools = [spec("t.bad", {"x": {"type": "widget"}})]
    with pytest.raises(G.GrammarError, match="unknown type"):
        G.build_tool_grammar(tools)


def test_a_duplicate_tool_name_is_refused():
    tools = [spec("t.dup", PATHY), spec("t.dup", PATHY)]
    with pytest.raises(G.GrammarError, match="duplicate"):
        G.build_tool_grammar(tools)


def test_two_names_that_collapse_onto_one_rule_are_refused():
    """`a-b` and `a.b` both normalise toward `args-a-b`; a collision would mean
    one tool silently inheriting another's argument shape."""
    tools = [spec("x.y", PATHY), spec("x-y", PATHY)]
    with pytest.raises(G.GrammarError):
        G.build_tool_grammar(tools)


def test_duplicate_names_are_found_even_in_a_fresh_list():
    with pytest.raises(G.GrammarError, match="duplicate"):
        G.build_tool_grammar([spec("t.a", PATHY), spec("t.a", {})])


# --- the two shapes that compile cleanly and then kill the sampler ------------


@pytest.mark.parametrize(
    "gbnf",
    [
        # the exact defect: a bare, unquoted minus
        'args-x ::= "{" ws "n" ws ":" ws -?[0-9]+ "}"\n',
        'args-x ::= "{" ws "n" ws ":" ws - [0-9]+ "}"\n',
        'args-x ::= "{" ws "n" ws ":" ws -[0-9]+ "}"\n',
    ],
)
def test_a_bare_minus_is_refused(gbnf):
    with pytest.raises(G.GrammarError, match="unquoted"):
        G._assert_no_bare_metacharacters(gbnf)


@pytest.mark.parametrize(
    "gbnf",
    [
        # multi-line alternation with a leading bar -- the original emitter's shape
        'toolname ::=\n  | "a"\n  | "b"\n',
        # and without one
        'toolname ::=\n  "a"\n  | "b"\n',
        # a leading bar on its own is an empty first alternative
        'toolname ::=\n  | "a"\n',
    ],
)
def test_a_multi_line_alternation_is_refused(gbnf):
    with pytest.raises(G.GrammarError):
        G._assert_single_line_alternations(gbnf)


@pytest.mark.parametrize(
    "gbnf",
    [
        # the shape we now emit
        'root ::= "a" | "b"\n',
        'root ::= "a" | "b"\nws ::= [ \\t]?\n',
        # a multi-line rule with no alternation is fine
        'a ::=\n  "x" "y"\nb ::= "z"\n',
        # a hyphen inside a rule name is not a bare literal: `args-file-read`
        'root ::= "a" args-file-read | "b" args-file-restore\nargs-file-read ::= "{}"\n',
        # a minus inside a character class is fine
        'ws ::= [-+ ]?\n',
        # a quoted minus is the whole point
        'n ::= "-"? [0-9]+\n',
    ],
)
def test_safe_shapes_are_accepted(gbnf):
    G._assert_no_bare_metacharacters(gbnf)
    G._assert_single_line_alternations(gbnf)


def test_the_generated_grammar_passes_both_guards():
    """The guards are only worth anything if the real output satisfies them."""
    g = G.build_tool_grammar(ALL_TOOLS)
    G._assert_no_bare_metacharacters(g.gbnf)
    G._assert_single_line_alternations(g.gbnf)


def test_no_int_rule_ships_a_bare_minus():
    """Regression guard on the value that caused it.

    Asserted on `_TYPE_RULES` directly because that table is the only place a
    character can reach the grammar without passing through the quoter.
    """
    for name, rule in G._TYPE_RULES.items():
        # Reuse the guard rather than a second, subtly different scanner: a
        # hand-rolled regex here would be a third implementation of "what counts
        # as quoted", and it would be the one that disagrees.
        G._assert_no_bare_metacharacters(f"rule-for-{name} ::= {rule}\n")


def test_every_alternation_rule_is_one_line():
    g = G.build_tool_grammar(ALL_TOOLS)
    for line in g.gbnf.splitlines():
        if "::=" in line and "|" in line.split("::=", 1)[1]:
            assert "|" in line, line
    # and no line after a rule starts with a bare bar
    assert not any(ln.strip().startswith("|") for ln in g.gbnf.splitlines())


# --- the decision grammar -----------------------------------------------------


def test_the_decision_grammar_admits_exactly_two_words():
    g = G.build_decision_grammar()
    assert "tool" in g.gbnf and "answer" in g.gbnf
    assert g.gbnf.count("|") == 1, "a third alternative would reopen the escape hatch"
    # nothing else may be admitted: this is the pass that failed under `auto`
    assert ".*" not in g.gbnf


def test_the_decision_grammar_passes_the_guards_too():
    g = G.build_decision_grammar()
    G._assert_no_bare_metacharacters(g.gbnf)
    G._assert_single_line_alternations(g.gbnf)
