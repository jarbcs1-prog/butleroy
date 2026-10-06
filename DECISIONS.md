# DECISIONS — v2

Supersedes v1. v1 is retained in git history (`675eb67`).

Context: a fully local, 24/7 Windows assistant on an RTX 3060 12 GB / Ryzen 5 5600G (6c/6t)
/ 32 GB DDR4 / ~500 GB NVMe. Machine verified 2026-09-30.

---

## Part I — Resource discipline (unchanged from v1, still governing)

### D1 — Hard resource budget
The assistant may occupy **at most 7.0 GiB of 12 GiB VRAM** and **16 GiB of 32 GiB RAM**, and
only while actively answering. Rationale is the user's own constraint:

> "if it ends up taking all my VRAM and DDRAM for itself, how am I going to get it to do
> anything that will also require such resources?"

Models **take turns, they do not coexist**. VRAM is a leased resource, not a setting.
Enforced by D3 and the arbiter in ARCHITECTURE.md, not by good intentions.

### D2 — Embedded inference, built by us (SUPERSEDED by D25 — see the correction below)
~~Use `llama-cpp-python` from the `abetlen` prebuilt index (`win_amd64`, CUDA **cu124**,
sm_86 covers the 3060). Available through **v0.3.35**; no MSVC build required.~~

**Phase 0 proved this wrong.** The prebuilt wheels are compiled with AVX-512 and cannot run on a
Ryzen 5 5600G. D25 replaces this decision: we build `llama-cpp-python` from source with a
portable CPU baseline. The embedded-Python-runtime choice survives; only the "prebuilt wheel"
half of it is dead.
PyTorch never enters the stack (see D18).

### D3 — One child process per model, killed on idle TTL
Each model runs in its own process, killed after **90 s idle**. This is the only reliable way to
return VRAM on Windows — it is not a setting, it is a process lifetime. It also gives crash
isolation. Transport is **Windows named pipes** (`multiprocessing.connection`, no pywin32
needed); **no sockets, no listening ports** anywhere in the system.

### D4 — One multimodal model, not a fleet
A single ~9B model with a paired `mmproj` covers text + vision. Whisper stays in CPU RAM and is
promoted to GPU only while actively listening. No second multi-GB resident model.

### D5 — Safety is deterministic code, never a model
Three-tier allow / confirm / deny, per-tool `destructive` flag, path and verb pattern rules,
append-only audit log. Extended to a four-tier authority model in D13.

### D6 - Native tool-calling, grammar only as a backstop — **SUPERSEDED by D34**

> The original reasoning was that native tool-calling is cleaner and a grammar is a fallback for when
> the model produces malformed JSON. Measurement in Phase 1 (D34) showed the opposite for a 4B: the
> model **will not decide** to call a tool, and a grammar is the only thing that makes it commit. The
> idea is not discarded, it is promoted — and it is the one idea from the source document that
> survives intact.


The toy single-action GBNF grammar in the original source doc is not sufficient. Use the model's
native tool-calling with pydantic validation.

### D7 — Offline wake word
`pvporcupine` is **rejected** — it requires a cloud-issued access key. Use `openWakeWord` (ONNX)
or Silero VAD.

### D8 — Hotkey is `Ctrl+Alt+Space`
`Win+Space` is OS-reserved; `Win+Alt+Space` collides with common vendor software.

### D9 — Uncensored models: priority order and verified VRAM
Priority, in the user's stated order: **abliterated → heretic → uncensored.** Most candidates
carry all three tags, so this is a tiebreaker rather than a filter.

Sizes below are **measured file sizes from the HuggingFace API**, not estimates. The v1 ladder was
too optimistic and is corrected here.

Reference model: `DavidAU/Qwen3.5-9B-The-Defiant-Fable-Uncensored-Heretic-NEO-IMATRIX-MAX-MTP-GGUF`
(887 likes, 1.85 M downloads; tags heretic / uncensored / abliterated / imatrix).

| Quant | File size | + KV/cache overhead | Verdict vs 7.0 GiB ceiling |
|---|---|---|---|
| IQ2_M | 4.60 GB | ~5.3 GB | quality-degraded |
| IQ3_M | 5.23 GB | ~5.9 GB | viable, mild degradation |
| **IQ4_XS** | **5.96 GB** | **~6.7 GB** | **Safest 9B option** |
| Q4_K_S | 6.10 GB | ~6.8 GB | tight but viable |
| Q4_K_M | 6.36 GB | ~7.1 GB | **REJECTED — over ceiling** |
| Q5_K_M | 7.14 GB | ~7.8 GB | **REJECTED — over ceiling** |
| Q6_K / Q8_0 | 7.97 / 9.76 GB | — | **REJECTED** |
| `mmproj-F16` | 0.86 GB | — | vision, loaded on demand |

**Correction to v1:** Q4_K_M was recorded as "fits if audio stays on CPU". At 6.36 GB file + ~0.75 GB
overhead it is over the 7.0 ceiling. Only IQ4_XS and below are safe at 9B.

**MoE is excluded outright.** `Qwen3.5-35B-A3B` has a smallest quant of **10.74 GB** — 54 % over
the ceiling, and most of a 12 GB card. **MoE keeps every expert resident; the "A3B" active-parameter
count buys speed, not VRAM.** This eliminates the 35B-A3B, 122B-A10B and 397B-A17B branches
entirely, and is the single most commonly misunderstood fact about MoE.

`gpt-oss-20b` is excluded: smallest quant **11.00 GB** on a 12 GB card, leaving nothing for the OS
or display.

**Open risk retained:** a model newer than the bundled llama.cpp may fail to load due to an unknown
architecture. Partially de-risked — `ggml-org/Qwen3.5-0.8B-GGUF` is published by llama.cpp's own
organisation, confirming Qwen3.5 is a supported architecture, and every candidate descends from
Qwen3.5. Phase 0 therefore proves Qwen3.5, not Qwen2.5. Remaining risk is per-repository GGUF
conversion quality, not core architecture support.

**Architectures independently confirmed present** in the `llmfit` catalog (12,937 models, verified
2026-09-30): `qwen3_5`, `qwen3_5_text`, `gemma4`, `gemma4_unified`, `gpt_oss`. This **downgrades the
Gemma-4 load risk** from "highest" to normal, and `gemma4_unified` indicates genuine any-to-any
multimodality matching the candidate's `any-to-any` pipeline tag.

**Additional candidates surfaced**, ordered by the user's priority (abliterated → heretic →
uncensored):
| Model | Tags | Note |
|---|---|---|
| `mckerm1t/gemma-4-e4b-it-abliterated-bf16` | **abliterated** | BF16 only — would need requantising to GGUF |
| `llmfan46/gemma-4-E4B-it-ultra-uncensored-heretic-GGUF` | heretic, uncensored, **GGUF** | Q5_K_M 5.37 GB — Phase 1 wildcard |
| `p-e-w/gpt-oss-20b-heretic-ara-v3` | heretic | still 11 GB — excluded on size |
| `deadbydawn101/gemma-4-E2B-Heretic-Uncensored-*` | heretic, uncensored | E2B, smaller sibling |

**Note on the `llmfit` catalog's own fields:** `parameter_count` and `min_vram_gb` are unreliable for
MLX and pre-quantised rows (a 9B listed as "1.4B, 0.7 GB VRAM"). Only its `architecture` and MoE/dense
fields were used. Its benchmark `id`s are opaque hashes that do not join to the catalog, so **no
per-model speed figure was taken from it** — see D22.

---

## Part II — Scope decisions (new in v2, from the clarified brief)

### D10 — Hard isolation from all other projects
**`G:\localaized` shares no code, config, state, or runtime dependency with `F:\theseas`,
`F:\crm`, or any other project on `F:` or `E:`.** Those projects were consulted as *sources of
inspiration and templates only* — per explicit user directive. This is a hard boundary.

Consequences, all binding:
- The supervision code is **vendored as an owned fork** at `vendor/supervisor/`, with its own
  provenance note recording origin and licence. It is *our* code once vendored.
- **No file in this project hardcodes a path to another project.** Every external directory is
  reached only through a declared **workspace manifest** — an explicit, user-approved record of
  `root`, `language`, `stack`, `test_command`, `danger_level`. The assistant has no built-in
  knowledge that any particular project exists.
- No shared git history, remotes, dependencies, or state directories.
- If a workspace manifest is absent, the assistant has no capability in that directory at all.

### D11 — Reasoning local, transport may be remote
The original directive *"It has to be fully local no cloud stuff"* is **satisfied at the level of
reasoning**: all model weights, prompts, inference, memory, and decision-making are on-device.

Two things are necessarily not local and are accepted as **transports**, not as reasoning:
- **Telegram** (Bot API, `api.telegram.org`) — required by the 24/7 requirement.
- **Email** (IMAP/SMTP) — inherently a remote protocol.

**Cloud model providers are permitted in exactly one place: Plane D, delegated workers.**
Planes A (daemon), B (the brain), and C (supervisor) never contact a model API off-device.

### D12 — Supervisor vendored as a fork; express API replaced with named pipes
The supervision system is ~4,400 lines of working TypeScript across 26 files and already
implements agent registry, heartbeats, loop detection with an optional `LLMJudge`, watchdog,
circuit breakers, recovery engine, audit trail with integrity verification, and the intervention
ladder `observe → warn → throttle → pause → restart → replace → escalate`.

Two details make the adoption unusually clean:
- `monitoring/llm-judge.ts` is an **optional injected dependency** — the supervision loop needs
  no LLM. There is a purpose-built slot for our local brain to fill.
- `adapters/python-subprocess-adapter.ts` registers `RuntimeType.PYTHON_SUBPROCESS`. The system
  was architected to supervise a Python subprocess runtime — which is exactly Plane A.

**Modification:** `api/supervisor-api.ts` uses **express** and opens a listening port. This
violates D3 and is replaced with a named-pipe transport. Everything else is adopted.

### D13 — Four-tier authority, with an unattended cap and a channel rule
The assistant can delete files and send email, and must be reachable while the user is absent.
Therefore:

| Tier | Nature | Examples | Confirmation |
|---|---|---|---|
| **T0** | read | enumerate system, read files, search, `git status` | none |
| **T1** | reversible write | drafts, scratch files, edits under git in a declared workspace | audit log only |
| **T2** | consequential | move, archive, bulk rename, commit, run builds/tests | in-app confirm |
| **T3** | irreversible / external | **delete, send email, install/uninstall, admin change, send Telegram** | always |

Two binding rules on top of the tier:

1. **Unattended cap.** With no interactive desktop session, **only T0 and T1 execute**. T2 and T3
   queue for approval until the user returns.
2. **Channel rule.** **Telegram may request but never authorize T3.** A compromised Telegram
   session must not be a remote-delete capability. T3 executes only after confirmation at the PC.

Tiers are assigned by **deterministic code** in the policy gate (D5), not by the model. The
model proposes a tool call; the gate classifies and authorizes it.

### D14 — Obsidian vault as the human-readable memory substrate
The user's second brain at `E:\Obsidian_Vault` becomes the assistant's persistent memory — chosen
because it is plain markdown on disk, and because it makes every piece of assistant state
**inspectable and editable by the user in a tool they already use**.

Measured structure (2026-09-30): 53,492 `.md` files totalling 7.5 GB — but the distribution is
extremely skewed:

| Folder | `.md` files | Size | In scope |
|---|---|---|---|
| `datasets/` | 53,064 | 6.08 GB | **EXCLUDED — training corpus, not notes** |
| `.smart-env/` | 0 | 1.29 GB | **EXCLUDED** |
| `evernote/` | 319 | 56 MB | yes |
| `.opencode/` | 30 | 52 MB | yes |
| `recovery/` | 34 | 0.1 MB | yes |
| `execution_plans/` | 19 | 0.1 MB | yes |
| `copilot/` | 17 | — | yes |
| `docs/` (separate vault) | 6 | — | yes |
| `training/` | 1 | — | yes |

**The indexable knowledge base is ~430 notes, not 53,492.** This is what makes the vault usable
as an index at all.

Rules:
- **Index scope is an explicit allowlist.** `datasets/` and `.smart-env/` are hard-excluded by
  deny-path, not by size heuristic.
- The assistant writes its own state under a dedicated `Assistant/` folder in the vault —
  journal, plans, decisions, capability memory, friction log — all as notes the user can read,
  edit, correct, or delete by hand. This instantiates D5's auditability *and* requirement 4.
- **No Obsidian UI automation.** Direct file I/O only; Obsidian picks up changes itself.
- The main vault has **no community plugins** (`community-plugins.json` is empty), so there is no
  Obsidian Git safety net. The assistant therefore maintains **its own** snapshot/rollback, and
  writes append-only wherever the format permits.

### D15 — Live introspection over RAG for system knowledge
The user's strongest capability requirement is *"knowledge of the system it lives in"* and
*"local tech support and maintenance"*.

**A snapshot of a live machine goes stale within a day, and then confidently lies** — the worst
failure mode an assistant can have. Therefore system knowledge is obtained by **querying the
machine**, not by embedding an inventory of it:

| Tier | Content | Mechanism | Half-life |
|---|---|---|---|
| **Live** | programs, services, scheduled tasks, disk, RAM/VRAM, processes, git state | tool calls (`Get-CimInstance`, `Get-Service`, `winget list`, `nvidia-smi`, `git status`) | seconds |
| **Structural** | notes, `AGENTS.md`, `ARCHITECTURE.md`, schemas, manifests | `sqlite-vec` over the scoped note set, re-indexed on mtime change | hours |
| **Episodic** | requests, decisions, failures, outcomes | append-only `journal.jsonl` / `decisions.jsonl` | permanent |

RAG is used only for **durable documents**. Volatile state is never cached into an index.

### D16 — Delegation is a capability restriction, not an instruction
The user requires: *"It must not start doing any work itself, other than the planning,
orchestration and later on supervision."*

The strongest available guarantee is architectural, not rhetorical. **Plane B has no
code-editing capability inside a target project at all.** For project work its only capability
is `delegate()`. It therefore *cannot* do the work itself even if it wanted to; there is no
mechanism to do so.

This restriction applies to **project work only**. General administration (requirement 2c) and
teaching (2e) retain their own tool sets, since those are the assistant's own job.

### D17 — Honest capability boundaries
Stated plainly so the design is not built against a fiction:

| Requirement | Verdict on one RTX 3060 + a 4B fast tier and a 9B deep tier |
|---|---|
| Knowledge of the system | **Strong.** Mostly tool calls, not intelligence (D15). |
| Tech support / maintenance | **Strong**, for the same reason. Diagnosis = read state + reason. |
| Administration (files, mail) | **Strong.** Deterministic code executes; the model selects the tool. |
| Pedagogy | **Strong and cheap.** Small models teach well with tight retrieval and Socratic prompting. |
| Coding | **Weak.** Competent single-file work, explanation, review. Multi-file refactors of a large codebase will be wrong. Not to run unsupervised on production code. |
| Orchestrating real projects | **Not as the thinker.** Neither tier can plan a 113 KB-scale project well; the deep tier is a second opinion, not the planner. |

**Consequence:** the local assistant is the always-on front desk, personal automation layer, and
orchestrator-in-chief. The engineering labour happens in Plane D. This is not a compromise — it
is the correct division of labour, and it is exactly what D16 enforces.

### D18 — PyTorch is excluded from the stack, for two independent reasons
1. **Architecture.** `faster-whisper` is CTranslate2, `piper` and `openWakeWord` are ONNX
   Runtime. No component needs PyTorch. It would add ~2.5 GB of disk and a second accelerator
   runtime competing for the same GPU.
2. **It was the reason Laya was rejected.** See D19.

### D19 — Laya rejected as both intent router and safety gate
The original source document advocated Laya (~421 M non-autoregressive decision model) as a fast
pre-LLM router and safety gate. Verified against PyPI: it is real (Convai Innovations, v0.3.22,
keywords `decision-model, rlcd, routing, guardrails`) but **its own README states base checkpoints
score 0.36 and 0.35 against a 0.318 random baseline** — near chance — reaching 0.766 only after
fine-tuning. All releases are within one week of 2026-09-29 and it is pre-1.0. Its hard
dependencies include `torch>=2.0.0`.

