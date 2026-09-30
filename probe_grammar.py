"""Does a grammar make the model commit when tool_choice=auto did not?

This is the test that decides the architecture. Phase 1 found the fast tier
narration under `auto` and clean native calls when pinned. A GBNF grammar
constrains decoding at the token level, so "The user wants me to..." is not a
legal token sequence in a call position - the model cannot narrate if the
grammar does not permit prose.

Three mechanisms, same prompt that failed in phase1_measure.py:
  1. tools= + response_format json_object with a schema   (llama-cpp -> GBNF)
  2. tools= + an explicit hand-written GBNF grammar
  3. a raw GBNF grammar, no tools= at all
"""
import json
import os
import time

os.environ["PATH"] = (
    os.environ.get("PATH", "")
    + r";C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v13.3\bin"
)
from llama_cpp import Llama, LlamaGrammar  # noqa: E402

MODEL = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "models", "Qwen3.5-4B-Uncensored-HauhauCS-Aggressive-Q8_0.gguf",
)
PROMPT = "List the files in C:\\Users\\Public\\Documents."

TOOL = {
    "type": "function",
    "function": {
        "name": "file_list",
        "description": "List the files in a directory on the local machine.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to list."}
            },
            "required": ["path"],
        },
    },
}
SCHEMA = TOOL["function"]["parameters"]

# A hand-written grammar for exactly this shape. Note what it does NOT allow:
# no free text, so the model has nothing to narrate into.
GBNF = r'''
root   ::= "{" ws "\"tool\":" ws tool "," ws "\"arguments\":" ws args ws "}"
tool   ::= "\"file_list\""
args   ::= "{" ws "\"path\":" ws string ws "}"
string ::= "\"" pathchar* "\""
pathchar ::= [^"\\] | "\\" ["\\/]
ws     ::= [ \t\n]*
'''

llm = Llama(model_path=MODEL, n_ctx=4096, n_gpu_layers=-1, n_batch=512, verbose=False)

# The `grammar` kwarg takes a LlamaGrammar object, not a string. Passing a raw str
# gets as far as _init_sampler and then dies with "'str' object has no attribute
# '_grammar'" - which is at least loud, unlike the nested-in-response_format case.
GRAMMAR = LlamaGrammar.from_string(GBNF, verbose=False)


def show(label, out, parser_note):
    choice = out["choices"][0]
    msg = choice["message"]
    content = msg.get("content") if isinstance(msg, dict) else msg.content
    calls = (msg.get("tool_calls") if isinstance(msg, dict) else msg.tool_calls) or []
    names = [
        (c["function"]["name"] if isinstance(c, dict) else c.function.name) for c in calls
    ]
    text = content or ""
    print("-" * 74)
    print(label)
    print(f"  native tool_calls : {names or 'NONE'}")
    print(f"  finish_reason     : {choice.get('finish_reason')}")
    print(f"  content           : {text[:220]!r}")
    if calls:
        fn = calls[0]["function"] if isinstance(calls[0], dict) else calls[0].function
        raw = fn.get("arguments") if isinstance(fn, dict) else fn.arguments
        print(f"  arguments         : {raw}")
    print(f"  parser            : {parser_note}")
    return text, names


def try_json(text):
    try:
        obj = json.loads(text)
    except Exception as exc:  # noqa: BLE001
        return f"NOT JSON - {type(exc).__name__}"
    if isinstance(obj, dict) and obj.get("tool"):
        inner = obj.get("arguments", {})
        return f"tool={obj['tool']} args={inner} (valid: {isinstance(inner, dict)})"
    return f"valid JSON but no tool: {obj}"


print("=" * 74)
print("GRAMMAR-FIRST PROBE - can decoding be forced to commit?")
print("=" * 74)

# 1. tools= plus a json_object response_format (llama-cpp-python turns this into
#    a GBNF grammar internally).
out = llm.create_chat_completion(
    messages=[{"role": "user", "content": PROMPT}],
    tools=[TOOL],
    response_format={"type": "json_object", "schema": {
        "type": "object",
        "properties": {"tool": {"type": "string"},
                       "arguments": SCHEMA},
        "required": ["tool", "arguments"],
    }},
    max_tokens=120, temperature=0.0,
)
t, _ = show("1 tools= + json_object+schema", out, "")
print("  parser            : " + try_json(t))

# 2. tools= plus an explicit hand-written grammar, passed as the TOP-LEVEL kwarg.
#    The first run of this probe nested it inside response_format and it was
#    SILENTLY IGNORED - no error, no warning, just unconstrained output. Grammar
#    is a top-level parameter of create_chat_completion.
out = llm.create_chat_completion(
    messages=[{"role": "user", "content": PROMPT}],
    tools=[TOOL],
    grammar=GRAMMAR,
    max_tokens=120, temperature=0.0,
)
t2, _ = show("2 tools= + GBNF (top-level kwarg)", out, "")
print("  parser            : " + try_json(t2))

# 3. the same grammar with no tools= at all - the model is never told a tool
#    exists, it can only produce the shape the grammar permits.
out = llm.create_chat_completion(
    messages=[{"role": "user", "content": PROMPT}],
    grammar=GRAMMAR,
    max_tokens=120, temperature=0.0,
)
t3, _ = show("3 raw GBNF, no tools=", out, "")
print("  parser            : " + try_json(t3))

print("=" * 74)
ok = all(
    "tool=file_list" in try_json(x) for x in (t, t2, t3)
)
print("RESULT:", "ALL THREE COMMITTED" if ok else "at least one still failed to commit")
print("=" * 74)

