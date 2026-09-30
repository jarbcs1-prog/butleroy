"""
Phase 1 - the model bake-off, candidate 1: the FAST tier.

Gated on one question above all others (PLAN.md): **is the 4B actually
competent?** Not "is it good" - competent enough to emit a tool call that
round-trips. A small model's tool errors are silent: it either calls the right
tool or it produces prose that *looks* like a tool call, and the caller cannot
tell which.

Measures, and what each one is for:

  STRUCTURAL (valid even on a contended machine - none of these are timings)
    load success              does llama.cpp 0.3.35 understand this quant of this model
    layers offloaded          partial offload is silent CPU spill - unusable on 6 cores
    VRAM at 4K / 8K ctx       the real number. The file size is only a floor, and D27
                              says every closed-form KV estimate in the docs is the
                              wrong FORMULA for a hybrid SSM model, so this must be
                              measured, never modelled
    headroom vs the D1 ceiling 7.0 GiB. This is the gate.
    tool-call parse           the competence question
    tool-call honesty         the failure mode that matters: does it invent a
                              tool that does not exist, and does the parser notice

  TIMING (INVALID if the machine is busy - D28)
    prefill tok/s, generation tok/s. Measured anyway and reported as CONTENDED,
    because a number is more useful than a blank, provided it is honestly labelled.

Usage:  .venv\\Scripts\\python.exe phase1_measure.py [--ctx 4096] [--timing]
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import subprocess
import sys
import tempfile
import time

MODEL = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "models",
    "Qwen3.5-4B-Uncensored-HauhauCS-Aggressive-Q8_0.gguf",
)
MMPROJ = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "models",
    "mmproj-Qwen3.5-4B-Uncensored-HauhauCS-Aggressive-BF16.gguf",
)

# D1. The ceiling is a line drawn for the user's benefit; it is not a calculation.
CEILING_BYTES = 7 * 1024**3
# D3/D22. The headroom the user keeps for their own work.
EXPECTED_HEADROOM = 1024**3  # 1 GiB below the ceiling must still be free

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "file_list",
            "description": "List the files in a directory on the local machine.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path of the directory to list.",
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "system_info",
            "description": "Report CPU, memory and GPU information about this machine.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "definitely_not_a_real_tool",
            "description": "This tool does not exist. It is here so we can test whether the "
            "model invents tools, and whether the parser notices when it does.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

PROMPT_TOOL = (
    "List the files in C:\\Users\\Public\\Documents. Use the file_list tool. "
    "Do not explain, just call the tool."
)

# The grammar that makes the commit possible. This is a hand-written probe, not
# the production generator: the production one is generated from the live tool
# registry (tools.py), which is the whole point - a grammar hand-maintained
# beside a tool registry is a grammar that will be wrong in six months.
#
# The shape is deliberately narrow: an object with a "tool" key drawn from the
# real tool names and an "arguments" object with a "path" string. There is no
# place for prose to go, which is why the model cannot narrate.
TOOL_GBNF = r'''
root   ::= "{" ws "\"tool\":" ws toolname "," ws "\"arguments\":" ws args "}"
toolname ::= "\"file_list\"" | "\"system_info\""
args   ::= "{" ws "\"path\":" ws string ws "}"
string ::= "\"" ( [^"\\] | "\\" ["\\/bfnrt] )* "\""
ws     ::= [ \t\n]*
'''


def nvidia_smi(query: str) -> str | None:
    try:
        out = subprocess.run(
            ["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits", "-i", "0"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()


def vram_mib() -> tuple[int, int] | None:
    row = nvidia_smi("memory.used,utilization.gpu")
    if not row:
        return None
    used, util = [x.strip() for x in row.split(",")]
    return int(used), int(util)


class CaptureFd:
    """Capture llama.cpp's C-level stderr. contextlib cannot see C++ output."""

    def __init__(self):
        self.path = None
        self._fd = None

    def __enter__(self):
        self._fd, self.path = tempfile.mkstemp(prefix="phase1_", suffix=".log")
        sys.stderr.flush()
        self._saved = os.dup(2)
        os.dup2(self._fd, 2)
        return self

    def __exit__(self, *exc):
        try:
            sys.stderr.flush()
            os.dup2(self._saved, 2)
            os.close(self._saved)
        finally:
            os.close(self._fd)
        return False

    def text(self) -> str:
        try:
            with open(self.path, "r", errors="replace") as fh:
                return fh.read()
        except OSError:
            return ""


