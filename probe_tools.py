"""Is the missing tool call the MODEL, or the plumbing? Do not guess - measure.

Four variants, all with the same loaded model:
  A. the original prompt (failed in phase1_measure.py)
  B. the same, without the "do not explain" instruction
  C. tool_choice pinned to a specific function
  D. asking for the textual <tool_call> form instead of a native one

If D works and A/B/C do not, the model can do tools and llama-cpp-python is not
handing the template what it needs. If D also fails, the model does not do tools
and the two-tier plan needs revisiting.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
os.environ.setdefault(
    "PATH", os.environ.get("PATH", "")
) + r";C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v13.3\bin"

from llama_cpp import Llama  # noqa: E402

MODEL = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "models", "Qwen3.5-4B-Uncensored-HauhauCS-Aggressive-Q8_0.gguf",
)
FILE_LIST = {
    "type": "function",
    "function": {
        "name": "file_list",
        "description": "List the files in a directory on the local machine.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Absolute path."}},
            "required": ["path"],
        },
    },
}
PATH_ARG = r"C:\Users\Public\Documents"

VARIANTS = [
    ("A original", [{"role": "user", "content":
        "List the files in C:\\Users\\Public\\Documents. Use the file_list tool. "
        "Do not explain, just call the tool."}], {"tool_choice": "auto"}),
    ("B no 'do not explain'", [{"role": "user", "content":
        "List the files in C:\\Users\\Public\\Documents using the file_list tool."}],
     {"tool_choice": "auto"}),
    ("C pinned tool_choice", [{"role": "user", "content": "List C:\\Users\\Public\\Documents."}],
     {"tool_choice": {"type": "function", "function": {"name": "file_list"}}}),
    ("D textual <tool_call>", [{"role": "user", "content":
        "List the files in C:\\Users\\Public\\Documents. Reply with a single "
        "<tool_call> block containing the file_list call. Output the block only."}],
     {"tool_choice": "auto"}),
]

llm = Llama(model_path=MODEL, n_ctx=4096, n_gpu_layers=-1, n_batch=512, verbose=False)

print("=" * 74)
print("TOOL-CALLING PROBE - is it the model or the plumbing?")
print("=" * 74)

for label, msgs, extra in VARIANTS:
    out = llm.create_chat_completion(
        messages=msgs, tools=[FILE_LIST], max_tokens=200, temperature=0.0, **extra
    )
    msg = out["choices"][0]["message"]
    content = msg.get("content") if isinstance(msg, dict) else msg.content
    calls = (msg.get("tool_calls") if isinstance(msg, dict) else msg.tool_calls) or []
    names = [c["function"]["name"] if isinstance(c, dict) else c.function.name for c in calls]
    text = content or ""
    print("-" * 74)
    print(f"{label}")
    print(f"  native tool_calls : {names or 'NONE'}")
    print(f"  has <tool_call> text: {'YES' if '<tool_call>' in text else 'no'}")
    print(f"  content           : {text[:260]!r}")
    if calls:
        fn = calls[0]["function"] if isinstance(calls[0], dict) else calls[0].function
        raw = fn.get("arguments") if isinstance(fn, dict) else fn.arguments
        print(f"  arguments         : {raw}")
        try:
            print(f"  parsed            : {json.loads(raw) if isinstance(raw, str) else raw}")
        except Exception as exc:  # noqa: BLE001
            print(f"  parsed            : FAILED {type(exc).__name__}: {exc}")
    print(f"  finish_reason     : {out['choices'][0].get('finish_reason')}")

print("=" * 74)
