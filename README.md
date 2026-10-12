# Butleroy — LocalAIzed Concierge for Windows

A 24/7 assistant that lives on one Windows 11 machine, knows that machine, runs the administrative work that needs doing, and orchestrates software projects by delegating to a team of agents - with all reasoning and all memory on-device.

> **Naming.** The product, the package (`butleroy`), the console script
> (`butleroy`), the named pipes (`\\.\pipe\butleroy.*`) and the credential
> variables (`BUTLEROY_*`) are all Butleroy — see DECISIONS.md D73. Two things
> are deliberately **not**: the project directory is still `G:\localaized`,
> because a filesystem path is not a product name and moving the checkout would
> invalidate the venv and every relative path; and `LOCALAIZED_*_PASSWORD` /
> `LOCALAIZED_*_TOKEN` still work with a deprecation warning, because a rename
> that silently drops a configured credential is worse than a stale name. The
> mark geometry the product draws lives in `src/assistant/brand.py`; see
> DECISIONS.md D72 and `brand/README.md`.

## Read in this order

| File | What it is |
|------|------------|
| **DECISIONS.md** | 81 numbered decisions with rationale and evidence. v1 is in git history. |
| **ARCHITECTURE.md** | Four planes, the VRAM arbiter, authority tiers, memory design |
| **PLAN.md** | Seven phases with measurable gates |
| **INSTALL.md** | How to install it. **Read §3 first** — one step is mandatory and counter-intuitive |
| **HANDOFF.md** | Where the work stands and how to resume it |
| `brand/README.md` | The brand kit: mark, palette, glyphs, voice, widget rules |
| `src/assistant/settings.py` | The settings window (tray -> Settings...). Models only, so far |
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
butleroy --check              # same, once installed
verify_install.py              # prove the install actually works
```

## Where it stands

| | |
|---|---|
| Tests | **1412 passed**, 3 skipped, 47 subtests — no `PYTHONPATH` needed. Zero known failures; the intermittent Tcl abort in HANDOFF §5 is still open, so full-suite runs occasionally abort |
| Install | `uv pip install -e .` in 2.9 s; `verify_install.py` exits 0 |
| Restart | The daemon survives restarts (D64) |
| Launch | One command starts tray + chord + prompt window (D65, D67) |
| Tools | 61 registered, **61 with executors** — every tool can run |
| Phases | **0–6 closed.** Every registered tool has an executor |

**Blocking defects found and fixed** — every one of them passed the suite as it
stood, which is 1,090 tests for the last one:

- **D64** — the daemon could start exactly **once**. Every reboot or crash after
  that killed it permanently.
- **D65** — four complete modules, composed by nothing. `Tray` and `Overlay` were
  referenced from zero modules in `src/`.
- **D67** — the prompt window was mapped, titled, and completely dead. No Tk
  event loop, and the T3 confirmation poll was never armed.
- **D68** — the window **proposed** a tool and dropped it. `_run_turn` printed
  `proposing <tool> (<args>)` and never sent it through `tool_call`, so the front
  door you sit in front of could not run any tool at all — including a T0 read
  needing no approval. It failed silently: no error, and the audit chain verified.
- **D69** — **every T3 confirmation dialog could not be built.** One button's hover
  colour had seven hex digits, so Tk raised while constructing the dialog — and
  silently, because the 1-second poll presents from a callback where Tk prints the
  traceback and carries on. A pending destructive action could be neither approved
  nor refused. 24 tests on the confirmation rules passed against it, because their
  fake button ignored every colour it was given.
- **D70** — `email.*`, six tools over stdlib IMAP, **no new dependency**. The
  credential is configuration and never a tool argument, because an argument reaches
  the append-only audit chain; the mailbox argument is `mailbox`, not `folder`,
  because the path screen refuses every real mailbox name under the other spelling;
  and `email.delete` files a message locally before expunging it, so it can be
  recovered. A real IMAP server on a real socket then found four bugs no fake could
  — including that **the client never authenticated at all**.
- **D71** — `reflect.*`, self-improvement that *cannot* do damage. **No tool in the
  group takes a path argument**, so every file is derived from the state directory
  and "the assistant modified its own code" is an operation with no representation;
  the gate denies `src/` independently, and the promoted overlay reaches the system
  prompt and nothing else — a lesson can change how the assistant behaves, never
  what it may do. The daemon records its own refusals, so a lesson rests on what
  happened rather than on what the model says happened.

**Proven end to end, across the whole authority range.** Type a question into the
window and get an answer back — overlay → `brain_turn` → VRAM lease → the
`brain_server` child → real llama.cpp on the GPU → streamed answer → transcript.
Asking *how much VRAM am I using right now* commits to `system.gpu_state` and shows
the real figures. Asking it to delete a file in an approved workspace produces the
confirmation dialog on its own, and approving it removes the file. A lesson promoted
from repeated refusals reaches a **resident** model's prompt with no reload. All
measured on the 4B through the real arbiter and gate.

**Outstanding:** autostart is documented in `INSTALL.md` §9 but not registered —
it is a persistent change to the machine, so it asks first. See `HANDOFF.md` for the
current state of play and `PLAN.md` → "Immediate next action".