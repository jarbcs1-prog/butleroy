# Local AI Assistant

A 24/7 assistant that lives on one Windows 11 machine, knows that machine, runs the administrative work that needs doing, and orchestrates software projects by delegating to a team of agents — with all reasoning and all memory on-device.

## Read in this order

| File | What it is |
|------|------------|
| **DECISIONS.md** | 66 numbered decisions with rationale and evidence. v1 is in git history. |
| **ARCHITECTURE.md** | Four planes, the VRAM arbiter, authority tiers, memory design |
| **PLAN.md** | Seven phases with measurable gates |
| **INSTALL.md** | How to install it. **Read §3 first** — one step is mandatory and counter-intuitive |
| **HANDOFF.md** | Where the work stands and how to resume it |
| `AI_Assistant_Local.md` | The original source document. **Superseded** — retained for provenance only |

## The governing constraint

> "As much as I'd like that my assistant would be as performant as possible, if it ends up taking all my VRAM and DDRAM for itself, how am I going to get it to do anything that will also require such resources?"

This is encoded as an invariant, not a preference: **the assistant never takes resources it needs from you.** The daemon is always resident (~300 MB); only the model cycles and is killed after 90s idle, which is the only reliable way to return VRAM on Windows. A dedicated arbiter is the single authority on GPU memory and always preempts the assistant's own model before it can affect your workload.

## Hardware (verified 2026-09-30)

RTX 3060 12 GB · Ryzen 5 5600G (6c/6t) · 32 GB DDR4 · ~500 GB NVMe · Python 3.11.9 · CUDA 13.3 · `opencode` v2.0.23 · Obsidian at `E:\Obsidian_Vault`

## What it will and will not do

**Will:** know this machine by querying it · diagnose and maintain it locally · administer files, mail, Telegram and media · teach · orchestrate real software projects by delegation · reflect and improve against evidence.

**Will not:** take over the machine · write its own code or policy · act on your behalf while you're asleep beyond reversible reads and writes · let Telegram authorize anything destructive.

## Hard boundaries

- **No cloud reasoning.** All weights, prompts, inference and memory on-device. Telegram and IMAP are transports, not reasoning. Cloud model providers are permitted in exactly one place — the delegated-worker plane.
- **No listening ports.** All inter-process transport is Windows named pipes.
- **No coupling to any other project.** This repository shares no code, config, state or runtime dependency with any project on `F:` or `E:`. External directories are reached only through an explicit, user-approved workspace manifest.
- **Safety is deterministic code**, never a model and never a prompt instruction.

## Start here

**Phase 0 — PASSED.** The foundation is real and measured, not assumed:

- Built `llama-cpp-python` 0.3.35 from source. The prebuilt wheels ship a CPU backend compiled with AVX-512, which this Ryzen 5 5600G cannot execute — they die with `STATUS_ILLEGAL_INSTRUCTION` on the CPU path, so the fix is `GGML_NATIVE=OFF` and our own build (D25). 20 min at `-j3`.
- `phase0_gate.py` exits **0**: llama.cpp reports **25/25 layers on GPU**, VRAM moves **+1154 MiB**, peak GPU utilisation **21 %**, 223 tok/s generation.
- llama.cpp 0.3.35 understands **Qwen3.5**, so D9's open architecture risk is closed.
- 106 tests green inside the venv.

**Start it:**

```
python -m assistant            # tray icon, Ctrl+Alt+Space, prompt window
python -m assistant --check    # preflight only, no display needed
locala --check                 # same, once installed
verify_install.py              # prove the install actually works
```

## Where it stands

| | |
|---|---|
| Tests | **1080 pass**, 3 skipped, 47 subtests — no `PYTHONPATH` needed |
| Install | `uv pip install -e .` in 2.9 s; `verify_install.py` exits 0 |
| Restart | The daemon survives restarts (D64) |
| Launch | One command starts tray + chord + prompt window (D65, D67) |
| Tools | 61 registered, **51 with executors**, 10 to go |
| Phases | 0–5 closed. Phase 6 (`reflect.*`) not started |

**Blocking defects found and fixed in this round** — all three passed the
previous 1025-test suite:

- **D64** — the daemon could start exactly **once**. Every reboot or crash after
  that killed it permanently.
- **D65** — four complete modules, composed by nothing. `Tray` and `Overlay` were
  referenced from zero modules in `src/`.
- **D67** — the prompt window was mapped, titled, and completely dead. No Tk
  event loop, and the T3 confirmation poll was never armed.

**Proven end to end:** typing a question into the window and getting an answer
back — overlay → `brain_turn` → VRAM lease → the `brain_server` child → real
llama.cpp on the GPU → streamed answer → transcript. **Not yet proven:** a turn
that requires a *tool call*. A greeting correctly produces none, and D34 says an
unconstrained tool call fails *silently* — the model narrates it in prose and the
window looks normal. Ask it *how much VRAM am I using right now*.

**Outstanding:** `email.*` (6 tools, IMAP) and `reflect.*` (4 tools, Phase 6).
Autostart is documented in `INSTALL.md` §9 but not registered. See
`HANDOFF.md` for the current state of play and `PLAN.md` → "Immediate next
action".