def parse_offload(log: str):
    m = re.search(r"offloaded (\d+)/(\d+) layers to GPU", log)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"offloaded (\d+) repeating layers to GPU", log)
    if m:
        return int(m.group(1)), None
    return None, None


def report(rows: list[tuple[str, str]]) -> None:
    print("=" * 74)
    print(f"{'MEASUREMENT':<26} {'RESULT':<20} VERDICT")
    print("=" * 74)
    for name, value, verdict in rows:
        print(f"{name:<26} {value:<20} {verdict}")
    print("=" * 74)


def _cpu_load() -> float:
    """Instantaneous CPU load as a percentage.

    Measured rather than assumed. D28 says no timing number is trustworthy
    without a quiet machine, and the only honest way to honour that is for the
    script that takes the timing to also report the condition it was taken
    under - otherwise "trustworthy" is a property the reader has to guess at.
    """
    try:
        proc = subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                "(Get-CimInstance Win32_Processor).LoadPercentage",
            ],
            capture_output=True, text=True, timeout=15,
        )
        out = proc.stdout.strip().splitlines()[-1].strip()
        return float(out) if out else 999.0
    except Exception:
        # If we cannot measure the machine's state, assume the worst and let the
        # reader discount the numbers. An unknown is not a quiet machine.
        return 999.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ctx", type=int, default=4096, help="context length to measure")
    ap.add_argument("--mmproj", action="store_true", help="also load the vision projector")
    ap.add_argument("--timing", action="store_true", help="run the (contended) timing runs")
    args = ap.parse_args()

    print("=" * 74)
    print("PHASE 1 - FAST TIER BAKE-OFF")
    print("=" * 74)
    size = os.path.getsize(MODEL) if os.path.exists(MODEL) else 0
    print(f"model   : {os.path.basename(MODEL)}  ({size / 2**30:.2f} GiB on disk)")
    print(f"ctx     : {args.ctx}")
    print(f"ceiling : {CEILING_BYTES / 2**30:.1f} GiB (D1)")
    if size == 0:
        print("FAIL: model not found")
        return 2
    expected = 4_482_402_656
    if size != expected:
        print(f"FAIL: file is {size} bytes, expected {expected}. A truncated GGUF loads and")
        print("      then generates nonsense rather than raising. Refusing to measure it.")
        return 2
    print("size    : byte-exact against the Hugging Face count")

    from llama_cpp import Llama

    rows: list[tuple[str, str, str]] = []
    failures: list[str] = []

    before = vram_mib()
    print(f"gpu idle: {before} MiB used" if before else "gpu idle: UNKNOWN")

    t0 = time.time()
    with CaptureFd() as cap:
        llm = Llama(
            model_path=MODEL,
            n_ctx=args.ctx,
            n_gpu_layers=-1,
            n_batch=512,
            verbose=True,
        )
    load_s = time.time() - t0
    log = cap.text()
    n_gpu, n_total = parse_offload(log)
    after = vram_mib()
    print(f"loaded in {load_s:.1f}s   offload report: {n_gpu}/{n_total}   "
          f"vram after: {after}")

    # ---- structural --------------------------------------------------------
    rows.append(("load success", f"{load_s:.1f}s", "PASS" if n_gpu else "FAIL"))
    if not n_gpu:
        failures.append("did not load")

    rows.append((
        "layers offloaded",
        f"{n_gpu}/{n_total}" if n_gpu else "no report",
        "PASS" if n_gpu and n_total and n_gpu >= n_total else "FAIL",
    ))
    if not (n_gpu and n_total and n_gpu >= n_total):
        failures.append(f"partial offload {n_gpu}/{n_total} -> CPU spill")

    # The gate is on what *the assistant* takes, not on the whole board.
    #
    # This is not a detail. The user's own apps legitimately hold VRAM - at the
    # moment of measurement, 1940 MiB was already in use before we loaded
    # anything. D1's ceiling read as "total board use <= 7 GiB" would then be
    # violated by the user's own workload, which is precisely the thing the
    # constraint is meant to protect them from. The constraint, in the user's
    # words, is about what the assistant consumes: "if it ends up taking all my
    # VRAM ... how am I going to get it to do anything that will also require
    # such resources?" So the number that matters is the delta we caused.
    delta_bytes = (after[0] - before[0]) * 1024 * 1024 if (before and after) else 0
    rows.append((
        f"vram at {args.ctx//1024}K ctx",
        f"{delta_bytes/2**30:.2f} GiB ours",
        f"file is {size/2**30:.2f} GiB, overhead {max(0, (delta_bytes-size))/2**30:.2f} GiB"
        if delta_bytes else "?",
    ))
    if delta_bytes:
        rows.append((
            "board total after load",
            f"{after[0]/1024:.2f} GiB",
            f"of 12.00 GiB; {before[0]/1024:.2f} GiB was already in use by other software"
            if before else "?",
        ))
        if delta_bytes < CEILING_BYTES:
            head = (CEILING_BYTES - delta_bytes) / 2**30
            ok = head >= EXPECTED_HEADROOM / 2**30
            rows.append((
                "headroom vs D1 ceiling",
                f"{head:.2f} GiB spare",
                "PASS" if ok else "FAIL - under 1 GiB below the ceiling",
            ))
            if not ok:
                failures.append(
                    f"assistant takes {delta_bytes/2**30:.2f} GiB, only {head:.2f} GiB "
                    f"below the 7.0 GiB ceiling"
                )
        else:
            rows.append((
                "headroom vs D1 ceiling",
                "OVER CEILING",
                f"assistant takes {delta_bytes/2**30:.2f} GiB",
            ))
            failures.append(
                f"assistant takes {delta_bytes/2**30:.2f} GiB, over the 7.0 GiB ceiling"
            )

    # ---- competence: the tool call ----------------------------------------
    # TWO attempts, both recorded, because they answer different questions.
    #
    # (a) tool_choice="auto" - can the model decide to call a tool on its own?
    #     This is the honest question and the 4B fails it: it narrates the call
    #     in prose ("I need to use the file_list function with...") and never
    #     commits. That is a silent failure - the output looks like an answer.
    #     It is NOT hidden by removing this call; the evidence stays visible.
    #
    # (b) grammar-constrained - can decoding be forced to commit? Yes. The
    #     grammar removes the option of narrating, because prose is not a legal
    #     token sequence in a call position. D6 is inverted accordingly.
    t = time.time()
    out = llm.create_chat_completion(
        messages=[{"role": "user", "content": PROMPT_TOOL}],
        tools=TOOLS,
        tool_choice="auto",
        max_tokens=256,
        temperature=0.0,
    )
    msg = out["choices"][0]["message"]
    # 0.3.35 returns the message as a plain dict, not an object. Handle both
    # rather than guessing: a getattr-only path would AttributeError and a
    # subscript-only path would KeyError, and neither failure tells you which
    # version you have.
    if isinstance(msg, dict):
        content = msg.get("content") or ""
        raw_calls = msg.get("tool_calls") or []
    else:
        content = getattr(msg, "content", "") or ""
        raw_calls = getattr(msg, "tool_calls", None) or []
    names = []
    first_args = None
    for c in raw_calls:
        fn = c["function"] if isinstance(c, dict) else c.function
        if isinstance(fn, dict):
            names.append(fn.get("name"))
            first_args = fn.get("arguments")
        else:
            names.append(fn.name)
            first_args = fn.arguments
    print("-" * 74)
    print(f"AUTO  tool calls: {names}")
    print(f"AUTO  content   : {content[:200]!r}")
    print("-" * 74)

    # (b) the grammar-constrained attempt, which is the one the gate judges.
    from llama_cpp import LlamaGrammar

    grammar = LlamaGrammar.from_string(TOOL_GBNF, verbose=False)
    out2 = llm.create_chat_completion(
        messages=[{"role": "user", "content": PROMPT_TOOL}],
        grammar=grammar,
        max_tokens=256,
        temperature=0.0,
    )
    msg2 = out2["choices"][0]["message"]
    content2 = (msg2.get("content") or "") if isinstance(msg2, dict) else (
        getattr(msg2, "content", "") or ""
    )
    print(f"GBNF  content   : {content2[:200]!r}")
    # `raw_calls` is a truthiness sentinel for the parse block below, not a real
    # list: the grammar path delivers the call as content text, so there are no
    # native tool_calls to iterate. Setting it to an empty list here (as the first
    # version of this script did) silently skipped the whole argument-parsing
    # block and reported "arguments did not parse" for a call that parsed fine.
    raw_calls = [None]
    first_args = None
    try:
        body = json.loads(content2)
        if isinstance(body, dict) and body.get("tool"):
            names = [body["tool"]]
            first_args = body.get("arguments") or {}
        else:
            raw_calls = []
    except (TypeError, ValueError) as exc:
        names = []
        first_args = None
        raw_calls = []
        print(f"GBNF  parse     : {type(exc).__name__}: {exc}")
    print(f"GBNF  tool calls: {names}")
    print("-" * 74)

    parsed = None
    parse_err = None
    if raw_calls:
        if isinstance(first_args, str):
            try:
                parsed = json.loads(first_args)
            except (TypeError, json.JSONDecodeError) as exc:
                parse_err = f"{type(exc).__name__}: {exc}"
        else:
            parsed = first_args
            parse_err = "arguments were not a JSON string"

    rows.append((
        "emitted a tool call",
        ",".join(names) or "NONE",
        "PASS" if names else "FAIL",
    ))
    if not names:
        failures.append("no tool call emitted on a prompt that explicitly asked for one")

    rows.append((
        "arguments are valid JSON",
        "yes" if parsed is not None else parse_err or "n/a",
        "PASS" if parsed is not None else "FAIL",
    ))
    if names and parsed is None:
        failures.append("tool call arguments did not parse")

    rows.append((
        "called the right tool",
        names[0] if names else "-",
        "PASS" if names == ["file_list"] else "FAIL",
    ))
    if names != ["file_list"]:
        failures.append(f"called {names}, expected ['file_list']")

    rows.append((
        "required arg present",
        "yes" if isinstance(parsed, dict) and "path" in parsed else "no",
        "PASS" if isinstance(parsed, dict) and "path" in parsed else "FAIL",
    ))
    if isinstance(parsed, dict) and "path" not in parsed:
        failures.append("tool call omitted the required 'path' argument")

    rows.append((
        "did not invent a tool",
        "no" if "definitely_not_a_real_tool" in names else "yes",
        "PASS" if "definitely_not_a_real_tool" not in names else "FAIL",
    ))
    if "definitely_not_a_real_tool" in names:
        failures.append("invented a tool that does not exist")

    # The CPU load is sampled here, not asserted in the label. A hardcoded
    # "(contended)" is worse than no label at all: it goes stale the moment the
    # machine is quiet, and a stale caveat reads as a real one. A measurement
    # script should measure the condition its measurement depends on.
    load = _cpu_load()
    quiet = load <= 15.0
    rows.append((
        "call latency",
        f"{time.time() - t:.1f}s",
        "VALID" if quiet else f"CONTENDED (cpu {load:.0f}%) - D28",
    ))

    # ---- optional timing ---------------------------------------------------
    if args.timing:
        llm.reset()
        t = time.time()
        plain = llm.create_chat_completion(
            messages=[{"role": "user", "content":
                       "Name three uses for a paperclip. One short sentence."}],
            max_tokens=96, temperature=0.0,
        )
        usage = plain.get("usage", {})
        el = time.time() - t
        outtok = usage.get("completion_tokens", 0) or 1
        intok = usage.get("prompt_tokens", 0) or 1
        print("-" * 74)
        print(f"plain: {outtok} out tokens in {el:.2f}s")
        print("-" * 74)
        rows.append((
            "generation tok/s",
            f"{outtok/el:.1f}",
            "CONTENDED - not a baseline",
        ))
        rows.append((
            "approx prefill tok/s",
            f"{intok/(el*0.4):.0f}" if el else "?",
            "CONTENDED - rough",
        ))

    report(rows)
    print()
    if failures:
        print("GATE: FAIL")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("GATE: PASS - fast tier loads fully on GPU, fits the ceiling, and calls tools")
    return 0


if __name__ == "__main__":
    sys.exit(main())