Rejected. Routing and safety are **deterministic code** (D5, D13), not a 0.36-accuracy model.

### D20 — Reuse over reinvention
Existing assets are treated as templates to be ported, not rebuilt:
- The vendored supervisor (D12).
- The **3-level context allocation** from the S.E.A.S. orchestrator — Level 1 isolated / Level 2
  filtered / Level 3 full, keyed on `request_complexity` ≤4 / >4 / >7. This maps directly onto a
  9B's narrow context window and is ported near-verbatim.
- The **4 friction categories** (Structural, Epistemic, Interaction, Strategy) and **4 capability
  promotion levels** (L0 Observation → L1 Candidate Lesson → L2 Local Adaptation → L3 General
  Capability Update), which are *identical* across two independent sources. Ported as-is.

**Caution:** the quality figures circulating in the source orchestrator definition (routing
accuracy 95 %, workflow execution <50 ms, 100+ concurrent workflows, voice latency <100 ms) are
**aspirational claims, not measurements.** Nothing is designed against them.

### D21 — Self-improvement proposes; a deterministic gate disposes
Given that this agent will hold delete and email tools, unattended self-modification is not an
acceptable risk. Therefore:

- The assistant may write **only** to its own state directory. It may not modify its own code, and
  **may not modify its own policy** — enforced by deny-path in the policy gate, not by prompt.
- Self-improvement proposals are evaluated against a baseline (one change per cycle, verified)
  and **promoted only on evidence**, with `validation_score × confidence` gating and a ≤500-token
  `### OPERATIONAL CONSTRAINTS` overlay injected into the system prompt.
- Capability promotion requires: observed more than once, transfers across contexts, does not
  create harmful rigidity, and is validated.
- Named anti-patterns, carried over: performative reflection, reflection without change,
  overfitting (one failure → permanent rule), endless analysis.

## Part III - Model sizing (settled in v3)

### D22 - The brain is two tiers that take turns, not one resident model
A single 9B does not fit the D1 budget with the headroom the user requires, and two models cannot
co-reside either (4.17 + 5.96 = 10.13 GB). The resolution is **eviction-based tiering**, which is
already the mechanism D3 and the VRAM arbiter are built around:

| Tier | Model | Quant | File | mmproj | Purpose |
|---|---|---|---|---|---|
| **Fast** (resident by default) | `HauhauCS/Qwen3.5-4B-Uncensored-HauhauCS-Aggressive` | Q8_0 | 4.17 GB | 0.63 GB | admin actions, tool selection, voice turns, quick questions, most planning |
| **Deep** (on demand) | `DavidAU/Qwen3.5-9B-The-Defiant-Fable-Uncensored-Heretic-NEO-IMATRIX-MAX-MTP-GGUF` | IQ4_XS | 5.96 GB | 0.86 GB | hard decomposition, architecture trade-offs, adversarial review of a plan |
| Wildcard (Phase 1 only) | `llmfan46/gemma-4-E4B-it-ultra-uncensored-heretic-GGUF` | Q5_K_M | 5.37 GB | 0.92 GB | MoE — if it measures fast it may displace the fast tier entirely |

Escalation protocol: on a hard planning turn the arbiter **kills the fast tier, loads the deep tier,
serves exactly one turn, then the 90 s idle TTL kills it and the fast tier reloads.** Peak stays at
~6.7 GB, inside the 7.0 GiB ceiling. Cost is one model load (~5–15 s) per escalation; escalations are
rare because the fast tier handles most turns, so the amortised cost is a small tax on rare events.

This is the legitimate descendant of the original plan document's "Laya in front of the heavy LLM"
idea. The idea survives — a small fast model in front of a bigger slow one — but the 0.36-accuracy
model does not (D19). The fast tier is a real, measured, competent 4B rather than a router.

**Consequence for Plane D:** because the local brain is deliberately not a strong programmer (D17),
the delegation path is load-bearing rather than an optimisation. The 4B is good at *deciding* and
*explaining*; `opencode` workers do the writing.

### D23 - Sizing is set by measurement, but three sizing rules are already known
`llm-checker` was run against this machine. Two of its outputs are recorded here; one is rejected:

- **Accepted:** tier `MEDIUM`, best backend `NVIDIA CUDA`, 6 physical cores, AVX2 confirmed, driver
  616.92, fingerprint `cuda-rtx-3060-12gb`.
- **Rejected:** it reports `Detected GPUs: 2`, `Total VRAM 24GB`, `pooled safe envelope 22GB`. It has
  counted the single RTX 3060 twice (once via CUDA, once via its generic `systeminformation` path).
  There is one GPU and 12 GB. This is recorded because "22 GB available" is exactly the kind of number
  that would justify a model the user's own constraint forbids.
- **Rejected:** `Max model size: 10GB` assumes the whole card. The D1 ceiling is 7.0 GiB, and the
  measured RTX 3060 speed/VRAM curve shows a cliff above 8 GB where medians fall from ~200 to ~109
  tok/s output. The ceiling is not a guess.

Standing rules, all derived from measurement rather than preference:
1. **MoE does not buy VRAM.** All expert weights stay resident. "35B-A3B" is a *speed* claim, not a
   footprint claim — its most aggressive quant is 10.74 GB, which is over budget by 54 %. Any future
   "it's MoE, so it must fit" argument is wrong.
2. **A quant's file size is a floor, not a total.** Add 0.5–0.9 GB of KV cache and compute buffers
   before judging a candidate.
3. **Q8_0 over IQ for the resident tier.** The 4B at Q8_0 is 4.17 GB and essentially lossless; the
   9B is where the quant has to become lossy, because it only runs alone.
4. **A model newer than the bundled llama.cpp may not load** (D9 open risk). Phase 0 now proves
   `Qwen3.5` itself loads, not `Qwen2.5`, because every real candidate descends from Qwen3.5 — one
   test that closes both the CUDA question and the architecture question.

### D24 - These two tools are instruments, not dependencies
`F:\llm-checker-3.8.1` and `F:\llmfit-1.1.14` were read for numbers. No code was imported, no runtime
dependency taken, no path baked in (D10). Their findings are transcribed into D9, D22 and D23 and
after Phase 1 neither tool is required. Phase 1 keeps them available for cross-checking its own
measurements, which is their entire remaining value.

## Part IV - What Phase 0 actually proved

Phase 0 ran on 2026-09-30. **The gate is now PASSED** — `phase0_gate.py` exits 0. Three things that
were previously assumptions are now measurements, and one assumption is dead.

### D29 — D25 is confirmed, and the build must never be reverted to a wheel
D25 predicted that a source build with a portable CPU baseline would fix the crash. It did, and the
prediction is now verified rather than merely argued:

| Evidence | Prebuilt wheel | Our build |
|---|---|---|
| EVEX (AVX-512) sites in `ggml-cpu.dll` | 9,786 (76.7 % of `0x62` bytes) | **10 (1.02 %)** |
| Same scan on a control DLL known to be AVX-512-free | 2.74 % | 2.74 % |
| `ggml-cuda.dll` size | 819 MB (8 architectures) | **66 MB** (one) |
| `phase0_gate.py` | exit 1, `STATUS_ILLEGAL_INSTRUCTION` | **exit 0** |

Our `ggml-cpu.dll` now contains *fewer* EVEX-prefixed bytes than a DLL that is definitionally
AVX-512-free, which is the ordinary false-positive rate of the `0x62` byte and not a residue of the
instruction set. The fix is complete, not partial.

**The operational rule this creates:** a plain `uv pip install llama-cpp-python` would pull a
prebuilt wheel and silently reintroduce a crash that only appears on this CPU. The install must
always use `--no-binary :all:` plus the `CMAKE_ARGS` in `build_phase0b.bat`, and `probe_isa.py` is
the standing smoke test — it is now in the risk register. This is D25's generalisable lesson made
concrete: *verify every install on this CPU, never assume the import succeeded.*

**Two build attempts were required, and the first is the more instructive one.** Attempt 1 configured
CMake successfully and printed `Found CUDAToolkit ... 13.3.73`, then failed with `No CUDA toolset
found`. It had silently selected the **Visual Studio** generator, which does not invoke `nvcc`
directly — it requires a CUDA toolset registered *inside* the Visual Studio installation, and
`nvcc` merely being on PATH is not sufficient. That is exactly why the toolkit was discovered one
line before the failure: the two mechanisms are different. Fixed by switching to the **Ninja**
generator. A second trap came with it: the VS generator is multi-config, so it ignores
`CMAKE_BUILD_TYPE`; under Ninja an empty build type silently produces an unoptimised build, so it is
now pinned to `Release`. Both flags are in `build_phase0b.bat` with the failure they address in a
comment.

### D25 — The prebuilt wheel cannot run on this CPU; we build it ourselves
**This is the important one, and it supersedes D2.** (Proven; see D29 for the confirmation.)

The symptom: every load dies at context construction with
`OSError: [WinError -1073741795]`, i.e. `0xc000001d` = `STATUS_ILLEGAL_INSTRUCTION`. That is a
hardware fault — the CPU tried to execute an opcode it does not have.

The diagnosis, in order, each step eliminating an alternative:

1. **Not the model, not the architecture, not VRAM.** The GGUF parses perfectly and
   `llama_print_system_info` reports `arch = qwen35`, `n_layer = 24`, correct tensor shapes.
2. **Not CUDA.** A run with `n_gpu_layers=0` — pure CPU, no CUDA code path whatsoever — fails with
   the *identical* error code at the *identical* call site. The GPU is exonerated.
3. **This CPU has no AVX-512.** Probed directly: `np.longdouble(1).nbytes == 8`. A Ryzen 5 5600G
   is Zen 3; Zen 3 has no AVX-512. The maximum x87 width is 80-bit.
4. **The wheel's CPU backend is compiled with AVX-512 anyway.** `llama_print_system_info` prints
   `AVX512 = 1`, which in ggml reflects a *compile-time* `__AVX512F__`, not a runtime check. The
   build machine had AVX-512; the flag was baked in and shipped to every user.
