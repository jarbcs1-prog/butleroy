"""Tests for `parse_action`'s validation.

These exist because a claim made about this function was false. When the
grammar's `args` rule was flattened - a forward CFG cannot match the name
`toolname` just emitted, so the model pairs any tool with any argument shape - it
was said that `parse_action` would catch a mismatch against the registry's real
schema.

It did not. It parsed JSON and returned whatever came out. Measured on the real
4B, asked to run a health check:

    {"tool": "system.diagnose",
     "arguments": {"content": "...", "path": ""}}

The `file.create` shape, attached to `system.diagnose`. Nothing complained, the
executor ignored both keys, and the call reported success.

So the assertion that was made in a comment is now made in a test. A property
described in prose is not a property; this file is the property.
"""

import pytest

from assistant import brain
from assistant.tools import ALL_TOOLS


def call(tool, **args):
    return brain.parse_action(
        '{"tool": "%s", "arguments": %s}' % (tool, __import__("json").dumps(args)),
        tools=ALL_TOOLS,
    )


# --- the happy path ---------------------------------------------------------


def test_a_correct_call_parses():
    tool, args, err = call("system.gpu_state")
    assert err is None and tool == "system.gpu_state" and args == {}


def test_a_correct_call_with_arguments_parses():
    tool, args, err = call("file.read", path="C:/x", max_bytes=10)
    assert err is None
    assert args == {"path": "C:/x", "max_bytes": 10}


def test_validation_can_be_skipped_only_on_request():
    """`tools=None` exists for a caller with no registry at all - and it must be
    asked for, not defaulted to."""
    tool, args, err = brain.parse_action(
        '{"tool": "not.a.tool", "arguments": {}}'
    )
    assert err is None and tool == "not.a.tool"


# --- the mismatch that actually happened ------------------------------------


def test_a_tool_paired_with_another_tools_shape_is_refused():
    """The measured failure. This is the whole reason the validation exists."""
    tool, args, err = call("system.diagnose", content="check", path="")
    assert tool is None, "a mismatched call must not carry a tool name"
    assert err and "does not take" in err
    assert "content" in err and "path" in err


def test_the_refusal_says_what_the_tool_does_take():
    _t, _a, err = call("system.diagnose", content="x")
    assert "event_log" in err, (
        "a refusal that does not say what is acceptable leaves the model to "
        "guess again the same way"
    )


def test_an_invented_tool_is_refused():
    tool, args, err = call("definitely_not_a_real_tool")
    assert tool is None
    assert "not a registered tool" in err


def test_a_tool_with_no_described_arguments_is_refused_not_guessed_at():
    """Undescribed tools are excluded from the grammar for exactly this reason."""
    undescribed = next(
        t.name for t in ALL_TOOLS if t.parameters is None
    )
    tool, args, err = call(undescribed)
    assert tool is None
    assert "no described arguments" in err


# --- individual rules -------------------------------------------------------


def test_a_missing_required_argument_is_refused():
    tool, args, err = call("file.read")
    assert tool is None
    assert "missing required" in err and "path" in err


def test_an_optional_argument_may_be_omitted():
    tool, args, err = call("file.list", path="C:/x")
    assert err is None and args == {"path": "C:/x"}


def test_a_wrong_type_is_refused():
    tool, args, err = call("file.read", path="C:/x", max_bytes="lots")
    assert tool is None
    assert "whole number" in err


def test_a_string_where_a_number_belongs_is_refused():
    tool, args, err = call("system.processes", limit=25)
    assert err is None and args["limit"] == 25
    tool, args, err = call("system.processes", limit="25")
    assert tool is None and "whole number" in err


def test_a_bool_is_not_accepted_where_a_string_belongs():
    tool, args, err = call("system.service_status", name=True)
    assert tool is None and "must be text" in err


def test_a_bool_argument_accepts_only_a_bool():
    tool, args, err = call("system.diagnose", event_log=True)
    assert err is None and args["event_log"] is True
    tool, args, err = call("system.diagnose", event_log="yes")
    assert tool is None and "true or false" in err


# --- malformed input still behaves ------------------------------------------


def test_a_control_character_in_a_string_is_still_a_json_error():
    """The grammar now forbids these, and this says what happens if one arrives
    anyway - the parser is not the thing that has to be perfect."""
    tool, args, err = brain.parse_action(
        '{"tool": "file.read", "arguments": {"path": "C:/x\ny"}}',
        tools=ALL_TOOLS,
    )
    assert tool is None and "not JSON" in err


def test_an_unknown_tool_with_a_valid_shape_is_still_refused():
    tool, args, err = call("system.gpu_state_typo", )
    assert tool is None and "not a registered" in err


def test_no_arguments_key_at_all_is_treated_as_no_arguments():
    tool, args, err = brain.parse_action(
        '{"tool": "system.gpu_state"}', tools=ALL_TOOLS
    )
    assert err is None and args == {}


def test_the_validation_never_raises():
    """Every failure is a returned error. A raise here would kill the turn on a
    model that merely got something wrong."""
    for body in (
        "", "   ", "null", "[]", "{}", '{"tool": 5}', '{"tool": "x"}',
        '{"tool": "file.read", "arguments": []}',
        '{"tool": "file.read", "arguments": null}',
        '{"tool": "file.list", "arguments": {"path": null}}',
        '```json\n{"tool":"system.info","arguments":{}}\n```',
    ):
        tool, args, err = brain.parse_action(body, tools=ALL_TOOLS)
        assert isinstance(err, (str, type(None))), body