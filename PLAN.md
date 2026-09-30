# Execution Plan

Governed by [`DECISIONS.md`](./DECISIONS.md). In particular **D1 (hard resource budget)**
and **D3 (idle eviction)** — every phase below is measured against them.

Each phase has an **exit gate**. If the gate fails we stop and fix, rather than building
on an unproven assumption. The single most important gate in this plan is Step 0.

---

## Step 0 — Prove native CUDA inference (do this first)

Everything else is downstream of "can llama.cpp run on this GPU, from a prebuilt wheel".
The original proposal assumed this without testing it. Nothing is installed yet, so this
is the cheapest possible moment to find out.

1. `uv venv --python 3.11 .venv`
2. Install `llama-cpp-python` from the `cu124` wheel index (`--extra-index-url`).
3. Download a **small** model (1B-class or smaller) — not the 9B.
4. Load with `n_gpu_layers=-1`. Read llama.cpp's own load log for the offload count.

**Exit gate**
- [ ] Wheel installs with no compiler invocation.
- [ ] Load log reports **N/N layers offloaded to GPU** — a partial count is a FAIL, and
      on 6 physical cores a CPU spill is unusably slow.
- [ ] Token generation observed; note tok/s.
- [ ] `nvidia-smi` confirms the process holds VRAM.

**If this fails**, we stop. Alternatives, in order of preference: `--no-binary
:all:` source build against CUDA 13.3 with VS BuildTools; or drop to CPU/Vulkan
backend. Do not proceed to Step 1 on a broken foundation.

---

## Step 1 — Model bake-off against the VRAM budget

Goal: pick the best model that leaves the headroom D1 requires, using **measured** not
estimated numbers.

1. Download 9B-class heretic/abliterated GGUF + paired `mmproj` (candidates in D9).
2. Load with `n_gpu_layers=-1`, 4K context. Record actual VRAM via `nvidia-smi`.
3. Re-test at 8K context. Record the delta — that delta is the KV cache cost.
4. Time prefill on a ~2K-token prompt and generation on ~200 tokens.
5. Verify native tool-calling: does the model emit a parseable tool call for a prompt
   that requires one?
6. Verify the model *loads at all* on the bundled llama.cpp (the D9 open risk).

**Ladder, ordered by headroom left, best capability first that still fits:**

| Tier | Weights (est.) | Leaves for whisper + vision | Verdict |
|---|---|---|---|
| 9B `IQ4_XS` / `Q4_K_S` | ~5.2–5.5 GB | ~1.5 GB | Safest under D1 |
| 9B `Q4_K_M` | ~5.7–6.2 GB | ~1.0 GB | Fits only if audio stays on CPU |
| 9B `Q5_K_M` | ~6.6–7.0 GB | ~0 GB | Rejected — violates D1 |

**Exit gate**
- [ ] 100% GPU offload at the chosen quant.
- [ ] Steady-state VRAM ≤ 7.0 GiB with 8K context.
- [ ] Measured numbers written into this file, replacing the estimates.
- [ ] Tool call parses and validates.

---

## Step 2 — Tool layer and the deterministic policy gate

Build the safety-critical part **before** any agent loop, and before any UI. A working
assistant that can delete your Downloads folder is worse than no assistant.

1. Tool registry: `name`, `description`, JSON schema, handler, `destructive: bool`.
2. First tools only: `launch_app`, `run_powershell`, `get_clipboard`, `type_text`,
   `list_running_processes`.
3. pydantic validation of every model-emitted call. Invalid → rejected, never executed.
4. Static policy gate (D5): three tiers, path/verb pattern rules, `destructive` flag,
   append-only audit log of every proposed and executed action.
5. Process-execution hardening: no `shell=True` string interpolation,
   `-NonInteractive`, timeouts, output truncation, working directory pinned.

**Exit gate**
- [ ] "Delete everything in my Downloads" produces a confirmation dialog and executes
      nothing until confirmed.
- [ ] A malformed or hallucinated tool call is rejected without reaching the executor.
- [ ] Every action, allowed or denied, appears in the audit log.

---

## Step 3 — Thin vertical slice (first real milestone)

The original plan's five phases produce nothing runnable until the end. This produces a
working assistant in one increment; everything after it is an addition to something that
already works.

1. Tray icon (`pystray`), resident, ~200 MB, no model loaded.
2. Global hotkey `Ctrl+Alt+Space` (D8) → toggles the overlay.
3. Frameless translucent always-on-top PySide6 overlay, streaming output.
4. Named-pipe transport to the model worker (D3).
5. Model worker: spawn on first prompt, stream tokens, **kill after 90 s idle**.
6. Wire the 5 tools from Step 2 into the loop.

**Exit gate — all must be true, no terminal visible**
- [ ] Hotkey → type prompt → streamed reply → action executed.
- [ ] Idle 90 s → `nvidia-smi` shows VRAM returned to baseline.
- [ ] Overlay stays responsive while the worker is loading the model.

---

## Step 4 — Add capability (only after Step 3's gates pass)

Sequenced by cost per unit of value, and each gated the same way.

1. **Voice** — `faster-whisper` (CTranslate2, **not** torch) in CPU RAM by default;
   Piper for TTS. Wake word via `openWakeWord`, not Porcupine (D7).
2. **Screen vision** — `mss` capture, `mmproj` from Step 1, short-lived GPU residency,
   never resident alongside the LLM (D1).
3. **Planner loop** — bounded-iteration plan/execute/observe, error text fed back for
   self-correction. Hard iteration cap.
4. **Memory** — local embeddings + vector store on disk. `sqlite-vec` preferred over
   `chromadb` for footprint. Explicitly *not* using Laya for retrieval scoring (D5).

---

## Explicitly out of scope

- Cloud inference, hosted APIs, telemetry, anything that phones out.
- Local HTTP servers / ports (`llama-server`, LM Studio, Ollama). Named pipes only.
- Autostart at login and installer/packaging, until the assistant is genuinely useful.
- Anything requiring admin elevation. If a task needs it, surface it to the user rather
  than elevating silently.
