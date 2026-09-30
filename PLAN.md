# PLAN

Re-sequenced against the clarified brief. Every phase has a **gate** — a measurable condition.
If a gate fails, we stop and reassess rather than build on sand.

The ordering principle: **get the LLM-free machinery working before the model.** This way you own
a useful system even if the models disappoint you, and the project is not hostage to model quality.

---

## Phase 0 — Prove native CUDA inference · **HARD GATE**

Nothing above this line is real until this passes.

1. `uv venv` on Python 3.11.9 at `G:\localaized\.venv`.
2. Install `llama-cpp-python` from the prebuilt cu124 `win_amd64` index (D2).
3. Download **one small model**: `ggml-org/Qwen3.5-0.8B-GGUF` Q4_K_M, ~500 MB.
   **Not** Qwen2.5 — every real candidate descends from Qwen3.5, so this one test closes the
   architecture question (D9's open risk) as well as the CUDA question.
4. Load with `n_gpu_layers=-1`. Generate.
5. Measure with `nvidia-smi`: confirm **N/N layers offloaded to GPU**.

**Gate:** N/N layers on GPU **and** verifiable GPU utilisation during generation. Two questions
answered by one test: *does CUDA work here* and *does llama.cpp understand Qwen3.5*.

**If this fails, do not proceed.** Fall back to source build via VS BuildTools + `vcvars64`
(D2), and if that fails, reassess the whole premise before writing another line.

*Nothing has been installed yet. This is the first thing that happens.*

---

## Phase 1 — Model bake-off

Real candidates only (D9/D22), downloaded **one at a time**, freed and `nvidia-smi`-confirmed
before the next download. For each:

| Measure | Why |
|---|---|
| Load success | **D9 open risk: unknown architecture may not load** |
| Layers offloaded | partial offload means silent CPU spill — unusable on 6 cores |
| VRAM at 4K ctx, measured | file size is a floor; this is the real number |
| VRAM at 8K ctx, measured | ditto |
| Prefill tok/s | the RAG bottleneck, the number you actually feel |
| Generation tok/s | the number you quote |
| Native tool-call parse | must round-trip through pydantic |

**Candidates, in the order they earn the download:**

| # | Model | Quant | File | Role under test |
|---|---|---|---|---|
| 1 | `HauhauCS/Qwen3.5-4B-Uncensored-HauhauCS-Aggressive` | Q8_0 | 4.17 GB | **fast tier** — the one that must be good |
| 2 | `DavidAU/Qwen3.5-9B-The-Defiant-Fable-Uncensored-Heretic-NEO-IMATRIX-MAX-MTP-GGUF` | IQ4_XS | 5.96 GB | **deep tier** |
| 3 | `llmfan46/gemma-4-E4B-it-ultra-uncensored-heretic-GGUF` | Q5_K_M | 5.37 GB | wildcard — MoE, if fast enough it may replace both |

Cross-check each measurement against `llm-checker gpu-plan` and `llmfit`'s RTX 3060 preset
(104 usable rows). Expect ~200 tok/s out below 6.5 GB and a fall toward ~109 tok/s above 8 GB; a
candidate landing outside that band means our measurement is wrong, not the tool.

**Gate:** a model loads fully on GPU, emits a valid tool call, and measured VRAM leaves ≥1.5 GiB
headroom of the D1 ceiling. The fast tier is the gate — if the 4B is not competent, the tiering
scheme is wrong and we find that out here rather than in month three.

**Kill criterion:** if the 9B fails to load or misses the gate, the deep tier is simply dropped and
Plane D does the heavy reasoning. This is a *planned* outcome, not project failure — the delegation
architecture does not depend on the deep tier existing.

---

## Phase 2 — The resident daemon · **no LLM required**

This is the 24/7 promise, and it costs no GPU. Ship it before touching the brain.

- `uvicorn`-free service loop; `pystray` tray; global hotkey `Ctrl+Alt+Space` (D8).
- Named-pipe server (`multiprocessing.connection`, no pywin32) — the transport for everything.
- **Policy gate**: path allowlist/denylist, tier lookup T0–T3, unattended cap, channel rule (D13).
- **VRAM arbiter** with leases and self-preemption.
- **Audit log**: append-only, hash-chained, mirroring the supervisor's `verifyIntegrity` approach.
- **Workspace manifest resolver** (D10) — empty by default.
- **Telegram long-poll bridge** (D11 — transport only; no model on this path yet).
- **Obsidian bridge** (D14) — scoped read + `Assistant/` write.

**Gate:** hotkey opens the overlay with **no terminal visible**; a T3 tool call from any channel
prompts and executes nothing until confirmed at the PC; an unattended session executes T0/T1 only;
`nvidia-smi` shows VRAM at baseline throughout.

---

## Phase 3 — Supervisor fork + brain, wired together

- Vendor the supervision system at `vendor/supervisor/` with a provenance note (D10, D12).
- **Replace `api/supervisor-api.ts` (express, opens a port) with named-pipe transport.**
- Register Plane A as a `PYTHON_SUBPROCESS` runtime.
- Bring up Plane B as a real child process with the 90 s TTL lease.
- Register Plane B as the `LLMJudge` — the supervision loop itself still runs LLM-free.
- Port the **3-level context allocation** (D20) and the **4 friction categories / 4 promotion
  levels** (D20, D21).

**Gate:** full loop works with Plane B's `delegate()` returning **mocked** results. Loop detection
fires on an agent that repeats itself. Circuit breaker opens after the configured threshold. Audit
chain verifies. Restarting Plane B does not lose supervision state.

---

## Phase 4 — Real workers

- Bind `delegate()` to `opencode run` subprocesses (`opencode v2.0.19` confirmed present).
- Wire the role templates to a real manifest in a **sandbox workspace you approve**.
- Auditors as roles in the same graph: `architect → worker → reviewer`.
- Escalation path when `SUPERVISOR_ESCAPATION_WEBHOOK` is unset — default to the Telegram bridge
  as a *notification*, never as an authorization (D13 rule 2).

**Gate:** an end-to-end task in the sandbox decomposes, delegates, supervises, reviews, and
completes with an intact audit chain. **First time this touches real project code.**

---

## Phase 5 — Voice

- `openWakeWord` or Silero VAD — **not `pvporcupine`**, it needs a cloud key (D7).
- `faster-whisper` / CTranslate2 — **CPU by default**, promoted to GPU only while listening (D18).
- `piper` for TTS — ONNX, CPU, tiny.
- Whisper→brain→piper round trip with the model TTL respected mid-turn.

**Gate:** wake word → transcription → response → speech, end to end, with no VRAM overshoot past
the D1 ceiling and no window stealing focus mid-sentence.

---

## Phase 6 — Self-improvement

Now, and only now, because it requires a working audit trail to be trustworthy (D21).

- Friction log: Structural / Epistemic / Interaction / Strategy.
- L0 Observation → L1 Candidate Lesson → L2 Local Adaptation → L3 General Capability Update.
- One change per cycle, against a baseline. Promotion on `validation_score × confidence`.
- `### OPERATIONAL CONSTRAINTS` overlay injected at ≤500 tokens.
- Self-changes confined to a scratch state directory; **deny-path on its own `policy/`**.

**Gate:** a repeated, validated lesson appears in the overlay. A single failure does **not**
produce a permanent rule. The assistant cannot write its own policy even if instructed to.

---

## Out of scope

- Anything cloud-bound other than Telegram/IMAP transport and Plane D workers.
- Local HTTP servers or listening ports of any kind.
- Silent admin elevation.
- **Rewriting the assistant's own code or policy by itself** — permanently, at every phase (D21).

---

## Risk register

| Risk | Likelihood | Mitigation |
|---|---|---|
| Qwen3.5 GGUF fails to load on this llama.cpp build | **Real** (D9) | Phase 0 tests Qwen3.5 *itself* precisely so this is caught before anything is built on it |
| Delegated workers contend for VRAM | Medium | Arbiter leases; one worker at a time; preemption favours you |
| Model too slow for usable interaction | Medium | Tight context (3-level allocation), measured prefill in Phase 1, TTL keeps the GPU free |
| The 4B fast tier is not actually competent | **Medium** | Phase 1 gates on it, not on the 9B. Discovered in Phase 1, not month three |
| Tier swap latency annoys in practice | Medium | ~5–15 s, only on hard planning turns; measure in Phase 1 and revisit if frequent |
| Assistant over-reaches on a destructive tool | Low | Tiers are code, not prompt (D13); deny-path absolute; unattended cap |
| Scope creep — brief is ~5× v1 plan | **High** | Gates prevent forward motion on unproven ground; each phase is independently shippable |
| Obsidian vault too large to index | **Resolved** | ~430 indexable notes; `datasets/` hard-excluded (D14) |
| "22 GB VRAM available" from a tool | **Resolved** | llm-checker double-counts the 3060; recorded and rejected in D23. One GPU, 12 GB |

---

## Immediate next action

**Phase 0.** One command sequence: create the venv, install one wheel, fetch
`ggml-org/Qwen3.5-0.8B-GGUF` Q4_K_M (~500 MB), prove full GPU offload. It either establishes the
foundation — CUDA *and* Qwen3.5 architecture support in one shot — or tells us quickly that it is
not there. Nothing else in this plan can be honestly planned until it passes.