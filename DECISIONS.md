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


## Part VI - What Phase 1 measured

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

---

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