5. **Direct binary evidence.** Scanning `ggml-cpu.dll` for EVEX-prefixed instructions (the 4-byte
   AVX-512 prefix `0x62` followed by a valid EVEX payload byte) against a control DLL that
   certainly has no AVX-512:

   | DLL | Size | `0x62` bytes | EVEX-shaped | Ratio |
   |---|---|---|---|---|
   | `ggml-cpu.dll` (the wheel's) | 1.01 MB | 12,754 | **9,786** | 76.7 % |
   | `cudart64_12.dll` (control) | 0.57 MB | 621 | 17 | 2.7 % |

   A megabyte of x86 code containing ~9,800 AVX-512 instruction sites is an entire AVX-512 kernel
   set compiled in. The control's 2.7 % is the ordinary false-positive rate of the `0x62` byte.
   This is not a theory any more.

**Consequence:** the `abetlen` prebuilt wheels are unusable on this machine, at every version on
the cu124 index that carries Qwen3.5 support. The fix is a source build with a portable baseline:

| Flag | Value | Why |
|---|---|---|
| `GGML_NATIVE` | `OFF` | stops `-march=native` baking the build machine's ISA in |
| `GGML_AVX512` | `OFF` | explicit, so it does not depend on the default |
| `GGML_AVX2` | `ON` | Zen 3 genuinely has it; we keep the speed we are entitled to |
| `CMAKE_CUDA_ARCHITECTURES` | `86` | only the 3060. The wheel ships `sm_600…900`; we need one |
| `CMAKE_CUDA_COMPILER` | CUDA 13.3 `nvcc` | matches the newest installed toolkit and MSVC 14.51 |
| `LLAMA_CURL` / tests / examples / server | `OFF` | nothing in this project opens a socket (D3) |

Building for a single CUDA architecture instead of eight is also the single biggest lever on
compile time, which matters on 6 cores.

**Generalisable lesson, and the reason this is a decision rather than a note:** a prebuilt binary
inherits its producer's assumptions. Nobody at `abetlen` was targeting a Zen 3 desktop, and there
was no reason for them to notice. This is the same failure class as D9 (a model newer than the
runtime) and D10 (a tool whose output contradicted reality): **a third party's claim about your
machine is an input to be verified, never a premise.** Every install from here on gets a smoke
test that runs *on this CPU*, not an import check.

### D26 — The CUDA runtime must be on PATH, and the version must match the build
`import llama_cpp` failed first with `Failed to load shared library llama.dll: Could not find
module`. The wheel's `lib\` directory is complete — `llama.dll`, `ggml.dll`, `ggml-base.dll`,
`ggml-cpu.dll`, `ggml-cuda.dll` (819 MB), `mtmd.dll` all present. The missing piece was the CUDA
**runtime**: only `CUDA\v13.3\bin` was on PATH, and the cu124 wheel wants
`cudart64_12.dll` / `cublas64_12.dll` / `cublasLt64_12.dll`, which live in `CUDA\v12.8\bin`. Both
toolkits are installed side by side and coexist because the DLL names differ per major version.

Because D25 now builds against CUDA 13.3, the 12.8 entry is only needed for the old wheel. The
general rule, which the daemon must implement: **the assistant launches its own model children
with a PATH it controls**, prepending the exact CUDA toolkit it was built against. It must never
inherit whatever PATH the desktop session happens to have, or it will break the first time the
user installs a different CUDA version.

### D27 — Qwen3.5 is a hybrid SSM model, so KV-cache arithmetic is invalid
Measured from the loaded model, not assumed:

```
ssm_d_conv = 4      ssm_d_state = 128     ssm_dt_rank = 16
ssm_n_group = 16    ssm_d_inner = 2048    full_attention_interval = 4
mrope sections = [11, 11, 10, 0]         n_ctx_train = 262144
```

Only every **4th** layer is full attention; the rest are Mamba-style linear-attention SSM blocks
with their own conv, decay and gate tensors. The tensor list confirms it — even blocks carry
`attn_qkv` plus `ssm_conv1d` / `ssm_dt` / `ssm_a` / `ssm_beta` / `ssm_alpha`; odd blocks carry
separate `attn_q` / `attn_k` / `attn_v` and no SSM tensors. The vocabulary also carries
`<|vision_pad|>`, `<|audio_pad|>`, `<|video_pad|>` and `<tts_*>` tokens, confirming the
any-to-any multimodality that D4 relies on.

**What this invalidates:** every closed-form KV-cache estimate in this document, including the ones
inside D1 and D22. A hybrid model does not allocate a KV cache per layer, so the familiar
`2 × layers × kv_heads × head_dim × ctx × bytes` calculation is simply the wrong formula. It is not
that the answer came out differently — the question does not apply.

**What survives:** the *ceiling* in D1, which was never a calculation. It is a line drawn for the
user's benefit, and it is unaffected by how the model spends its memory. The 4B/9B file sizes in
D22 are byte counts from Hugging Face and remain exact.

**New rule:** for hybrid architectures, **VRAM is measured, never modelled.** This promotes Phase
1's 4K-vs-8K context measurement from a nice-to-have to a load-bearing gate, and it means the
D22 headroom figures are provisional until measured. The hybrid structure may well be kinder to
VRAM than a plain GQA model at the same parameter count, in which case there is more room than
D22 assumed. That is a pleasant possibility, not a plan.

### D28 — Phase 0 timing numbers taken now are invalid
Two unrelated `uv` processes belonging to the user's `F:\Wan2GPT` project were running for the
whole of Phase 0, saturating the network and all 6 cores. They are not ours and were correctly left
alone. Consequence: **no Phase 0 or Phase 1 timing measurement taken on this machine is
trustworthy until the machine is quiet.** The structural results (architecture support, full
offload, the crash) are unaffected — those are not timing. Phase 1 must take its speed numbers
with a quiet machine, and must record that it did so.

For the record, the 223 tok/s figure measured *during* that contention is therefore an optimistic
lower bound on an unloaded machine, not a baseline. It is quoted in the Phase 0 results table only
to show the GPU was doing the work; it must not be used to predict interactive latency.

## Part V - Decisions taken while building Plane A

These came out of writing the code rather than out of planning it, which is why they are recorded
here rather than in Part I. Each one is a case where the obvious choice was the wrong one.

### D30 - The hotkey is a Win32 `RegisterHotKey`, and presence is read from the OS
Both are about the same thing: **the assistant must not be able to lie about who is in front of it.**

**The hotkey.** `Ctrl+Alt+Space` (D8) is registered through `ctypes` against `user32.RegisterHotKey`,
not through the `keyboard` library, which *is* installed and which Phase 5 will use for the wake word.
`RegisterHotKey` was verified to return success in a non-elevated session on this machine. Three
reasons, in order of importance:

1. `keyboard` requires administrator rights. Silent elevation is out of scope (PLAN.md), and a hotkey
   that only works in an elevated prompt is not a hotkey - it is a feature you cannot use.
2. It installs a low-level keyboard hook, so the process observes every keystroke, always. For a
   resident 24/7 daemon that is a standing liability, and it buys nothing when we want exactly one
   chord.
3. It is a dependency for a single integer.

The generalisable form: **prefer the mechanism with the smallest blast radius.** A process that can
only ever see one specific chord cannot be turned into a keylogger by a later refactor. That property
is worth more than the convenience of a library, and it is the kind of thing that is cheap to decide
correctly now and expensive to retrofit.

Two Win32 details that are not optional: `MOD_NOREPEAT` (0x4000) must be set or holding the chord
fires repeatedly, and the message pump must run on **the same thread that registered the hotkey**,
because hotkey registration is per-thread. Both failure modes look identical from the user's side -
"the hotkey sometimes doesn't come" - which is why they are written down.

**Presence.** D13's unattended cap needs to know whether a human is at the keyboard. Asking the caller
over the transport would be worthless: whoever sends the request can simply claim to be present, and
the whole ceiling becomes decorative. Presence is therefore read from `GetLastInputInfo`, which the
OS maintains and no message can forge. Consequences:

- `session.set_attended` may only **narrow** authority. Only `CHANNEL_PC` may assert presence; a
  remote channel requesting `attended=true` is **refused**, not ignored.
- Presence transitions go through the audited op, so the log reconstructs who was there and when.
- **A failed presence check does not mark the session unattended.** The honest response to not knowing
  is to keep the current state, not to assume the worst and silently stop working.

### D31 - The tray is a client of the daemon, never a second front door
Every tray action calls `daemon.handle` with `actor="tray"` and `channel="pc"`, exactly as a pipe peer
does. There is no privileged path from the icon to the filesystem. A tray that could do more than the
pipe would be a tray nobody audits, and the audit chain is the only evidence this system produces about
itself.

The same principle appears in three other places and is worth naming as one decision rather than four
coincidences: the manifest store has **no discovery method at all**; the gate recomputes
`allowed_roots` from the manifest store on **every** call rather than caching at construction; and a
tool with no executor raises "not implemented" rather than returning an empty success. In all four
cases the failure being avoided is the same one - **a component that appears to have done something it
did not do.** That is the failure that makes an assistant untrustworthy, and it is much more dangerous
than an error, because an error is visible.

For the same reason the tray's "Open prompt" item reports that the overlay is not built instead of
showing stale status, and `status` exposes `tools_executable` (3) alongside `tools_registered` (~58).
The gap is meant to be visible.

## Part VI - Confirmation (D32)

### D32 - Confirmation is a foreground, payload-bound, single-use human act
A confirmation is the highest-authority moment in the system. Everything below exists because the
failure mode is **not an error** - it is the system confidently doing something the user did not
sanction. An error is visible. A confidently wrong confirmation is not, which makes it the failure
this project has to be most afraid of. Settled with the user; the constants are in `overlay.py`.

**1. The dialog shows the resolved absolute path, never the model's string.** The model proposes
`Downloads\tmp`; `tmp` is a junction to `Documents\Thesis`; the executor follows it and the thesis
is gone. Nothing lied - the model, the dialog and the code all said what they meant, and the
*resolution* differed. So the dialog shows the resolved path, shows the model's string beside it when
they differ (a difference is a finding, and the user is the one who can say "that is not what I
meant"), and **enumerates the matched files rather than showing the pattern**: `Downloads\*.tmp`
renders as "3 files, 12.4 MB" with the names, because a human approving *3 files* and a human
approving *11,000* are making different decisions and only the first is informed.

**2. The approval is bound to a hash of the payload.** Displayed payload is hashed; the executor
re-resolves and re-hashes at execution; a mismatch **voids the approval** and the dialog returns. This
closes the gap between what was shown and what runs, and it is the reason an approval can never be
replayed against a different set of files.

**3. Timeout denies, and a denial is loud.** Expiry is a refusal, never an approval - a confirmation
that expires into approval is a footgun, and one that expires into denial is merely annoying. If the
session is **unattended**, the dialog waits indefinitely instead, because expiring unattended is the
same footgun with a longer fuse. A denial always posts a notification naming the tool and the reason
and leaves the request in the queue, so a timeout costs work but never silently. **One dialog at a
time**: a second T3 queues behind the first, because two stacked modals is how the wrong thing gets
approved.

**4. Three approval scopes, and the third does not exist.**

| Scope | Binds to | Chosen how |
|---|---|---|
| This call | exactly this payload, once, via the hash | default button |
| This class, 600 s | `(tool, root, operation-class)` for 10 minutes | deliberate second button, never the default |
| Always allow | - | **removed. No permanent T3 grant exists.** |

The middle scope needs two invalidation rules or it is a slow-motion permanent grant: **any T2+
action voids it** (the session has left the context the user approved for) and **loss of presence
voids it** (the grant meant "I'll be here for a few minutes", and they are not). Both log start and
expiry so the audit chain reconstructs which calls ran under which grant.

Single-use is the default because the alternative is worse in both directions. Too strict turns
"delete 40 files" into 40 dialogs and trains the user to hit Enter without reading, which is worse
than no dialog. Too loose hands the assistant a standing delete capability over every approved root.
600 s is the user's number, not a default I picked.

**5. Only the foreground dialog can authorize.** The tray may *show* that a confirmation is pending
and may *raise* the overlay, but it can never approve or deny - a transient one-click toast is the
exact shape of a clickjacking surface, and once Telegram is live anything that can reach the pipe
could pop one and wait. So: **only a human at the PC, looking at a foreground dialog, authorizes a
T3 action.** Not the model, not the supervisor, not a worker, not the tray, not Telegram. If the
dialog is not on screen, nothing is approved.

### D33 - `file.delete` quarantines instead of unlinking
Making D32 testable required at least one real T3 executor, and hard-deleting is the wrong
implementation for a system whose whole premise is reversibility. `file.delete` **moves** the target
into `state/quarantine/<timestamp>/<relpath>` and writes a manifest; `file.restore` puts it back.
The user-visible result is the same (the file is gone from where it was), and the difference is that
a mistaken T3 is a move rather than a loss.

This also makes the destructive path *testable*, which a real `unlink` would not be. The quarantine is
itself inside our own state directory, so the policy gate's self-path protection covers it: the
assistant cannot delete its own source, its own decisions, or its own quarantine through this tool.


## Part VIIa - What Phase 1 measured

### D34 - The assistant is grammar-first. Native tool-calling is the fallback, not the reverse.
Phase 1 gated the fast tier on *competence*, not on being good, and competence failed. The decisive
experiment (`probe_tools.py`, then `probe_grammar.py`) asked three questions of the same 4B on the
same prompt:

| Decoding constraint | Result |
|---|---|
| `tool_choice="auto"` | **No call.** It narrates: *"I need to use the file_list function with the path parameter set to ..."* |
| `tool_choice` pinned to `file_list` | Clean native call, correct arguments |
| **GBNF grammar-constrained** | **Clean call, every time, in all three variants tried** |

The reason is structural, not statistical. Prose is not a legal token sequence in a call position, so
a grammar-constrained decode does not make the model *better at deciding* to call a tool. It removes
the option of not committing, which is the only thing that was actually broken.

The strongest evidence is the third variant: a raw grammar with **no `tools=` at all**. The model was
never told a tool existed and still produced a correct call, because the grammar admits nothing else.

**Consequences, and they are not small:**
- The production grammar is **generated from the live tool registry**, never hand-maintained. A
  grammar hand-written beside a tool list is a grammar that will be wrong in six months, and it will
  fail in the one place where failing is unacceptable.
- **The call arrives in `content`, not in `tool_calls`.** The brain adapter parses content. Anything
  written against native `tool_calls` will silently see nothing.
- Native `tool_calls` with a pinned `tool_choice` is **kept** for the case proven to work, not
  removed. Two mechanisms, each used where it is measured to hold.
- I had earlier dismissed the source document's GBNF grammar as a toy. That judgement was wrong and
  the correction is recorded here rather than quietly overwritten: the original author was right about
  the mechanism, and wrong only about the model that would consume it.

### D35 - A VRAM ceiling is about what we consume, not about the board total
The first version of the Phase 1 gate compared **board total** VRAM (6.97 GiB) against the 7.0 GiB
ceiling and called it a failure with 0.20 GiB spare. That reading was wrong, and the user's own words
settle it: *"if it ends up taking all my VRAM and DDRAM for itself, how am I going to get it to do
anything that will also require such resources?"* The constraint is about **what the assistant takes**,
not about what the user is already running.

Measured, with both numbers kept visible: **2.00 GiB was already in use by the user's own software
before we loaded anything.** Our own consumption was **4.92 GiB** for a 4.17 GiB file, leaving
**2.08 GiB spare** under the ceiling. Read as a board total, D1 would have been "violated" by the
user's own workload, which is exactly the situation the rule exists to prevent.

**The rule:** every VRAM gate measures the **delta we caused** and additionally reports the board
total, so the distinction cannot be quietly lost by the next person who reads a number.

### D36 - A silent no-op in a constraint API is worse than an error
Passing `grammar` **nested inside `response_format`** to `create_chat_completion` is **silently
ignored** - no error, no warning, no log line. The model then produced output that plainly did not
match the grammar (one run invented `{"files": ["README.txt", ...]}`), and the natural reading is
"the model cannot do this" rather than "the constraint was never applied."

The correct form is a **top-level** `grammar=` kwarg taking a `LlamaGrammar` object
(`LlamaGrammar.from_string(...)`). A raw `str` dies loudly in the sampler, which is the better
failure.

Generalised: **for any mechanism whose whole job is to constrain output, a silent no-op is the worst
possible failure mode**, because the system keeps working and keeps returning plausible output. Every
such mechanism here must be proved active before it is relied on. That includes the grammar, and it
is why `probe_grammar.py` checks the emitted shape rather than trusting that the flag was set.

### D37 — The fast tier's speed, measured on a quiet machine (D28 satisfied)
D28 voided every timing number taken while the user's `Wan2GP` project was building. The user shut it
down and `phase1_measure.py` was re-run three times. Machine verified quiet first: CPU load 4–10 %,
and the lingering `G:\Wan2GP-Tauri` python process accumulated **0 s of CPU across three 3-second
samples** — alive, not working.

| Measurement | Contended (voided) | **Quiet (valid)** | Spread over 3 runs |
|---|---|---|---|
| Generation, 92 tokens | 58.28 tok/s | **57.85 tok/s** median | 56.51 – 58.07 (2.6 %) |
| Generation, 34 tokens (grammar call) | — | **58.81 tok/s** median | 58.77 – 58.88 (0.2 %) |
| Prefill, 427 tokens | 1076.83 tok/s | **1540 tok/s** median | 1477 – 1551 (5.0 %) |
| Prefill, 37 tokens | — | **1096 tok/s** median | 1091 – 1108 |
| Per-token generation | — | 17.2 – 17.7 ms | — |
| Model load | — | 2.5 – 2.8 s | — |
| Our VRAM at 4K ctx | 4.92 GiB | **4.92 GiB** median | 4.90 – 4.94 GiB |

**The contention penalty was real but sharply asymmetric: generation was essentially unaffected
(58.28 → 57.85, i.e. within noise and marginally *faster* when quiet), while prefill gained 43 %
(1077 → 1540 tok/s).** That is the expected shape — prefill does tokenisation and batch preparation
on the CPU, whereas a fully offloaded generation pass is GPU-bound. The lesson for D28's successor is
that "the machine is busy" means different things for different measurements, and a blanket caveat
hides that.

**Prefill being ~27× generation is the number that matters for this project.** The dominant workload
is not long answers, it is *long context*: 429 indexed vault notes, project manifests, tool schemas.
At 1540 tok/s a 4K-token retrieval prompt assembles in under 3 seconds, while a 400-token answer takes
about 7. The architecture is shaped accordingly — tight retrieval, short spoken and typed replies.

**Tool-call latency is 4.5 – 6.3 s end to end, and that is dominated by model load (2.5 s) plus
first-token prefill, not by the grammar.** The grammar adds no measurable cost. This is the number
the D22 tier swap must be judged against, and it is consistent with the 5 – 15 s budget already
recorded for a full eviction-and-reload.

**A stale caveat is as misleading as no caveat.** The script had the string `(contended)` hardcoded
into its output row. The moment the machine went quiet that label became false, and a false caveat
reads exactly like a true one. `phase1_measure.py` now samples the CPU load itself and prints
`VALID` or `CONTENDED (cpu N%)` accordingly; if the load cannot be measured it assumes the worst,
because an unknown is not a quiet machine.
---

## Part VII — What it took to make the brain emit valid JSON

### D38 — A generated grammar is tested on its output, not on its shape

Three bugs, one cause, found only by looking at what the model actually emitted.

In GBNF, the quotes around a literal are **syntax**. They delimit the literal and the model never
produces them. So a rule written as

    toolname ::= "file.list"

makes the model emit `file.list` — which is not valid JSON, because a JSON string needs its own quotes
in the output text. The first live tool call came back as

    {"tool":
    file.list,
    "arguments": ...

`"tool":` was correctly quoted because `root` spells it `"\"tool\":"`, and the tool name was bare
because `toolname` did not. `json.loads` stopped at character 9 and the call was discarded at the last
step, having been forced successfully at every step before it.

The fix is `_json_quoted_literal`: JSON-escape the content, add the JSON quotes, then GBNF-escape the
whole thing so the quotes survive as content.

The same bug then appeared twice more, in argument keys and then in string values. The value case is
the subtle one. Writing it as the single literal `"[^"\\]*"` is **wrong**, and looks right: inside
double quotes a GBNF character class is just characters, so that rule matches the seven-character
string `[^"\\]*` and nothing else. A JSON string value takes three fragments:

    "\"" [^"\\]* "\""

And once the value carries its own closing quote, the separator between arguments must become a **bare**
comma, because a `"\",\""` separator emits `""` between the two.

Two things are worth recording beyond the fixes.

**The `args` dispatcher was never a constraint at all.** It read

    args ::= "file.list" args-file-list | "file.read" args-file-read

on the reasoning that `args` immediately follows `toolname`, so it could re-read the name. GBNF is a
forward CFG. It cannot look back at what a previous rule emitted, so the second occurrence is not a
match against the first — it is more text the model must produce:

    {"tool": "file.list", "arguments": "file.list"{"path": "C:/x"}}

Not JSON. And since the two occurrences were never required to be equal, it constrained nothing at
all while reading like the strictest part of the grammar. It is now a flat alternation of the argument
shapes, which means the grammar no longer ties a shape to the named tool. That is the right trade: a
mismatch is caught by `parse_action` against the registry's real schema, which is a *stronger* check
than the grammar was, because it sees the tool that was actually named rather than only what the
grammar would have permitted. A constraint that reads like a guarantee and is not one is worse than a
weaker constraint that is honest about itself.

**Every one of these compiled, and every one passed every shape assertion.** The question "does the
grammar look right?" was asked three times and was the wrong question three times. What holds is
running the *emitted* text through `json.loads` in the tests — reducing each generated rule to the text
the sampler will produce and parsing the result. That renderer was itself wrong three times (it
substituted whole value rules instead of the class inside them; used one stand-in for every class, so a
path landed where a number belonged; emitted optional literals unconditionally, so `-` prefixed an
optional argument), and each of those produced a test failure that looked like a product bug. That is
in its docstring, because a helper that finds three phantom bugs and a generator that contains three
real ones should both be readable.

This is D36 again, one level up. D36 said a constraint mechanism must be *proved active* before it is
relied on. D38 says the same about the thing the constraint produces: a generated artefact must be
tested on the output it will become, because a generator and its output are two different things, and
only one of them is what a user will see.

### D39 — A timeout on a confirmation must expire into a denial

The overlay's T3 dialog counts down from 60 seconds. At zero it denies.

This is not a UI preference. A dialog that expires into *approval* is a footgun: the user walked away,
the countdown ran out, and an irreversible action happened on their behalf while the audit log recorded
them as having consented. One that expires into a denial costs the user a moment of attention and
nothing else, which is not a real cost.

Two consequences, both implemented and both tested:

* **No timer at all while the session is unattended.** A countdown the user cannot see is worse than
  no countdown, because it looks like the same thing.
* **The denial is loud.** An expiry posts a visible message naming the tool, so a timeout can cost work
  but can never pass silently. The rest of D32's rules hold unchanged: one dialog at a time (a second
  T3 queues rather than stacking), the resolved path shown beside the model's string when they differ,
  globs enumerated rather than shown as a pattern, and no permanent grant.

The queued-dialog rule needed care. Marking a confirmation as *seen* the moment it was offered — so the
poll loop would not raise it twice — meant a second queued request was recorded, refused a dialog
because one was up, and then skipped by every later poll. A pending destructive action with no way to
reach it. The state kept is now `_answered`, and the poll offers the oldest unanswered request
whenever no dialog is open.---

## Part VIII — The assistant can finally read the machine

### D40 — Nothing listens; exactly one component makes outbound calls, to one host

D3 said "no TCP anywhere", and that remains exactly true of how our components
talk to each other: named pipes, never sockets. Telegram changed the shape of the
world, because talking to a bot API needs an outbound HTTPS request, and a rule
that has quietly stopped being true is worse than no rule.

So the rule is narrowed and stated precisely rather than left to be discovered as
an exception later. Three parts:

* **Nothing listens.** No component binds a port. Ever.
* **Exactly one component makes outbound calls** — `assistant.telegram` — and it
  does so to one hardcoded host, `api.telegram.org`. `_api()` builds the URL from
  that constant, so a URL assembled from a message can never send the token
  somewhere else.
* **The default TLS context is not replaced.** A context that skipped certificate
  or hostname verification would put the bot token on the wire to anything that
  answered, and the token is the whole of the access control.

This is D3's intent — minimise the surface an outside party can reach — restated
so that a future reader can check whether it still holds.

### D41 — A Telegram message may ask; it may never authorise, and it may never claim presence

Both follow from the user's own account of how they will actually use it:

> "the bigger odds are that usually i wouldn't be around the pc if i'm grabbing a
> coffee or something, not because i'd be sleeping"

That is a different situation from sleeping, and it produces two different rules.

**It never authorises.** A T3 arriving by Telegram becomes a *queued*
confirmation. The message says so, names the confirmation id, and `/approve`
exists and refuses. It exists deliberately: a command that was never implemented
is indistinguishable from one that was removed, and the user is entitled to know
this one was considered and declined. The refusal is audited.

**It never claims presence.** The bridge does not call `session.set_attended` at
all — not even to mark the session attended when the user is plainly talking to
it. The commonest reason to be away is ten minutes with a phone in your pocket,
and the machine is unattended throughout. Treating that message as presence would
unlock T2 and T3 on a machine nobody is watching, which is exactly the failure
D30 exists to prevent. T0 and T1 run from the coffee shop; T2 and T3 queue.

The allowlist is *silent* — a stranger gets no reply at all, because a polite
refusal confirms the bot exists and echoes back that the chat id was recognised.
An empty allowlist is refused at construction, because it would authorise nobody
correctly and everybody if the check were ever bypassed.

### D42 — The tool name is settled before the arguments are generated

This one was found the hard way, and it is the most consequential correction in
the project so far.

The grammar's `args` rule had been flattened into a plain alternation over every
tool's argument shape, because GBNF is a forward CFG and cannot match the name
`toolname` just emitted. The reasoning at the time was that `parse_action` would
catch a mismatch against the registry's real schema.

**It did not.** `parse_action` parsed JSON and returned whatever came out. The
claim existed in a comment; the code behind it did not. Measured on the real 4B,
asked to check the machine's health:

    {"tool": "system.diagnose",
     "arguments": {"content": "...", "path": ""}}

The `file.create` shape, attached to `system.diagnose`. Nothing complained. The
executor ignored both keys. The call reported **success** while doing something
other than what the model said — which is the failure D31 exists to prevent, and
which is worse than an error because an error is visible.

Two fixes, and the second is the better one:

1. **`parse_action` now validates** against the registry: unknown tool, undescribed
   tool, an argument the tool does not take, a missing required one, a value of
   the wrong type. Each is a returned error naming what the tool *does* take, so
   the model has something to act on next time.

2. **The name is decided in pass 1, so pass 2 has exactly one tool in scope.**
   `build_named_decision_grammar` admits either the bare word `answer` or
   `tool:<name>`, and `build_tool_call_grammar(name, tools)` then generates a
   grammar containing only that tool's shape. The model cannot pair a name with
   another tool's arguments, because nothing else is in the grammar. This costs
   no extra pass — the decision and the name are the same choice — and one extra
   token of constraint.

Effect, measured: 2 of 5 system questions produced a call before this, 5 of 5
after. The two that failed did so with the *right tool name* and another tool's
arguments, which is precisely what fix 2 removes.

### D43 — A generated artefact is tested on what it will become, not on how it looks

D38 said this for the grammar generator. Four more instances turned up in one
session, and they have a common shape worth naming on its own:

> **The generator emitted something legal in its own language that the consumer
> cannot accept.** Each compiled. Each passed every assertion about the
> generator's output *shape*. Each produced a document that would not parse.

| What | Emitted | Why it fails |
|---|---|---|
| `bool` as `(true\|false)` | bare `true` | in GBNF a bare word is a **rule reference**; `from_string` printed "Undefined rule identifier 'true'" to stderr, returned a good object, and the child segfaulted at the first sample |
| string class `[^"\\]*` | permits raw newlines | a newline inside a JSON string is illegal, and the model produced one |
| optional tail `( ws "," ws key )` | leading comma | correct only when a required argument precedes it; on an all-optional tool it emitted `{ ,"limit": 100}` |
| `[0-9]+` | unbounded | asked about RAM, the model emitted a 500-digit `limit`, spent the token budget on zeros and stopped mid-number |

The `bool` one survived review for a specific reason worth recording: the first
grammar module carried a `_NON_RULES = {"true", "false", ...}` exemption listing
them, with a comment calling them "terminals that appear inside character
classes". **That was a check weakened to agree with the broken output** — the
check could not fail, because it had been told the bug was intended. The
exemption is deleted rather than corrected, because a list of names a module
promises not to check is a list of bugs it will never report.

The optional-comma one survived because every render test built a tool that
happened to have a *required* argument. The fix for that class of gap is
`test_every_tool_in_the_registry_renders_as_valid_json`, which walks all 23 and
asks only whether the rendered text parses. Adding it immediately found a second
bug (the missing required-to-optional comma) that no hand-written test would have
covered.

The general rule: **a test that exercises one hand-picked instance is a test of
that instance.** Where a generator emits one artefact per registry entry, the
test must walk the registry.

### D44 — The machine's own answers are measurements, and a probe that fails says so

`assistant.sysinfo` reads the machine. The rule is D36 applied to measurements:
**a probe that fails returns an error, never an empty result.** `{"error":
"nvidia-smi not found"}` and `{"vram": null}` look identical to a caller, and only
one means "check something else".

`system.diagnose` is the same rule applied to conclusions. It returns `checked`,
`unchecked`, and `complete`, because a health check that quietly checks four of
six things and reports "all clear" is worse than no health check — it gets
believed. A finding is only ever raised by a probe that actually returned data.

Two implementation notes that are load-bearing rather than incidental:

* **`nvidia-smi` is never mocked away in the tests.** Every probe here answers a
  question about the hardware it is standing on, and a mocked `nvidia-smi` proves
  only that the mock returns what the mock returns. The failures that matter — a
  column that moved, a locale, a field that is `[N/A]` — cannot be faked into
  existence. `tasklist`'s `"8 K"` / `"3,648 K"` column is the worked example: the
  probe had never worked, and reading the real output is the only thing that
  found it.
* **`Win32_Product` is not used** for the package inventory, though it would
  enumerate installed programs. It is a repair operation that reconfigures every
  MSI package it finds — minutes on this machine, and it can break things. The
  uninstall registry keys are what Windows itself uses.
## D45 - Two tiers means one tier at a time, and the swap is the interesting part

The deep tier is 5.96 GiB of file. The fast tier occupies 4.92 GiB. A 12 GiB card
cannot hold both, so "we have two models" is not a statement about simultaneity -
it is a statement about *choosing*. The interesting behaviour is therefore not
the loading, which `llama.cpp` does, but the transition.

**A tier change stops the old child before starting the new one.** Not "starts it
and the arbiter sorts it out". The ordering is the claim: the deep model only fits
once the fast model's memory is actually gone, and a request made while it is
still held is a denial, correctly issued, for no reason the user can act on.

**One child, not two.** The alternative - a fast child and a deep child side by
side - cannot be made to work on this card, and pretending otherwise would show up
as an arbiter denial the user could do nothing about.

**A lease is sized to the ceiling the tier may occupy, not to the file it loads.**
The gap is measured, not assumed: on the 4B a 4.17 GiB file occupies 4.92 GiB,
the difference being the CUDA context and the KV cache. Requesting the file size
would under-ask by most of a gigabyte and let us exceed D1 while every number on
the books said we were under it.

**An unknown tier name is refused, never defaulted.** A silent fallback means the
caller asked for the better model, got the worse one, and has no way to tell -
which is worse than an error, because the answer still looks like an answer. The
two tiers are only worth having if they are distinguishable, and a fallback is
precisely a way of making them indistinguishable.

**The daemon names a tier; the child names a file.** `--tier deep`, never a path.
A second copy of the model table in the daemon would be a second answer to "which
models exist", and the two would drift apart in exactly the way that produces a
model nobody can load.

Two consequences worth stating plainly:

* **Conversation gets the fast tier; anything touching files asks for the deep tier
  by name.** Not inferred from the prompt text. A heuristic that guesses when to
  spend 6 GiB is a heuristic that will eventually guess wrong on a quiet question,
  and the cost of guessing wrong is the user's card.
* **Whether the deep tier earns its place is a measurement, not an assumption.** The
  4B picks the right tool and gets the arguments right about half the time - a
  remembered `Desktop` path instead of one it was given. If the 9B is not
  meaningfully better at that, the honest outcome is one tier and less machinery,
  and that outcome is preferable to a second model that is merely expensive.

---

## D46 - The child watches for its own parent

D3 says VRAM comes back when the child dies, and the idle TTL is what makes it
die. But the sweeper that enforces the TTL runs *in the daemon*, so a daemon that
dies takes the sweeper with it - and the child survives holding ~5 GiB with
nothing left to reclaim it. Observed twice in one session: a crashed probe left
two orphans holding the pipe, and the card stayed busy until they were found by
hand. D3's premise assumes the parent outlives the child, which is exactly what a
crash denies.

**The child checks whether the process that started it is still running.** Not
`os.getppid()`: Windows does not reparent orphans the way Unix does, so a child
keeps its dead parent's pid forever and that check reports a parentless child as
having a live parent indefinitely. A kernel handle is held instead and asked
whether the process has exited.

**A handle that will not open means death, not life.** Every way of arriving at
that branch other than "the parent is running" means the child has lost the thing
that owns it, and the failure that matters is the child surviving.

**Checked every two seconds**, not every tick of the 250 ms loop. `OpenProcess` is
cheap but not free, and two seconds is not a meaningful delay against a 90-second
TTL. A Job Object would be the stronger guarantee - kill-on-close, enforced by the
kernel - and is the right answer for a build that must survive a hard power loss.
This is the belt to that braces, and deliberately the smaller piece.

Verifying it took three attempts, and **each failure was the test, not the code**:
spawning the detector directly makes the test its own parent; `taskkill /T` is a
deliberate tree-kill that would not happen in production; and a venv `python.exe`
re-execs the base interpreter, so the detector's real parent was a wrapper one
level below the process that was killed. The detector was right every time. The
evidence that mattered was `orphans still running: 0`, not the printed line - a
grandchild inheriting a killed parent's stdout pipe will lose the very line you
are looking for.

---

## D47 - The kernel, not a poll, is what brings the VRAM back

**The strong version of D46 is a Windows job object with `KILL_ON_JOB_CLOSE`, and
it is armed - not the polling.** Polling needs a live process to poll, so it cannot
cover `TerminateProcess`, a power loss, or a bluescreen. The job is enforced by the
kernel: the child dies the instant the last handle to it is released. The daemon
holds that handle; there is no other way for the child to be killed. Polling stays
as the fallback for when the job cannot be created, and every failure to arm it is
audited rather than swallowed (D44).

**The flag was in the wrong field for a while, and nothing noticed.** It was written
at byte offset `0x28`, which is `ActiveProcessLimit`; `LimitFlags` is at `0x10`.
`SetInformationJobObject` accepted the structure and returned success, so the guard
looked installed and armed nothing. It was found by asking a question the code had
never been asked: *what flags are actually stored?* Byte offsets into a C structure
are replaced by real `ctypes` declarations, and `job_limit_flags` exists so the
answer is observable rather than assumed. A claim that cannot be read back is not a
claim.

The same class of error bit twice more, so it is named: a Win32 HANDLE does not fit
in a 32-bit `int`, and ctypes returns one by default. The symptom is
`ERROR_INVALID_HANDLE` from the very next call, which reads like "the API refused"
rather than "the caller truncated it". Signatures are declared once, in one place,
instead of at each call site.

---

## D48 - The deep tier does not earn its place, and that is the better outcome

Measured, because D45 promised the decision would be a measurement. Same five
questions, same workspace, same gate, both tiers:

| | fast (4B) | deep (9B) |
|---|---|---|
| reached a human or the gate | **5/5** | **5/5** |
| our VRAM | 5081 MiB | 6419 MiB |
| total time for the five | 23.6 s | 29.6 s |
| wrong tool chosen | 0 | 0 |

And on the questions that should separate a small model from a large one -
ambiguity, absence, a place it was never told about:

| | fast | deep |
|---|---|---|
| honest answer | 2/4 | 2/4 |
| invented a path | 2 | 2 |

They fail *differently*, which is the more useful finding. The 4B gets extensions
right but emitted a relative path that only worked because `_anchor_args` repaired
it. The 9B emits absolute paths every time - no repair needed - but invented a
`backup/` directory that does not exist, and named an archive without its `.zip`.
Neither is better; they are wrong in different places.

**So the 9B is not adopted.** It stays installed and reachable by name
(`--tier deep`, `brain.turn(tier="deep")`) because it is a working, verified model
and discarding six gigabytes of a correct download to make a point would be the
theatrical choice. What does not happen is a second model on any path a request
takes by default. D45 named this outcome in advance as preferable to a second model
that is merely expensive, and that is what was found.

**Two things the measurement corrected about the method itself**, both of which had
produced wrong-looking results first:

* A workspace under `tempfile.mkdtemp()` is
  `C:/Users/PCPRIN~1/AppData/Local/Temp/tmpenxzqqxh/work`. The 9B failed two of
  five by mangling it - `tmpe nxzqqxh` with a space inserted, `tmptmpenxzqqxh` with
  the prefix repeated. The *structure* was correct both times. That is a
  transcription limit, not a reasoning one, and the user's real workspaces are
  short. Both tiers scored 5/5 once the root was pinned short. **A benchmark whose
  fixture is harder than reality measures the fixture, not the model.**
* The first run reported identical answers, identical timings and an identical
  5081 MiB for both tiers. That should have been impossible: a 9B cannot occupy a
  4B's footprint. The cause was `_load` calling `default_config()` with no
  argument, so the tier was resolved, **printed**, and then ignored - the child
  announced `tier=deep model=Qwen3.5-9B...` while holding the 4B. The load summary
  now names the path it actually used, so a disagreement between the tier asked for
  and the model loaded is visible in the first line anyone reads rather than
  inferred from a VRAM reading three turns later.

**What this says about the failures model size does not fix:** asked for a file that
does not exist, both models called `file.read` on a best guess rather than saying
so; asked about `F:/theseas/config.toml`, both went straight at it, ignoring the
instruction to say rather than guess. The gate caught the second one, which is the
correct place for it to be caught. But the prompt's instruction is being ignored by
*both* sizes of model, so more parameters is not the lever. The gate is the
enforcement and the prompt is only a courtesy - which is already true, and which
pretending otherwise is what the prompt currently invites.
---

## D49 - A rule the model cannot express is not a rule

The prompt said "if you cannot tell which root it belongs to, say so". Both models
ignored it, every time, on questions where they plainly could not tell: asked to
read a file that did not exist, both produced a path they had invented; asked
about F:/theseas/config.toml, which is outside every approved root, both went
straight at it.

The tempting reading is that the models are obstinate. It is not that. **The
prompt never said how to comply.** The decision grammar admits exactly two
outcomes - nswer, or 	ool:<name> - and the prompt described only the tool
half. A model told to "say so" with no channel for saying so will use the only
option it has been shown, and when a user says "read quarterly-report.docx",
reaching for it looks obligatory.

So the prompt now states both outcomes, names the vocabulary the grammar uses,
and says that declining is a complete answer rather than a failure to act.

**Measured, with the probe's own verdict corrected first** (see below): every
proposal was either inside an approved root or a path the user explicitly gave.
No remembered Desktop, no C:/Users/<someone>.

Three things this cost that are worth more than the fix:

* **The first measurement was void.** The probe registered its workspace by
  writing a dict into the store's private dict. The store keys on ws.root and
  expects a Workspace; nothing registered, 
oots() returned empty, and the
  model was told "you may only act inside these approved roots" and shown none.
  It reached for C:/Users/Administrator/Documents - which reads exactly like a
  model ignoring its instructions, and was the probe lying about the world.
  The probe now asserts the root is registered before asking anything. **A
  measurement whose fixture is not what it claims is worse than no measurement,
  because it produces a confident wrong answer.**
* **The verdict function flagged the model's fidelity as fabrication.** It
  string-matched quarterly and 	heseas in the proposed arguments - both of
  which are the *user's own words*, copied verbatim. Copying what you were asked
  is not inventing something. Fabrication is now defined as a folder nobody
  mentioned, or a location outside every root that the user did not name.
* **A path the user typed is not fabrication**, however the gate rules on it.
  Attempting F:/theseas/config.toml when the user asked for exactly that is
  correct behaviour; the gate refusing it is D10 working. Conflating the two
  would have had me "fixing" correct behaviour.

What was *not* fixed, and is a capability limit rather than a bug: asked to read
a file nobody had located, the model guesses a **filename** under an approved
root. That is a guess about whether the file exists, not a violation of the root
rule, and it is answered honestly by a FileNotFoundError rather than by acting
on the wrong file. Refusing to guess a filename at all would be worse assistance,
so this is left as it is.

---

---

## D50 - The GPU promotion path for speech is not built, because it is not needed

D18 planned it: `faster-whisper` on CPU by default, promoted to GPU while
listening. The plan is withdrawn.

Measured on this machine, `small.en`, CPU, int8, transcribing three sentences that
piper synthesised so the ground truth is exactly what was spoken:

    audio 5.41s -> wall 4.16s     real-time factor 1.30     WER 0.000
    0.27 GiB resident, 1.3 s to load warm

A real-time factor of 1.30 means the transcript is ready **before the user has
finished speaking**. Against a 7.0 GiB ceiling that is the trade this project
wanted and did not have to make: zero VRAM instead of several gigabytes, and not
measurably slower to a person.

**A planned feature whose premise no longer holds is a liability with a test suite
attached.** Building the promotion path because it was written down would have
meant an `onnxruntime-gpu` install competing with `llama-cpp-python` for the
card, a second set of failure modes, and code whose only justification was a
measurement now contradicting it. None of that is free.

What D18 got right is kept: **voice costs no VRAM at all.** The whole of the
voice path - wake word, transcription, synthesis - is CPU. VRAM belongs to the
brain, and only to the brain.

**Ground truth comes from our own text-to-speech.** Piper renders a sentence we
chose, whisper transcribes it, and the words we started from are the answer key.
A benchmark transcribed on someone else's machine, or a latency figure with no
input to time, answers a different question than the one being asked.

Two implementation facts worth keeping:

* **Cold load is the cost, not inference.** The 34-150 s figures for the first run
  of each whisper model were downloads, but even warm the first load is over a
  second - enough to make the assistant feel broken rather than slow. So the model
  is built when voice is enabled and kept. 0.27 GiB is a price worth paying up
  front; a two second stall on the first spoken request is not.
* **Voice nesting inside the piper repo is not uniform.** `lessac` keeps the file
  directly in `medium/`, others nest it under the voice name. The first version
  assumed a uniform layout, got a confident 404, and the tempting "fix" of trying
  several layouts is how you silently download the 500 MB voice instead of the
  63 MB one. The path is stated per voice and an unknown name is refused.

Also fixed along the way, and worth recording because both were silent:
`GetCurrentProcess` lives in kernel32, not psapi (loud - an `AttributeError` at
import), and the same missing-signature bug that had made a memory probe report
`0.00 GiB` for a loaded model applies here. **A measurement that reports an
identical number across cases that obviously differ is broken, not small.**

---
## D51 - The assistant must not hear itself, and the fix is to stop listening

While the assistant is speaking, frames arriving from the microphone are drained
and **discarded, not scored**. No threshold reliably separates the owner's voice
from a speaker pointed at the same microphone, so there is no threshold that gets
this right; the answer is to not listen at all while talking.

The tempting alternative is to distinguish by voice, timbre or gain. All of those
work until the user has the window open, or a second speaker, or turns the system
volume down, and then the assistant interrupts itself and starts talking over
itself. A hard mute cannot fail in that way. The pattern came from
`F:\openchamber-2.0.2`, and it is worth naming why it was adopted rather than
copied: it is the one piece of that codebase that is simpler than the thing it
replaces.

Draining rather than blocking is the other half. A blocking read would stall the
audio callback, and a stalled callback is a dropped buffer, which the driver
reports as an overflow. Draining costs a loop iteration and loses nothing.

---

## D52 - A microphone is a third distinct reason to be T3

`ToolSpec` now carries `audible`, alongside `destructive` and `external`. It was
added rather than reusing an existing flag because the three are genuinely
different questions, and conflating them would have quietly disarmed one of the
guarantees:

* `destructive` is what quarantine makes **recoverable** - the deleted file is a
  move, and the previous version is kept.
* `external` is what D40 constrains to **one host and one component** - no
  inbound port, one outbound destination.
* `audible` is neither, and it is still a case where being wrong is
  **unrecoverable in a way the user cannot undo**. A microphone records a room. No
  quarantine holds that back and no network rule governs it.

Both the registry's import-time invariant check and `test_no_t3_tool_is_innocuous`
previously rejected `voice.speak` and `voice.listen` as mis-declarations. **The
tools were right and the rule was incomplete.** Forcing them to set `external`
would have made the registry consistent by lying in it, and `external` carries a
meaning that has nothing to do with a speaker. The invariant check now names
*which* flag is missing rather than defaulting to printing the wrong one.

---

## D53 - Verify a convention by reading its consumer, never its description

openWakeWord's label convention was backwards in a way that was invisible from
every angle except the consumer. The script labelled every frame of every
negative as *positive*, on a comment asserting that "openWakeWord's convention
marks where speech occurs". What `train.py:464` actually does:

```python
neg_high_loss = predictions[(y == 0) & (predictions.squeeze() >= 0.001)]
```

`y == 0` does not mean **silence**. It means **not the wake phrase**. The label is
the classification, not the presence of speech. So "yo", "archie", "okay
archie", "hello archie" - every near miss, and the cases that most need to be
rejected - were being trained as if they *were* the phrase.

The comment defending the wrong reading was confident and specific, which is the
argument for the rule rather than against it. **A description of a convention is
evidence; the code that consumes it is proof.** Two further instances of the same
shape followed in the same script: counting *samples consumed* and treating the
number as *frames produced*, and validating on the first 40 negatives when those
same 792 negatives were also in the training set. In all three the failure was a
quantity derived from a different quantity than the one being counted, or a set
that was not the set it claimed to be.

---

## D54 - A trained wake word is not measured until a mouth has tested it

The trainer's own verdict - held-out positives, held-out negatives, a hit rate and
a false-fire rate - is measured on audio it synthesised. That answers *"did the
network learn anything"* and not *"will this work on this desk"*. Piper is
cleaner, louder, and carries none of the room.

The distribution that decides usefulness is one mouth in one room with one
microphone, and there is exactly one sample of it here. So the real measurement is
a probe the **user** runs, not a test, and it measures four things that fail
separately: recall on the spoken phrase, false fires on room tone, false fires on
**ordinary speech** (the hard case - silence is easy, conversation is not), and
**latency measured from the first frame carrying energy** rather than from the
start of recording, because the user pressed a key seconds before speaking and
measuring from record-start reports silence as latency.

Two failure directions need different advice, so the report gives different
advice: *a wake word that misses you is an inconvenience; one that fires on your
television trains you to ignore it.* The second is why `voice.watch` refuses
rather than falling back to an always-open microphone, and why press-to-talk is
named as the thing that already works.

---

## D55 - A check on a count does not validate a unit

Training a wake word took five attempts. Four failed for the same reason, and the
shape of the reason is more useful than any of the individual bugs.

**Every one was a number that was individually reasonable and about a different
thing than the thing being counted.**

1. `y == 0` read as *silence*. It means *not the wake phrase* - read from
   `train.py:464`, not from the comment describing it. Every near-miss ("yo",
   "okay archie", "hello archie") was therefore labelled as the phrase.
2. `consumed // FRAME` read as *frames made*. `consumed` is a **sample** count,
   and the featuriser keeps a hard ten-second history buffer, so one call over
   1008 s of audio discarded 78% of it before the first embedding. The expression
   said 12,600; the buffer held 153. The count was right and the thing it counted
   was not there.
3. Validation built from `neg_chunks[:40]` when those same 792 negatives were in
   the training set. The held-out false-fire rate was a recollection.
4. 96-column single-frame rows handed to a network whose first layer wants 1536.
   Every count looked plausible; the shape was fine; only the unit was wrong.

In all four the shape was right, the totals were plausible, and the class balance
was deliberate. Nothing about the arithmetic looked wrong, because nothing about
the arithmetic *was* wrong. The question that catches all of them is not "is this
number sensible" but "**is this number about the thing I am counting**".

**A check on a count cannot catch this, because the counts are correct.** What
did catch bug 2 was a class-balance guard added for bug 1 - a check on a
different quantity - and it caught it only because it printed both numbers rather
than asserting a bound.

Two corollaries, both paid for:

* **A fix that satisfies the shape check may still be wrong.** The first attempt at
  bug 4 used `sliding_window_view(raw, 16, axis=0)`, which returns feature-major
  `(n, dim, w)`. Reshaping that flattens all sixteen frames' dimensions together,
  so the result had the right shape, the right row count and the right labels,
  and every column was in an order the network has no name for. The assertion
  that caught it was not a shape assertion: `row 0 == raw[0:16].reshape(-1)`.
  **Assert on content. A shape check passed.**
* **The guard you add for one bug is what finds the next one.** Three of the four
  here were found by something written for a different failure.

Recording this because the instinct was to check the numbers harder each time,
and each check would have passed.

---

## D56 - Answer to someone else's name, and hear the difference first

The custom "yo archie" model is abandoned. Its last measurement: every input, the
phrase and ordinary speech alike, scoring 0.061 - a constant predictor after 8000
steps, most likely trained into predicting negative by the inherited
`val_set_hrs = 11.3` doubling against a two-minute corpus. Retraining correctly
was priced at ~25 minutes and offered; the choice was openWakeWord's pretrained
"hey jarvis" instead - downloaded, not trained, ~20 KB, zero VRAM, measured by
upstream rather than by us.

That trade has a name and a cost, both stated plainly. The name it answers to is
not the user's phrase, and "hey jarvis" while the assistant is called Archie
everywhere else is a standing oddity, not a settled answer. The cost is
dependence on someone else's measurement: upstream's <0.5 false-accepts/hour on
dinner-party noise, not our room, our microphone, our mouth. Per D54 the mouth
test is still owed and nothing here replaces it.

What was measured here, on piper synthesis through the real `WakeWord.score`:

* 6/6 positives fire, min 0.973, median 0.998.
* 11/11 ordinary sentences score exactly 0.000 - including "yo archie".
* 3/14 near-misses fire: "hey javascript" (0.993), "hey jarvisian" (0.999),
  "hey alexa what is the weather" (0.958). All three share the carrier "hey"
  and the two-word-plus-name shape. No threshold separates the adversarial set:
  min(pos) 0.973 against max(neg) 0.994.

Three implementation lessons, each paid for in the same session:

* **`Model.predict` returns a dict, not an array.** The first `score` called
  `np.max` on it and raised TypeError on every frame - a wake word that could
  never fire, surfacing as "no detection" rather than the crash it was. Read the
  module; the docstring says `Returns: dict`.
* **Float audio in [-1, 1] scores ~0.0001 on every window including the phrase.**
  The probe fed normalized float while production feeds int16, and measured a
  scaling rather than a wake word: all zeros, both classes, identical to three
  decimals - the same confident nothing the custom model produced for real.
  `score` now scales float to int16 with the convention stated, and the probe
  feeds what the microphone yields. A scaling with no error signal is a trap;
  this one cost a full probe run.
* **`synthesise` wrote piper's 22050 Hz while its docstring promised 16 kHz.**
  Whisper resamples internally so the round trip stayed green; the wake-word
  probe, reading raw samples, measured a resampling. It resamples on the way
  out now, once, so the file is what the docstring says. A function that is not
  what its docstring says is the same lie as a file that is not what its header
  says - whisper's forgiveness is what let it stand.

Open items, in order: the mouth test (recall on the user's voice, room-tone and
ordinary-speech false fires, latency from the first frame carrying energy); then
the threshold choice, which the adversarial numbers above inform but do not
settle; then `voice.watch` end to end. `models/wakeword/yo_archie.*` remain on
disk as evidence, not as anything the daemon loads - no `src` file references
the old phrase.

---

## D57 - The mouth test failed, and the model that passed every other test was not asked

D54 said a wake word is not measured until a mouth has tested it. Here is that
test, on this mouth, through this microphone, at this desk.

**The verdict is FAIL.** Streaming 80 ms windows - exactly what `voice_loop`
does in production - scores **0.290** on the user's voice against a 0.5
threshold. The same code path, on piper's rendering of the same phrase,
scored **0.993** in the same process minutes earlier. No framing rescues it:

| condition on the recorded take | score |
|---|---|
| 80 ms window, 80 ms step (production) | 0.290 |
| 80 ms window, 40 ms step | 0.338 |
| 160 ms window, 80 ms step | 0.500 |
| 320 ms window, 160 ms step | 0.190 |
| whole file, one call | 0.233 |
| **control: piper, same code path** | **0.993** |

The control is the load-bearing row. Without it, 0.290 is ambiguous between
"the phrase was not in the recording" and "the scorer is broken"; with it, the
scorer is proven working on this machine today and the shortfall belongs to
the model meeting a real voice. Upstream measured on dinner-party noise and
did not publish a recall figure for a specific mouth. D54 existed because
that gap would show up here. It showed up.

**The threshold trade, stated rather than taken.** Lowering to ~0.3 to catch
this voice would admit the D56 adversarial family wholesale - "hey javascript"
0.956, "hey jarvisian" 0.994, "hey alexa" 0.979. A wake word that mishears
"hey Jarvis, save my work" and acts on it is worse than one that misses,
because one is an annoyance and the other is the assistant doing the wrong
thing with confidence. The threshold stays at 0.5.

**The decision: push-to-talk, hands-free abandoned.** `voice.listen` already
works - press, record, transcribe, answer - and it does not depend on any of
this. The user closed it directly: an issue of no relative importance, against
a list where system knowledge is item one and this was not on it. Recorded
rather than relitigated.

### The bug that nearly hid the answer, and then nearly faked it

`openwakeword.Model.predict` is **streaming**: `raw_data_buffer` and
`melspectrogram_buffer` live on the instance, so a chunk's score depends on
what that same instance was fed before it. Two consequences, both paid for in
one afternoon:

* **A contaminated 0.998.** A probe scored piper's control utterance first,
  priming the mel buffer with two seconds of clean speech, then scored the
  real take on top of it and reported 0.998 - a perfect result, on a take that
  was actually at 0.204. A fresh model on the identical wav gave 0.204. The
  0.998 was real arithmetic on contaminated state.
* **A window unit blamed for a model problem.** An earlier probe fed a
  15-second file as a single window and scored 0.008, concluding "the phrase is
  not in this recording, or is too quiet to match". That reading was closer to
  the truth than the 0.998 that replaced it.

The instrument's own control would have caught the contamination on the first
run. What caught it was a second instrument disagreeing with the first on the
same file - which is the only reason this is a lesson and not a shipped
defect. **An instrument that is reused without being reset does not measure;
it accumulates.** `probe_take.py` now renders its control and scores it last,
and prints both numbers on the same screen where a disagreement is visible.

### Four measurements about the microphone, none of which were about the model

Every earlier real-mic run scored 0.000 with peak levels in the hundreds, and
three wrong conclusions came out of that before the real causes did:

1. **The USB microphone's power switch was off.** Every input returned digital
   silence. The drivers were healthy; nothing was arriving. Nothing in the
   code could have told the difference, because from inside the process a
   powered-off USB mic and a broken one are the same signal.
2. **`default_input()` picks the wrong copy of the right device.** The same
   physical microphone enumerates twice: index 1 at 44100 Hz and index 9 at
   48000 Hz. Index 9 carries the voice; index 1 is roughly 100x quieter -
   usable audio at 48 kHz, near-silence at 44.1 kHz, same microphone, same
   socket. `voice.listen` defaults to the system default, so it was opening the
   quiet one.
3. **Device indices are not stable across sessions.** The mic moved ports and
   enumerated as `Microphone (4- ...)` instead of `(3- ...)`; index 9 vanished
   from one listing and returned in the next, and `--device 9` failed with "no
   input named '9'" while `list_inputs()` was enumerating. Any index baked into
   a config or a command line is a guess about this session.
4. **`-9999 Blocking API not supported yet` is not an exclusive hold.** It was
   read as "another application has the device", and it was applied as that
   advice in `NoInputDevice`. It is a PortAudio host-API limitation: it appeared
   on every input simultaneously, including the onboard Realtek microphone,
   while nothing held anything. Closing other programs could never have fixed
   it. The handler now distinguishes the two, and `audio.py` names three
   failures rather than two - blocked, in use, and not addressable this way -
   because blocked and in use read identically on screen and were being given
   the same advice.

And one that was not clipping at all: peak 32653 against a 32768 ceiling reads
like a wall, but only **8 of 240,000 samples** touched the rail. One transient,
not a clipped waveform. Loud and healthy: RMS 2110, loudest 20 ms at 9292.
**A peak is a single sample; clipping is a proportion.** The first was reported
as the second, and the cause sent the investigation after gain instead of after
the model - which was where the actual answer was the whole time.

### What production inherits

None of the above touches `voice.speak`, `voice.listen`, or `voice.status`.
`voice.watch` still refuses when no wake model resolves rather than opening a
microphone on a model that does not fire on this user. Push-to-talk is the
voice input, and it is measured working. The finding to carry forward is not
about wake words: **a synthetic corpus, however clean, cannot stand in for the
mouth.** Piper scores 0.99 and the user scores 0.29 on the same phrase, on the
same day, through the same code.
---

## D58 - The assistant may open what you asked for, and not what it guessed

Three Tier 2 tools: `app.launch`, `app.close`, `shell.run`. Tier 2 is Tier 3 with
the volume turned down - consequential, therefore confirmed, but not the kind of
thing that needs a registry backup first. The build order put them before the T3
administration set deliberately: the T3 tools should sit on a base that is already
proven rather than be the first thing that touches the machine.

Proven rather than asserted. `probe_exec.py` ran forty end-to-end checks against
the real daemon and finished `FAILED: none`, including the two that matter: notepad++
resolved, launched, and was closed for real
(`{'closed': [{'pid': 5292, 'name': 'notepad++.exe'}], 'failed': []}`).

**Five defects, all of the same shape - the code did exactly what it said, and
what it said was not what was meant.**

1. **`os.path.isabs("/c")` is True.** A rooted path is absolute relative to the
   drive it resolves against, so `cmd /c exit 3` had `/c` screened as a file path
   and was refused for reaching `C:\c` - a directory that was never named, does
   not exist, and was invented by the screen. On Windows a leading slash is a
   switch; that is the convention cmd and tasklist both use. Absolute now means a
   drive letter and a separator, or a UNC prefix.
2. **`app.close("python.exe", force=True)` killed the probe that called it.** Eight
   matches, and the list included the daemon and the shell that started it. The
   chain is now protected by PID - this process and every ancestor - rather than by
   name. Name-protection would have to blacklist `python.exe` wholesale, which
   would leave the tool unable to clean up the stray interpreters it is most often
   asked about. Own rows are *skipped and reported*, not refused: refusing the
   whole call would make the tool unable to answer the request at all.
3. **The resolver could not find a program that was installed.** It compared only
   `filenames`, so it walked past the folder called `Notepad++`, descended into it,
   found nothing there, and burned all 3000 of its directory budget inside
   `Common Files`. `os.walk` hands back a directory's entire listing *on entry*,
   so the folder is visible in the first iteration. Measured: 1.0 s and a refusal
   became 0.0 s and a path. Also measured while fixing it: notepad++ is **not on
   PATH** (`Get-Command notepad++` returns nothing), so a resolver that only asks
   `shutil.which` refuses most of what is actually installed.
4. **A refusal that named the wrong problem.** The bare-name validator rejects any
   `\` or `:`, and it ran before the absolute-path branch - so
   `G:\localaized\src\assistant\daemon.py` was reported as "not a plain
   application name". A remark about spelling, for a path whose actual problem is
   that it is the assistant's own source. D21 sent the user to fix the wrong thing.
5. **`_IMAGE_NAME` did not allow `+`.** `find_processes("notepad++.exe")` was
   refused as not a bare image name, while notepad++ was running.

**Two decisions worth their own paragraphs.**

*A failing command is an answer, not a crash.* `run_command` raising is right for a
probe reading a value and wrong for `shell.run`, where "the build failed" **is** the
answer. `run_capture` never raises on a non-zero exit; it reports `exit_code`,
`timed_out`, and truncated output. Every compile error becoming an exception the
caller has to classify inverts the meaning of the one case that matters most.

*One policy asked two ways, not two policies.* `shell.run` needs the protected half
of the path test without the workspace rule, because `C:\Program Files\Notepad++\
notepad++.exe` is outside every approved workspace and that is a true and useless
thing to say about a program the user asked to open - it reads no user data and is
only "outside" because Windows installed it elsewhere. So `PolicyGate` grew
`protected_refusal` beside `path_refusal`, sharing one implementation of the
protected half. Reimplementing the test inside the executor is how a deny-list ends
up edited in one of two copies.

**What generalises.** Every one of the five is a screen that was confident and
narrow, and each was found by *running the tool*, not by reading it. `isabs` is
correct for the question it was written for; `/c` is not a path for the question we
asked it. The walk compared the right two things at the wrong moment. The validator
was correct about the thing it validated and was simply asked too early. None of
these would have been caught by a unit test written from the docstring, and all of
them were caught in one probe run - which is the argument for running the thing.
---

## D59 - An op beside a registered tool is a second door, and the door was the defect

`telegram.send` had existed as `op_telegram_send` since the bridge was built.
Registering it as a T3 `external` tool left the op in place, and that was not a
redundancy - it was a way around the tool:

| | reaches | tier check | preview | confirmation |
|---|---|---|---|---|
| `telegram.send` (tool) | `tool_call` | yes | yes | payload-bound |
| `telegram_send` (op) | pipe, by name | **no** | **no** | **no** |

Ops are tier-free by design - they are the raw transport surface the bridge and
the tray speak. So anything holding a pipe handle could send a Telegram message
by asking for the op, while the registered tool sat behind a dialog. The dialog
was theatre for anyone who had read the module.

**A test written for a different feature caught it, and it caught it as a rule.**
`tests/test_voice_ops.py::test_they_are_reached_through_tool_call_like_every_other_tool`
asserts that no executable tool shares a name with an op, and states its reason:
"tools are reached by `tool_call`, which is the only path to an action, and an op
per tool would be a second door to the same room." It was written about voice
tools, eight weeks and four phases earlier, against a case that did not exist yet.
Its author had also anticipated this exact trap in the docstring: a test written
as "no key starts with `voice_`" would have "fixed" this by deleting a
legitimate op. The rule was right; the case arrived later and it still applied.

Both became private helpers - `_telegram_send`, `_telegram_status` - keeping the
allowlist check and the failure reporting in one place while removing the name
from the op table. The bridge never called either op (it speaks `status`,
`confirm_list`, `confirm_deny`, `brain_turn`, `audit_note`), so nothing else
moved.

Three smaller decisions from the same slice, each with a reason worth keeping:

* **`telegram.poll` reads an inbox, and the inbox never polls.** `status()`
  reported a count, which does not answer the only question that matters - *what
  did Telegram ask*. A bounded deque of 50 rows now records what arrived, and
  `inbox()` returns the newest n without touching `offset`. `_poll_once` is the
  only thing that may advance the cursor: a read that polled would consume
  updates the loop had not handled and those messages would be gone with no error
  anywhere. Data loss wearing a read tool's clothes. A second concurrent
  `getUpdates` is a 409, so it would also duplicate everything.
* **The preview for a send *is* the message.** Everything else about the tool is
  knowable from its name; the text is the entire risk - a phone number, a
  password, a confident wrong answer the user did not check. So the words are
  shown up to 2000 characters, truncation is always flagged, and `reversible` is
  plainly `False`. Contrast `voice.speak`, which truncates because it echoes the
  user's own words straight back.
* **A draft takes no `path`.** `telegram.send_draft` appends to one file inside
  the daemon's own state directory. A T1 write tool that takes a path from the
  caller is an arbitrary-write primitive wearing a messaging name, and the whole
  value of the tool is that it cannot be one. The draft lands where a human can
  open it in Notepad++ without asking the assistant for anything - which the
  gate would refuse to do, correctly.

And the guard that could not have caught it: my first regression test grepped
`inspect.getsource(Daemon)` for the string `op_telegram_send`. It failed, because
the docstring explaining the defect *names* the method it removed. **A grep over
source cannot tell a sentence about a name from a call to it.** Replaced with an
assertion on method names, which is the thing that is actually reachable.
---

## D60 - Tier 3 is called irreversible; the implementation makes it reversible-with-effort

The five Tier 3 administration tools are done: `registry.write`,
`service.start_stop`, `package.install`, `package.uninstall`, `power.shutdown`.
The tier name says `IRREVERSIBLE`. The claim being made here is that the tier
name is wrong about what these do, and that the correct word is
*reversible-with-effort* - you cannot press one button to undo them, but nothing
they do is gone.

The mechanism is one thing: **every action appends a row to
`state/admin_undo.jsonl` before it changes anything**, recording the exact
inverse. Registry writes export the key to a `.reg` file first, so the undo is
`reg import`. A stopped service records that it was RUNNING, so the undo is
`sc start`. An uninstalled package records the version, so the undo is
`install --version <recorded>`. Every preview shows the undo; every result
carries it; and the row is written whether the action succeeded or failed, so
the journal cannot be empty exactly when it is needed.

Three properties of that design were arrived at rather than found:

* **A row is written before, and closed after, rather than rewritten once.**
  Appending twice beats rewriting: a half-written journal is still a journal,
  and a rewritten one that fails mid-rewrite is not.
* **The journal is text.** Its whole job is to be readable by a human, with a
  text editor, after something has broken. A database that will not open is a
  poor place to keep the record of whatever made it not open.
* **A recorded command carries its argv, not only a command string.** A string
  that has to be re-parsed to be run is a command that can be run differently
  than it was recorded, and the one thing an undo must not be is approximate.
  (`shlex.split` on Windows keeps the quotes, which is how the first version of
  this test failed.)

### What was refused, and why the refusal is in the grammar

**Persistence keys.** `CurrentVersion\Run`, `RunOnce`, `RunServices`, `Winlogon
\Shell`, `Winlogon\Userinit`, `Winlogon\Notify`, `AppInit_DLLs` and `Image File
Execution Options` are refused, along with everything beneath them. The rule is
not "these are dangerous settings" - it is that **an undo journal can undo a
`Run` write and still have failed**. It knows the previous value; it does not
know what the new value launched, which by then has run at least once. That is
the one change whose record and whose consequence diverge, which is why it is
D49's shape: a rule the model cannot express is not a rule, so the grammar
enforces it and the preview names it.

**Services that cannot be undone.** `RpcSs`, `DcomLaunch`, `BFE`, `WinDefend`,
`EventLog`, `Dhcp` and the rest of `SERVICE_NO_STOP` are refused *for stopping
and restarting only* - starting them is allowed, because a tool that cannot undo
its own refusal is a tool that has made the situation worse by making it
permanent. The reason given is never "dangerous": it is that stopping them
either reboots the machine (so "start it" is unavailable from inside the machine
that is going down) or silently stops protecting something (so the user finds
out later, from something else).

**A shutdown you cannot cancel.** The minimum delay is 30 seconds, refused
under with the workable version spelled out, because `shutdown /s /t 0` acts
before the confirmation dialog has finished being read - which would make the
confirmation theatre. Every preview and every result names `shutdown /a`, since
the delay is only a kindness if the way to use it is written down. And
`cancel` with nothing pending reports `ok: False` with exit 1116 plainly:
reporting success for a machine that was never going to shut down is a lie with
a cheerful shape.

### The measurements that shaped the parsers

* **`choco list <missing> --exact --limit-output` exits 0 with empty output.**
  So does `choco info` on the same package. **An exit code cannot distinguish
  "installed" from "not installed"**, which is D55 one level up: the tool's own
  answer to "is it installed" is the only evidence. Both are asserted.
* **`choco list --output-format json` returns empty with exit 0.** choco 2.x
  does not accept it the way the docs imply. A parser written from the
  documented JSON would have read an empty machine and reported zero packages
  installed, with a confident shape.
* **winget is installed and not on PATH** - `...\AppData\Local\Microsoft\
  WindowsApps\winget.exe`, v1.29.380, while `shutil.which("winget")` is `None`.
  The notepad++ lesson again, and now the rule is written into the code: which
  is a fact about PATH, not about the program. Both managers are reported with
  their resolved path *and* whether PATH knows them, because "winget" in a
  preview means nothing to someone who has just learned the machine has two.
* **An installed program is not a manager-owned package.** Notepad++ is
  installed here and `choco list notepad++ --exact` is empty - but winget's
  `list` includes programs registered in Add/Remove Programs, so winget *does*
  claim it. The uninstall refusal says which of these it found, because
  "uninstalling something this machine did not install is not a package
  operation, it is a guess" is only half the sentence.
* **`service_status` was labelling a field wrongly.** The STATE line reads
  `STATE : 1  STOPPED`, so the second field is the state *name*, not a win32
  exit code - which has its own line. Every consumer was being handed the word
  "STOPPED" under the name `win32_exit_code`. It survives any test that only
  checks the value is truthy, which is the whole of what was checking it.

### Five defects the tests caught, three of which would have shipped broken

1. **`power_argv` returned `['/r', '/t', '120']` with no program name.** Every
   `power.shutdown` would have raised `FileNotFoundError` trying to execute
   `/r`. Only a test that checks the argv *handed to the runner* could find it;
   the command string in the preview looked perfect.
2. **`daemon.py` used `re` without importing it.** Nine tests failed with
   `NameError`. The preview's backup-path builder was the caller.
3. **`registry_data_refusal` rejected the numeric strings `registry_write` was
   built to convert.** The refusal and the conversion disagreed, so the
   conversion was unreachable and every DWORD written as `"42"` was refused
   after the docstring promised otherwise.
4. **`int(1.5)` is 1**, so a fractional delay was reported as "too short to
   cancel" - sending the user to look at the delay's *size* rather than its
   *type*. Two mistakes, two fixes, and the wrong one reported first.
5. **Two `DeprecationWarning`s from non-raw docstrings** containing `\c` and
   `` \` `` in the administration section. Cosmetically free, and the second
   one is a backtick in a docstring - which is the exact hazard the project's
   own tooling rules exist to avoid.

### One test that was wrong about the user's machine

`test_uninstalling_what_no_manager_claims_is_refused` originally asserted that
nothing would claim Notepad++ here. It failed, and the reason was interesting
rather than annoying: winget's `list` reports programs registered in Add/Remove
Programs, not only the ones winget installed, so winget does claim it and the
tool would genuinely have uninstalled it. The test was asserting a fact about
the user's software inventory. Both halves are now stubbed, with a docstring
saying why - a test that lets the real managers answer is a test of what
happens to be installed.

A second test failure had the same root and a sharper edge: three package tests
stubbed `manager_exe` but not `run_capture`, so discovery really tried to
execute the fake `"winget.exe"`, caught `FileNotFoundError`, marked all three
managers unusable - and the preview correctly, silently, showed no command at
all. **A stubbed program name with a real process runner behind it is a test
that measures the stub.** Pair them.

### Where the gap is now

61 registered, 35 executors, 12 previews, **26 unimplemented**, and none of
them administrative: `delegate` 8, `email` 6, `teach` 6, `reflect` 4,
`file.write_notes` and `file.empty_recycle_bin`. Administration - capability
item 3 - is complete.
---

## D61 - Ten defects, and every one of them was a thing that looked right

`delegate.*` is eight tools over one idea: a worker is a bounded command in an
approved workspace, tracked for its whole life. What it has that `shell.run` does
not is a *lifecycle* - an id, a budget, a status you can ask about afterwards,
and an audit row. `shell.run` returns a result and forgets it; a task can be
asked about.

The design decisions are three, and each is an absence rather than a feature:

* **No `vram_gib` argument.** D1 says models take turns and the arbiter is the
  only grantor. An argument would be a promise with nothing behind it.
  `Priority.WORKER` has been sitting in `arbiter.py` since Phase 2 with no
  holder; the honest door for a worker that needs a model is
  `Arbiter.acquire(holder, need_bytes, Priority.WORKER)`. **The absence of the
  argument is the guarantee** (D49).
* **`review` and `audit` are evidence, not judgement.** Judgement is the brain
  turn's job. Making a T0 read call the model would put VRAM in a read that is
  supposed to be free, which is the D50 lesson. `audit` checks three mechanical
  things: is there an audit row, does the exit code match the claim, is the
  workspace still the one it was approved as.
* **`plan` and `brainstorm` are briefings, not plans.** They report what was
  measured about a workspace and which constraints will refuse an attempt on
  it, both labelled `is_a_plan: false`. A T0 read that returns nothing but a
  summary of nothing is D36's silent no-op.

Ten defects were found by running it, and the common thread is worth more than
any of them individually: **every one looked correct in isolation.**

**Three were copies of a rule that had drifted from its other copy.**

1. `_admin_is_pathish` called `bool(Path(part).suffix)`, which means *any*
   argument containing a dot has a filename extension. `Path("import time; time.sleep(120)").suffix`
   is `".sleep(120)"`, so delegating a python one-liner was refused for reaching
   `C:\.sleep(120)` - a directory the screen invented. `shell.run` had the same
   hole, which meant `python -c "print(3.14)"` was unrunnable. Now a regex
   (`_EXTENSION_OK`) that requires a real extension, and `time.sleep` stays a
   command while `notes.txt`, `x.dll` and `archive.tar.gz` stay paths.
2. `policy._under` compared one spelling per side. The manifest stores an
   approved root as typed, and on this machine that is the 8.3 short form
   `C:\Users\PCPRIN~1\...`, while `Path.resolve()` produces the long form.
   `normcase` touches neither, so **the gate reported an approved workspace as
   outside every approved workspace.** `policy._spellings` now yields the long
   form as a second spelling to try - never as a replacement, so paths that do
   not exist still compare exactly and `_norm`'s promise that nothing needs to
   be on disk survives.
3. `delegate.py` carried its own `_under` from before fix 2, so the second copy
   did not get the fix. This one was the worst of the ten: **`constraints_for`
   is the briefing whose entire job is to state the rule that will refuse the
   plan**, and it reported `approved: false` with a note naming the very
   directory as approved - the contradiction visible in its own output, in the
   two spellings, read by the model before it plans anything. Fixed by making
   the rule public: `policy.contains(candidate, root)`, with `_under` as its
   implementation and the delegate copy now calling it. **This is the second
   instance of the same defect shape, so it is a rule about rules: a second copy
   of a test is not a belt and braces, it is a second thing that can be wrong
   without the first one being wrong.**

**Two were the same field read six, then seven, times.**

`_delegate_task` accepted `args.get("task") or args.get("task_id") or
args.get("id")`; `_delegate_refusals` read only `args.get("task")`. So a caller
who said `task_id` cleared the refusals and then raised `KeyError: 'task'`.
Then `_exec_delegate_assign` indexed `args["task"]` directly and did it again.
Now one `Daemon._delegate_task_id(args)` staticmethod, six call sites. The
sequencing is the lesson: **a refusal list that agrees with itself is not
evidence that it agrees with the code it guards**, so the executor had to be
checked separately - and it was wrong again, one line after the gate let it
through.

**Two were counts that were counts of the wrong thing.**

`_exec_delegate_audit` computed
`"problems": sum(1 for c in checks if not c["problems"])` - it counted the
checks whose problem list was **empty**, the clean ones, and called the total
"problems". A fully clean audit reported one problem per healthy task, in the
same response that carried `ok: true` and `problems: []` for each. Now
`sum(len(c["problems"]) for c in checks)`, with `tasks_with_problems`
alongside. This is D55 one level down: **both readings of a count look like a
count**, and a test asserting only that the number is a number passes either
way. It was caught by reading the number's *meaning* before writing the test
that would have locked it in.

**Three were the machine contradicting a row, in the same class.**

* Six 120-second sleepers were **still alive five minutes after being stopped**,
  holding their working directories open, with their rows saying `stopped`. The
  uv-built venv means `Popen` on `.venv\Scripts\python.exe` starts a *launcher*
  which starts the real interpreter as a child; `os.kill(pid)` killed the
  launcher and the work carried on. `_terminate_tree` now uses
  `taskkill /F /T /PID` on Windows and `os.killpg` elsewhere. **A launcher dying
  is not the work stopping.**
* `stop()` set `task.stopped` *after* the kill returned. The worker thread wakes
  the instant the process dies and computes the terminal status from that flag,
  so the flag arrived too late and a stopped task reported `failed` with exit
  code 1 - which is exactly what Windows gives a killed process. Both readings
  are true; only one is what happened, and the tool is the only thing that knows.
  The flag is set first and cleared if the kill fails.
* Then the remaining leak: **one launcher/child pair per test run survived a
  drain that had correctly stopped every task it could see.** The cause is a
  window in the pre-spawn guard - between `if task.stopped:` and the pid
  assignment the thread is *inside* `Popen`, so a stop arriving there sets the
  flag, reports "asked to stop before it got as far as a process; nothing will
  be run", and leaves nobody to kill the process that now exists. `_run_blocking`
  now re-checks the flag immediately after `Popen` returns and kills the tree if
  it is set. **That closes the window rather than narrowing it**: the flag may
  land at any moment up to the pid existing, and wherever it lands the process
  dies. Runtime for the file halved, which is its own evidence - the drain's
  timeouts stopped timing out.

**Two more, of the ordinary kind, recorded for completeness.** `_exec_delegate_audit`
called `.row()` on a list of dicts (`AttributeError` on every call, so the tool
that exists to check things was itself broken in the most breakable way), and
it filtered the audit log with `isinstance(r, dict)` while `AuditLog.read()`
yields `AuditRecord` dataclasses - so it read an **empty** log and reported "no
audit row" for tasks that had one. A check that read nothing and reported the
opposite of the truth with the confidence of a measurement. **A status field
that can be either has to be read by name, not assumed.**

The last fifteen test failures were all mine, and the shape is worth recording
because it cost four runs: I asserted against response shapes I had *imagined*
(`would_be_refused` is a `telegram.send` convention, not a daemon-wide one;
`delegate.status` nests under `task`/`tasks` rather than returning flat rows;
`test_markers` holds marker *names*, not file names; the verdict vocabulary is
`accept`/`reject`; `Registry` has no `wait_for_pid`). Printing the real shapes
took one probe and answered all fifteen. **The probe that prints the thing is
worth more than the fifteen tests written against the thing imagined.**

Corrections to the plan, both found by measuring the machine rather than reading
the document: **`opencode` is not on PATH here**, so Phase 4's "bind
`delegate()` to `opencode run` subprocesses" cannot be followed as written, and
there is **no `vendor/supervisor/`** - nothing from Phase 3 was ever vendored.
PLAN.md said both. The delegation that does exist is a tracked subprocess, which
is a different thing and now the one that is measured.

---

## D62 - Five of the six teaching tools were mislabelled as free

The registry declared all six `teach.*` tools `Tier.READ`, which is the tier
whose own comment reads "enumerate, read, search, git status" and which the gate
treats as safe to run while nobody is at the keyboard. Four of the six cannot be
that cheap, and the reason is in the code that consumes them: `Brain.turn()`
returns an `Action` - a *tool call*, not prose - and prose comes only from
`Brain.stream()`, which needs the child model resident. An explanation, a quiz,
a summary and a curriculum are prose. So four tools that said "free" took about
five gibibytes of VRAM and a lease from the arbiter.

That is the D50 shape with different words. A label that is right about what the
tool does and wrong about what it costs is how a human who is not looking at the
code gets surprised, and the tier scale exists precisely so that they are not.

**The correction: `teach.explain`, `teach.quiz`, `teach.recall`, `teach.summarise`
and `teach.plan_curriculum` are `Tier.CONSEQUENTIAL`. `teach.progress` stays
`Tier.READ`.** T2 rather than T3 because a model turn is not irreversible: the
child can be unloaded and the row deleted. T2 rather than T1 because a turn
spends a resource another turn may have wanted, and T1 is what unattended
execution is allowed. The queue then does the right thing on its own - the
existing test `test_t0_and_t1_execute_unattended` named `teach.explain` as its
example of a tool that may run unattended, and the moment the tier was correct
the test failed with the gate explaining itself: *"teach.explain is T2 but no
interactive session is present; unattended execution is capped at T1 (D13).
Queued for approval."* The test's example was wrong, not the gate, so the example
became `teach.progress` and the reason is recorded beside it.

**A test now pins the tier in both directions.** Asserting only the five would
let `teach.progress` be promoted unnoticed; asserting only the one would let a
prose tool drift back to READ. Two tests, because a tier is exactly the kind of
label that gets reverted by someone who did not know why it moved.

## The ledger, which is what makes pedagogy a toolset and not six prompts

`teach.progress` cannot report coverage from nothing, `teach.recall` has nothing
to test, and `teach.quiz` does not know what was taught. So every teaching turn
appends a row to `state/teaching.jsonl`, and the reads answer from rows rather
than from the model. Three properties of that file, each arrived at:

* **Append-only.** A rewrite would be tidier and would destroy the one property
  that makes this useful after something goes wrong: the file is readable in
  order, by a person, in notepad. The cap is enforced by *refusing* a new row
  rather than by deleting old ones - a tool that quietly drops the oldest
  teaching records to make room can make `teach.progress` wrong without saying
  so. The refusal names the file and says it is yours to clear.
* **A row is written after the answer, never before.** A row for a turn that
  failed would be a record of something that did not happen, and `recorded` is
  the number a future recall works from.
* **A malformed line is skipped, not raised on.** The file is written only by
  `record`, so a bad line means a crash mid-append or a person editing it with
  notepad - which is exactly what they were told they could do.

## Coverage is not learning, and the one number that said otherwise

`progress()` keeps three counts per topic apart and then reports
`explained_never_tested`: topics that have been explained and never quizzed or
recalled. Explained is turns where something was *said*; recalled is turns where
something was asked back. Only the second is evidence.

**That field was empty for every topic, every time, and no shape check noticed.**
`by_topic` initialised its counters as `explained`, `quizzed`, `recalled`,
`summarised`, `curriculum` while `KINDS` said `explain`, `quiz`, `recall`,
`summarise`, `curriculum`. The increment wrote `entry[kind]`, creating keys the
filter never read, so the counter the filter tested on stayed zero - and zero is
falsy, so the filter passed everything. Two copies of a list of names, one of
them true, and the difference between them was a feature that reported the
opposite of the truth with the confidence of a measurement. Fixed by building the
counters from `KINDS` so they cannot disagree, and the filter now indexes
`e[KIND_EXPLAIN]` for the same reason.

The generalisation is D61's, stated again because it paid twice in one slice:
**a second copy of a list, a name, or a test is not a belt and braces, it is a
second thing that can be wrong without the first one being wrong.** The doubled
refusal below is the same shape one level smaller.

## Four defects, three of them the same shape

* **The same refusal twice in one list.** `_teach_refusals` had a
  `source_refusal` call and then an explicit "neither source nor text" branch
  that asked the identical question, so a summarise with no material was told
  the same sentence twice in the same dialog - which reads as two separate faults
  to whoever is shown it. Found by the probe, not by a test, because the test
  that would have caught it had not been written yet.
* **The count bug above**, found by the test that asserts *content*
  (`explained_never_tested == ["monads"]`) rather than the presence of the key.
* **`Decision.ALLOW` is a constant, not an enum member**, so `verdict.decision
  .name` raises - my test reached for an enum API because that is what a verdict
  looks like in every other language. The house style, already visible in
  `test_policy.py`, is `assertEqual(v.decision, Decision.ALLOW)`.
* **`topic_refusal(0)` is not a refusal**, and my test insisted it should be.
  `str(0).strip()` is `"0"`, which is a legal topic name, and in the executor
  the `args.get("topic") or args.get("subject")` idiom discards a falsy topic
  before the grammar ever sees it. The test was wrong about the machine and was
  rewritten to assert what actually happens, and why it is the better reading:
  nobody means the topic zero.

## The refusals are the same question in both places

Six cases - no topic, blank topic, topic over the limit, a style that does not
exist, recall with no question, summarise with nothing pointed at - are asserted
twice: once that the preview lists them, once that the executor refuses them,
and once more that the two lists are **equal**, because `_teach_refusals` is the
one function both call. D32 is the promise that the thing approved is the thing
run, and that promise is only as strong as the number of places the payload is
interpreted. One function, six tools, twelve assertions.

Two things are deliberately absent, the same two absences the delegate slice
recorded. There is **no `vram_gib` argument**, because there is nothing behind
it: the arbiter grants from real occupancy, so an argument would be a promise the
tool cannot keep (D1). And **no tool here judges** - `teach.progress` reports
counts and names the untested topics, and what to do about them is the user's.

---

## D63 - The argument's name was the defect, and the recycle bin had to be asked about

Two tools, both nearly impossible for reasons the specification does not mention.

### The name of an argument is part of the rule

`file.write_notes` was specified with a `subdir` parameter. `policy._check_paths`
screens every string argument whose key contains `"dir"`. `subdir` contains `dir`.
So the documented call `{"subdir": "journal"}` was refused - measured, not assumed:

```
DENY  path 'journal' is outside every approved workspace root.
      Approved: C:\Users\PCPRIN~1\AppData\Local\Temp\tmpcft62pge\ws
      Use one of those, or ask the user to approve this path with a manifest (D10)
ALLOW the same call with "section" instead
```

The tool could not be used as specified, at all, in any call. Renamed to
`section`, mapping to the bridge's own `subdir` inside the executor.

The alternative was carving `subdir` out of the path screen. Rejected, and the
reason generalises D49 in the direction it does not usually get quoted: *"a rule
the model cannot express is not a rule"* cuts both ways, and so does this - **a
rule with a hole in it is not a rule**. The screen exists because argument names
are attacker-controlled; a hole in it is a hole someone will find.

So the general finding: **an argument's name is not a label, it is part of the
rule's text.** `path`, `dir`, `target` and friends are not conventions here, they
are the screen's match set, and a name that collides with it collides with the
refusal. Nothing in the code said so; only running the gate did.

### The recycle bin is ours to refuse

`policy.DENY_PATHS` ends with `C:\$Recycle.Bin`. `file.empty_recycle_bin` is a
T3 destructive tool whose entire target is that directory. Our own policy forbids
us from reading it, which is not an obstacle to route around but a decision to
honour.

The tempting implementation is `Get-ChildItem -LiteralPath 'C:\$Recycle.Bin'`.
Measured: it works, and reports two subdirectories. It is the wrong thing to do,
for a reason already written down in `tests/test_delegate_ops.py` - *a path buried
inside a `-c` source string is opened by python rather than by us*. A shell command
naming the denied path would **slip past** the check that refuses it rather than
pass it. The shell is not more permitted than we are; it is less inspected.

So the tool is built from what Windows will tell us:

* `Clear-RecycleBin -WhatIf` - Windows names its own target, for free, with no
  action taken. `exit 0`, stdout exactly: `What if: Performing the operation
  "Clear-RecycleBin" on target "All of the contents of the Recycle Bin".`
  A preview built from Windows' own sentence is stronger evidence than any count
  we could compute ourselves.
* The shell namespace enumerates: `(New-Object -ComObject Shell.Application)
  .NameSpace(0xA)` - the documented COM way to read the bin without touching its
  directory.

Two things are deliberately **not** done:

* **No byte total.** The COM `Size` column could not be verified on this machine,
  because the bin was empty. An unverifiable number is worse than no number, so
  the preview reports a count and names, and says in as many words that no size is
  given.
* **`names_target` is judged from stdout, not from the exit code.** Exit 0 with no
  `What if:` line means Windows declined to answer. A preview that read that as
  confirmation would be claiming something it does not have (D53). When
  `names_target` is false the preview sets `windows_said_nothing_useful`, and the
  executor **raises without calling `recycle_clear` at all** - a test asserts on
  the call log, not on a word.

The preview also carries the one clarification no other T3 needs: emptying the bin
**does not touch this system's quarantine**, which is where `file.delete` puts
things and where `file.restore` gets them from. Without it, a user could reasonably
believe emptying the bin had made their quarantine unrecoverable.

### The journal's first row with nothing in it

D60 gave every T3 an undo journal. `file.empty_recycle_bin` is the first action
with **no inverse**, and it still gets a row - with `undo: null` and a reason.
*"A journal that only records what it can undo cannot tell you which of its entries
were the ones you cannot take back."* Two rows per attempt, as before: one opened
before acting with `ok: null`, one closed after with the real outcome, and a failed
attempt is closed `ok: false` like any other.

`_admin_journal` is now used by a tool outside the administration section, which it
is named for. **It was not renamed.** Renaming is the tidier change and would have
touched D60's committed tests for a naming preference; the smell is recorded here
instead, and the method's docstring now says what it is (every T3) rather than what
it was born as.

### `overwrote` is measured, not reported

`file.write_notes` writes into the Obsidian vault. `write_note` never overwrites by
default - a repeated title produces a **timestamped sibling**
(`a-note-2026-20261006-052847.md`). Reporting `overwrote: false` from the default
would have been a claim the bridge could contradict (D53). So it is measured from
the filename: `overwrote = target.name == f"{slugify(title)}.md"`. True means this
write is the plain-named file; false means a sibling was written and the older note
is untouched, and the result carries `previous` plus *"Nothing you wrote was
replaced. If you meant to update the older note, say so and it will be done
explicitly."*

There is no `path` argument at all - the vault picks the directory, the same
argument as `telegram.send_draft` and the teaching ledger. A T1 write tool that
takes a path from the caller is an arbitrary-write primitive wearing a name.

### Three defects, one shape

1. **`how_we_counted` said half of what it was for, and one thing twice.** The
   field named *how* we counted but omitted the statement its own design called
   for - *why not the obvious way* - so the dialog invited exactly the question it
   should have answered. And it repeated the size caveat that `listing["note"]`
   had already carried. One field explaining the same thing twice reads as two
   separate faults to whoever is approving. That is the same shape D62 caught in
   the teaching refusals, in a different file.
2. **`sysinfo.py` was missing `import json`.** It would have been a `NameError` in
   `_recycle_payload`. Caught by reading the imports before running the code - the
   same shape as D60's *"daemon.py used `re` without importing it"*. Twice is a
   pattern: **read the imports before you write the function that needs them.**
3. **Three tests asserted a message no code produced.** `"the bin is as it was"`
   was never written. The real message is `"emptying the recycle bin failed with
   exit 5: "`, which is the house pattern every other T3 uses (`sc`, `package
   uninstall`, `power.shutdown`). The test now asserts the two **facts** - the exit
   code reaches the user, and the journal row is closed `ok: false` - and records
   why: `Clear-RecycleBin` failing part-way could have removed some items, so a
   reassurance about the bin's state would have been a guess, and **whether a
   partial clear is possible was not measured, so the tool does not claim either
   way.** The code was left alone deliberately: a special message here would be a
   second convention beside the house one.

### The stand-in that died three times

`tests/test_confirm.py` and `tests/test_daemon.py` each assert that a **destructive
T3 with no executor** reports `executable: false` and is never audited as having
run. Each named a specific tool as its example. The examples were `package.install`,
then `file.empty_recycle_bin` - and each became false **the day that tool gained an
executor**. The assertion died twice for reasons entirely unrelated to what it
tests, and this slice was about to kill it a third time.

Both now **derive** the example from the property itself:
`[s.name for s in ALL_TOOLS if s.destructive and s.tier.name == "IRREVERSIBLE"
and s.name not in Daemon._EXECUTORS]`, with an assertion that the list is non-empty
and a message saying what to do if it is empty - build a tool that cannot run, or
delete the section and say in the commit message that the property is now vacuous.

The general finding, and it is about tests rather than code: **a test that names an
example of a property is asserting about the example.** The property is what is
under test, so the test should ask for whatever currently has it. The count of
times a stand-in had to be moved is the evidence - and the first assertion in this
project that failed three times for one reason was found by running the suite, not
by reading the test.

---

## D64 - The daemon could start exactly once, and 1025 tests agreed it worked

Found by launching the daemon rather than running its tests, on the way to
shipping it. It is the most consequential defect in this file, and the least
visible: the project is specified as a 24/7 resident service and the code could
serve one process lifetime.

### The measurement

```
$ python -m assistant.daemon            # second ever start, real state/ directory
  File "src\assistant\daemon.py", line 508, in serve_forever
    ipc.store_authkey(self.authkey_path, self.authkey)
FileExistsError: [Errno 17] File exists: 'G:\localaized\state\authkey.bin'
```

Not a stale-file nuisance. Three facts, each measured:

- `Daemon.__init__` mints a **fresh** key on every construction (`daemon.py:305`).
- `store_authkey` opens the file `O_WRONLY|O_CREAT|O_EXCL` (`ipc.py:108`).
- **Nothing removes that file.** Not `shutdown()`, not an exit path.

So the second start raised `FileExistsError`, and every restart after it - reboot,
crash, service restart - died identically until a human deleted
`state\authkey.bin` by hand. The failure is *silent about its own cause*: the
traceback names a file, not the fact that the daemon is single-use.

### O_EXCL was right, and half of it was missing

The `O_EXCL` was a deliberate decision with a correct reason, written down at the
call site: never silently overwrite a key a **live** daemon is using, because two
daemons on one machine is a bug we want to hear about. That intent survives this
fix intact - `test_authkey_is_never_silently_overwritten` still passes and
`acquire_authkey` still refuses by default.

What was wrong is that one flag was being asked to answer **two** questions:

| Question | Answered by |
|---|---|
| *May I write this file?* | owning the state directory |
| *Is a key already there?* | reading it |

Ownership is not a property of the file; it is a property of the **machine**, and
the pipe is where that is decided. Windows fails a second `bind()` of a live pipe
name with `PermissionError [WinError 5]` - verified before relying on it, not
assumed. So binding the pipe *is* the proof of ownership, and once it has
succeeded, a key on disk is simply a fact: either ours from a previous run, or
nothing.

Adopting the existing key rather than minting a new one is what makes the restart
seamless. Rotating on every start would work for the daemon and fail for every
component that read the key earlier - the same shape as D38's grammar bug, where
the new value is right for the writer and wrong for the reader.

A clean shutdown now removes the credential, which restores an invariant worth
having: **a key on disk means a daemon is running.** That is also why a crashed
daemon (no `shutdown()`, key left behind) restarts correctly while a cleanly
stopped one mints a fresh key - the difference is real, and it is the difference
between a stale file and an absent one.

### The second defect, found by the first fix

`start_serving` originally wrote its `daemon.start` audit record **before**
binding the pipe. The docstring said "bind first"; the code did not. So a second
daemon contending for the same state directory appended a record to the shared
hash-chained `audit.jsonl` and *then* raised on the bind. Its record committed to
the losing daemon's in-memory chain state, so the incumbent's chain stopped
verifying - and per ARCHITECTURE.md a broken chain refuses startup, which meant
**one refused daemon turned a working service into a permanently broken one.**

The rule, which is the general part: **prove ownership before writing, never
after.** A process that fails to start must leave the state directory
byte-identical to how it found it. Startup now reads the key, binds the pipe, and
only then writes - and if `preflight()` refuses a broken chain, it hands back the
pipe and the credential on the way out rather than leaving either behind.

### Why 1025 tests passed

Because no test ever started **two** daemons against **one** state directory.
`test_authkey_is_never_silently_overwritten` asserts the refusal in isolation -
correct, and silent about the consequence. `test_tiers.py` writes a key by hand to
*reproduce* production state, so the state was always prepared rather than
reached by starting a daemon twice.

This is D61's exact shape, and the generalisation is worth stating: **a test that
asserts a property of a component in isolation cannot find a defect that lives in
the transition between two instances of it.** Lifecycle bugs need a lifecycle test
- a second real daemon on a real state directory, against a real pipe. That is
now `tests/test_authkey_restart.py`, 11 tests, no mocks: a mock of the key file
would have passed against the broken implementation, which is the entire reason
the bug survived a suite this large.

Suite after the fix: **1036 passed, 3 skipped, 47 subtests**, and two sequential
starts of the real daemon on the real `state/` both report `authkey adopted from`
with the chain still verifying at 21 records.

---

## D65 - Four complete modules and no way to start the product

The assistant had a daemon, a tray icon, a chord, a presence monitor, a prompt
window and a confirmation dialog. All six were tested. **No module imported
another.** `Tray` and `Overlay` appeared in `tests/` and nowhere else, so the
product could pass 1025 green tests and still have no route to being started -
and Phase 2's gate, "hotkey opens the overlay with no terminal visible", was
unreachable because nothing owned the process.

Measured, not inferred:

```
Tray(          referenced from src/ : 0 modules (12 call sites in tests/)
Overlay(       referenced from src/ : 0 modules (24 call sites in tests/)
python -m assistant               : does not exist
python -m assistant.daemon        : serves a pipe and nothing else
```

A grep for the wiring is the whole diagnosis. The individual components were
never wrong; they were never *composed*.

### The only thing that had to be decided was the order

`launcher.py` contains no new logic, and that is deliberate - the daemon still
holds the gate, the leases and the chain, and the tray is still a *client* of
the daemon rather than a second front door. What the launcher adds is an order,
and one invariant the order exists to hold:

1. **Bind the pipe first.** Nothing appears on screen until the daemon is
   genuinely serving. A tray icon for a daemon that lost the race to another
   instance is a lie about a running service: the menu would answer, and the
   answers would be nothing.
2. **Then the chord, the presence monitor, the tray** - chord before tray, so the
   icon cannot appear without the summons that belongs to it.
3. **Then park**, and let the tray's Exit item, a signal, or a second instance
   stop the daemon.

A refused start leaves no icon, no chord, no credential and no audit record -
the same rule D64 established for the state directory, applied one level up.

### A taken chord is survivable, and has to be

`tray.HotkeyUnavailable` already existed and already documented why: Ctrl+Alt+Space
is a popular IME toggle on several East Asian input method editors. Treating that
as fatal produces a daemon that starts and can never be summoned, which the user
experiences as *the assistant is broken* rather than as *something else owns my
shortcut* - two problems with the same symptom and different fixes.

So losing the chord costs the chord. The tray still comes up, and says why, in
red, so the two are distinguishable. The alternative - refusing to start - trades
a working assistant for a correctly-mourned one.

### Proof, and the limit of it

Two of the gate's claims are checkable without a screen, and both were checked
rather than assumed:

```
$ probe_chord.py                      # tries to register Ctrl+Alt+Space itself
before launch : chord was FREE
while running : ALREADY TAKEN (GetLastError=1409 = ERROR_HOTKEY_ALREADY_REGISTERED)
after stop    : chord was FREE                  <- released cleanly
live pipe     : ping -> pong | v0.3.0 | 51 executors | attended
threads       : tray, hotkey, presence, brain-sweeper, telegram, serve:<pipe>
```

**1409 while running and free again after stopping is the whole gate**, measured
from a second process: the chord is genuinely registered by Windows on the
assistant's behalf, and genuinely released. That is a stronger claim than "the
code called `RegisterHotKey`".

What is *not* proved: that the tray icon renders, and that the tkinter window
appears correctly. Both need eyes on the screen, and this is the first thing in
the project that cannot be closed by a test. It is recorded here as an open item
rather than as a gate that passed.

### The tests are about behaviour, and were checked by breaking them

Eleven tests, none mocking the thing under test. `Overlay` already set the
convention - its `call` is injected "so the tests can drive this whole module
with no tkinter display" - so the launcher takes its three OS components as
factories whose defaults are the real ones.

Two were mutated to confirm they fail for the reason they name, because a first
attempt at the ordering mutation only moved a line and still passed, which would
have made the ordering test worthless:

| Mutation | Test | Result |
|---|---|---|
| hotkey wired to `lambda: None` | `test_the_hotkey_opens_the_prompt_window` | **failed** as intended |
| a refused daemon still gets a tray | `test_the_daemon_is_bound_before_the_tray_appears` | **failed** as intended |

The general finding is D64's, one level up: **a test that cannot fail when the
property is broken is not a test of that property.** Both of these would have
passed against a launcher that built every object and connected none of them -
which is precisely the state the project was in.

Suite after: **1047 passed, 3 skipped, 47 subtests.** Plane A's load-bearing
invariant still holds - importing `daemon`, `launcher`, `tray` and `overlay`
pulls in no llama.cpp, no torch, no onnxruntime and no faster-whisper, so the
assistant is still a few hundred MB with no VRAM (D1).
