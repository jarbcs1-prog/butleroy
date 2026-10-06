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

---

## D66 - The install path was the last place D25 could still bite

Everything ran, and there was no way to install any of it. No `pyproject.toml`,
no requirements file, no `INSTALL.md`, and a suite that failed at collection on a
fresh checkout with `No module named assistant` unless `PYTHONPATH` was set by
hand. The whole thing was runnable from one directory, by one person, who
happened to have set it up.

### The trap, and why a comment was not enough

The obvious way to write a manifest is also the dangerous one. Declare
`llama-cpp-python` as a dependency and `pip install .` fetches the prebuilt
wheel - the one whose `ggml-cpu.dll` is compiled with AVX-512 and dies with
`STATUS_ILLEGAL_INSTRUCTION` on this Ryzen 5 5600G. So a dependency line would
have been a runtime that cannot execute on this machine, installed by the exact
command a new user is told to run, failing later inside llama.cpp with a message
that names nothing about the install.

The tempting fix is a comment in the file. D36 is the argument against that: a
constraint that is only written down is a constraint that something can
overwrite, and the test suite is the only thing in this project that has never
quietly stopped working. So `tests/test_install.py` asserts `llama-cpp-python`
is **not** in `[project].dependencies`, and fails with a message that says where
it does belong. The comment explains it to the next reader; the test prevents it.

### The core dependency set is almost empty, and that is the architecture

`pystray` and `pillow`. That is the whole thing, and it follows from D1 rather
than from minimalism: Plane A is pure stdlib, which is the *reason* a 24/7
daemon costs a few hundred MB and no VRAM. Measured per module before writing a
single line:

| Module | Third-party |
|---|---|
| `daemon`, `policy`, `ipc`, `audit`, `arbiter`, `manifest`, `grammar`, `obsidian`, `delegate` | **none** |
| `tray` | `pystray`, `PIL` |
| `overlay` | `tkinter` (stdlib) |
| `brain`, `brain_server` | `llama_cpp` |
| `speech` | `faster_whisper`, `piper`, `numpy` |
| `audio` | `openwakeword`, `sounddevice`, `numpy` |

So `butleroy` installs in **2.9 s**, resolves 4 packages, and cannot quietly become
a multi-gigabyte download for a tray icon. The heavy planes are extras (`brain`,
`voice`, `wakeword`), each independently skippable, and the tests assert
torch/faster-whisper/onnxruntime/librosa stay out of the core - because an
install that pulled them in would break D1 at install time, long before anyone
noticed.

### A verifier that imports is not a verifier

`verify_install.py` replaces a 6-line `check_install.py` that printed versions and
stopped. Its check that matters is `inference`, and the reason it exists is the
same trap: **the AVX-512 wheel imports perfectly and only faults on first
execution.** A verifier that stopped at `import llama_cpp` would report a broken
install as healthy.

```
[ ok ] python     3.11.14, 64-bit
[ ok ] imports    assistant.* imports; no model runtime loaded
[ ok ] no_ports   no TCP listener; transport is named pipes only
[ ok ] isa        ggml-cpu.dll: 10/984 EVEX-shaped (1.02%), under the 10% limit
[ ok ] daemon     preflight ok: 26 audit records verify, no listening ports
[ ok ] models     5 model file(s), 13.04 GB
[ ok ] inference  Qwen3.5-0.8B-Q4_0.gguf: generated 8 chars in 0.9s, 25/25 layers on GPU
```

Three things in there are decisions rather than code:

1. **`isa` measures a ratio, not a count.** A `0x62` byte is also the `BOUND`
   opcode and turns up in ordinary code and data - D25's own control DLL had
   *more* `0x62` bytes than our build and was clean. The threshold is 10 %,
   sitting in a gap between 76.7 % (broken) and 2.74 % (control), because a
   threshold in the middle of a real gap is not a guess about someone else's
   build. Tested against a synthetic EVEX library planted byte by byte, and
   against a large library full of `0x62` bytes with none EVEX-shaped - the
   guard is shown catching the failure mode, not merely present.
2. **`inference` asserts on llama.cpp's own `offloaded N/N layers to GPU` line**,
   captured by dup2'ing the real stderr, and **fails when the line is missing**
   rather than falling back to something weaker. Partial offload is silent CPU
   spill on six cores. `Llama.n_gpu_layers` does not exist in 0.3.35 - asking for
   `-1` leaves `0x7FFFFFFF` on `model_params`, which is what we *requested*, not
   what we *got*. The first draft of this file printed exactly that `None` and
   called it a result, which is D38's shape again: it compiled, it ran, and it
   reported nothing. `phase0_gate.py` already had the right helper; reusing it
   beat writing a second one.
3. **A missing model skips rather than fails.** 13 GB of weights are not shipped.
   A verifier that failed on their absence would report a good install as broken,
   and the user would learn to ignore it.

### The credential, reduced rather than excused

`state/telegram.json` held a live bot token in plaintext. It was gitignored, which
is not the same as secret. Now `BUTLEROY_TELEGRAM_TOKEN` may carry it, so it
need never exist on disk; the allowlist stays in the file, because it is
configuration rather than a credential and belongs somewhere a human edits it.
And `describe()` reports *where the token came from* - naming the location, never
the value - so "this machine holds a credential in plaintext" is a fact `status`
can show rather than something nobody thinks about for four years.

An environment token that is **malformed** is refused rather than ignored:
falling back to the file would make a typo in a variable look exactly like one
that worked, and the rotated credential would keep being the old one. An
environment token that is **empty** is ignored, because a cleared variable is
recoverable and refusing it would break a machine that was working an hour ago.

There is deliberately **no command that writes the token.** A feature that writes
a credential is a feature that can leak one; rotation is @BotFather plus one edit.

### Two small things the suite was silently paying for

- **`pythonpath = ["src"]` in `pyproject.toml`.** Until then the suite failed at
  collection with `No module named assistant` unless `PYTHONPATH` was exported -
  which is how 1025 tests once looked like a broken environment rather than a
  missing setting. Now `pytest` alone works from a fresh checkout.
- **460 KB of download logs untracked**, and `check_install.py` + `probe_cpu.py`
  deleted as unreferenced and superseded. The two `build_phase0b*.log` files stay
  tracked on purpose: PLAN.md cites them, and attempt 1 failing is the more
  useful record than attempt 2 succeeding (D29). `.gitignore` now says so
  explicitly, with a negated pattern, because `*.log` is exactly the kind of
  rule that quietly swallows the evidence it was meant to spare.

Suite after: **1068 passed, 3 skipped, 47 subtests**, with no `PYTHONPATH` set.
`uv pip install -e .` resolves 4 packages in 2.9 s, leaves the D25 build
untouched, and produces a working `butleroy` command.

---

## D67 - The window was mapped, titled, and completely dead

Reported by the user using it, which is the only way this was findable:

```
$ python -m assistant
[daemon] listening on \\.\pipe\locala.brain.req
[daemon] authkey  adopted from G:\localaized\state\authkey.bin
[daemon] telegram  bridge configured, 1 allowed chat id(s)

Ctrl+Alt+Space: nothing happens.
Tray icon -> click: a window titled "Local assistant 0.3.0", entirely white.
And it sat on top of everything, so it had to be closed to see the desktop.
```

D65 recorded this as the one gate "not closable by a test". It was closable. The
gate was never closed because **the overlay's lifecycle had no tests at all** -
24 tests covered what the T3 dialog *says*, and not one of them called `show()`,
`hide()` or `destroy()`. The three methods a user touches first were the three that
were broken, and they were the only ones that ever construct a real Tk root.

### Four defects, all in the same three methods

1. **No event loop, anywhere.** No `mainloop()`, no `update()`, no pump. This is
   the white window, and it is worth stating precisely because every cheap check
   passes: `winfo_exists` 1, `winfo_ismapped` 1, `winfo_viewable` 1,
   `state()` `normal`, `title()` correct, `geometry()` honoured. A `Tk()` that is
   never pumped maps a perfect window and paints nothing, because painting *is*
   event processing. It looks like a working window to everything short of a
   screenshot - which is why the unit tests passed and the user saw a
   white rectangle.
2. **`_build()` called `_poll()` while `self._root` was still `None`.** `_poll`
   opens with `if self._closing or self._root is None: return`, so it returned
   immediately and no `after()` job was ever registered. Instrumented and
   measured: `_poll` was entered with `self._root = None`, and `_poll_job` was
   `None` afterwards. So the **one-second poll that raises pending T3
   confirmations had never once been armed** - a destructive action queued from
   Telegram would silently never raise a dialog, and nothing would say so. That
   is the worst shape a bug in this module can take: the safe path quietly stops
   existing, and the user is left holding a pending deletion with no way to reach
   it.
3. **Tk was being touched from two threads.** The root was built on whichever
   thread called `show()` - the hotkey thread - while `_pump` ran on its own
   thread and reached `Text.insert` through `_handle_event`. tkinter binds a root
   to its creating thread and says so: `RuntimeError: main thread is not in main
   loop`, reproduced by calling `root.update()` from any other thread.
4. **A leak, and a bad default.** `Tray.stop()` stopped the pystray icon and
   nothing else, so **no code path anywhere called `overlay.destroy()`** - every
   shutdown left a live Tk thread and a live Tcl interpreter. And the prompt
   window was permanently `-topmost`, which is what the user had to close to see
   their own desktop.

### What the fix is

One Tk thread, named `overlay-tk`, that builds the root, runs `mainloop()`, and
is the only thread permitted to touch a widget. `_pump` stops being a thread and
becomes an `after` callback; `_poll` is armed from `_tk_main` once `self._root`
exists. `show`/`hide`/`destroy` marshal onto that thread through the queue that
already existed, via `_invoke`, which is **re-entrant** because `_ask` calls
`show()` from the Tk thread and an unconditional post-and-wait there would hang
the assistant on every prompt that arrives from Telegram.

The topmost flag came off the prompt window and stayed on the T3 dialog. That is
not a compromise: a destructive approval that can be buried behind a browser is an
approval nobody sees, which is the reason `overlay.py` gave for putting it there
in the first place. The conversation window raises itself on demand anyway, so
permanently pinning it bought nothing and cost the desktop.

### Two bugs in the fix, caught before shipping

Both were found by running the thing, which is the only reason they are cheap:

- **`destroy()` ate its own sentinel.** It set `_closing = True` and then queued
  `None`; `_pump` treated `_closing` as "drop everything queued", so the sentinel
  was never read, the root was never destroyed, `mainloop()` never returned, and
  every shutdown sat in the full 10-second join. The test file went from 80 s to
  1.4 s when the sentinel was moved ahead of the `_closing` check.
- **The exit code was -2147483645 after a perfectly clean shutdown.** The daemon
  exited with a Windows debug break, which a scheduled task records as failure.
  Cause: `Overlay` keeps `transcript`, `entry` and `status` as attributes, those
  are widget objects, each holds `.tk`, and that is the Tcl interpreter. Dropping
  `self._root` was not enough - the interpreter stayed alive on attributes that
  belong to the Overlay, and was finally released at *process* shutdown on the
  main thread, so Tcl deleted a per-thread async handler from the wrong thread.
  They are now cleared in `_tk_main`'s `finally`, **on the thread that created
  them**. Exit code 0, no warning. Two wrong hypotheses came first - `tkinter
  ._default_root` (already `None`) and the widgets cleared on the *main* thread -
  and both were cheap because they were measured rather than reasoned about.

### The test that was hollow, and had to be rewritten

The first version of `test_overlay_lifecycle.py` used a fake root that only
*recorded* `after` callbacks and left draining them to the test. That would have
passed against a Tk whose loop was never started, because **the test supplied the
pumping that production does not** - it would have "fixed" a blank window by
building a second blank window. The fake now runs a real loop (due jobs every
5 ms until destroyed), which is what makes "a queued destructive action appears
without anyone asking" an assertion about the code rather than about the test.

Eleven tests. The two that matter most:

| Assertion | What it would have caught |
|---|---|
| `after()` fires on a real Tk; text from another thread reaches a real widget | defects 1 and 3 |
| `destroy()` returns in <3 s | the sentinel bug |
| a T3 request surfaces with nobody calling `show()` or `_poll()` | defect 2 |
| the T3 dialog is still `-topmost` | trading the safety property for comfort |

Verified on the genuine Tk 8.6, through the real `App`, real `Tray` and the real
chord: `chord delivered -> True`, `viewable -> True`, `poll armed -> True`,
transcript rendering, `topmost -> 0`, `destroy() took 0.00 s`, **exit code 0**.

### And a pre-existing flake, fixed while here

`test_posting_a_hotkey_message_reaches_the_callback` failed once in a full-suite
run and passed in isolation - the exact failure `tray.py`'s own docstring records
from an earlier round. `Hotkey.trigger()` allowed the pump 2 s to consume the
message, and on a loaded machine that is not always enough. Raising the product's
budget would have been wrong: 2 s is a UX budget for the tray's menu item, and
falling through to the direct path after 10 s of a frozen menu is worse than the
fallback. So `trigger()` took a `timeout` parameter - production keeps 2.0, the
tests pass 30.0. What the tests assert is *delivery*, not latency, and a 2 s
budget measured on an idle desktop is not a fact about delivery.

Suite after: **1080 passed, 3 skipped, 47 subtests**, twice in a row.

### The spine, proven end to end — and the one thing it does not prove

After the D67 fix the user typed `Hello` into the window and got an answer back.
One turn, and it crosses every layer:

```
overlay._submit -> daemon brain_turn -> arbiter lease -> brain_server child
                -> real llama.cpp on the GPU -> streamed answer -> transcript
```

**The vertical slice works.** The previous real proof was Phase 2, on 2026-10-01,
before D64 through D67 touched any of that machinery — and every one of those
three changes had passed the full suite while doing it.

Recorded here rather than as a victory lap, because of what it does **not** cover.
A greeting correctly produces *no tool call*, so this turn never exercised the
grammar-constrained decision. And D34 is the load-bearing finding of the entire
brain design: under `tool_choice="auto"` the 4B **cannot be made to decide** to
call a tool — it narrates the call in prose and returns no call at all. Pinned to
GBNF it commits every time, and the call arrives in `content`, not `tool_calls`.

That is a **silent** failure. The window would say "I'll list that directory for
you" and then sit there, with the audit chain verifying and all 1,080 tests green.
So a prose turn and a tool turn fail differently, and only the first has been
exercised. The next check is one sentence typed into the window — *how much VRAM am
I using right now* — and it has to produce a tool proposal and then real output.

The general point is D36 again, one layer up: **a constraint that is set is not a
constraint that is active.** The grammar is compiled into every request; nothing
in the suite, and nothing about a greeting, demonstrates it fired.

## D68 - The window proposed the tool and then stopped, and the grammar was never the problem

The next action after D67 was one sentence typed into the window — *how much VRAM
am I using right now* — and the prediction was that it would fail because of D34:
the 4B narrates the call in prose instead of committing. **D34 was right about the
model and wrong about the consequence.** The model committed. The window threw the
proposal away.

### What was actually broken

`Overlay._run_turn` printed `proposing <tool> (<args>)` and returned. It never sent
the proposal back through `tool_call`. Its own comment recorded the intent and the
gap in the same breath — *"A proposal, never an execution. The daemon has already
gated it"* — and that second clause is the bug. The daemon had **not** gated it.
`op_brain_turn` gates nothing; its docstring says the opposite: *"It returns the tool
name and arguments to the caller, who must send them back through `tool_call`, where
the policy gate and - for anything T3 - a human get to see them."* The caller was
the overlay, and the caller did not.

So the window — the one front door the user is physically sitting in front of —
could never run a tool. Not a tool that needed approval, not a tool that could fail
the gate: any tool at all.

### Why it was so cheap to miss

Four properties, all of which point the wrong way:

- **Nothing said so.** The transcript read like an answer in progress. There was no
  error, no exception, no log line, and the status line went back to empty, which is
  what it does when a turn *succeeds*.
- **The audit chain verified.** `op_brain_turn` audits the proposal at
  `decision="allow"`, and that audit was genuine — the brain did propose. A verified
  chain is exactly what a healthy turn looks like.
- **`system.gpu_state` is T0.** It needs no approval and no approved workspace, so
  the read that should have worked most obviously did not, and no policy edge was
  anywhere near it.
- **The other front doors were both correct, which pointed at the model.** Voice
  carries its proposal through (`daemon._voice_turn` → `op_tool_call` →
  `_describe_result`). Telegram deliberately does not and says so in its own return
  text. The window did neither, so "voice works and the window doesn't" reads as a
  model difference rather than a missing line in one method.

That last one is the trap. Two working front doors make a third failure look like a
property of the thing they share — the model — when it is a property of the one
thing they do not share: their call sequences.

### Measured, on the real model, both before and after

`probe_turn_real.py` separates the two passes that D34 and D68 had been conflated
into, because a pass-1 failure and a pass-2 failure are indistinguishable from
outside the window — a sentence on screen and no tool output:

```
PASS 1  decision      : 'tool:\n"system.gpu_state"'   is_tool: True   1.50s
        action        : tool=system.gpu_state arguments={} parse_error: none

PASS 2  before  ops   : confirm_list, brain_turn, confirm_list, confirm_list, confirm_list
        transcript  | proposing system.gpu_state ()
        VERDICT: PASS 1 ok, PASS 2 FAILED

PASS 2  after   ops   : confirm_list, brain_turn, ..., tool_call
        transcript  | proposing system.gpu_state ()
                    | count: 1
                    | measured: true
                    | gpus:
                    |   name: NVIDIA GeForce RTX 3060
                    |   vram_total_bytes: 12,884,901,888
                    |   vram_used_bytes: 6,390,022,144
                    |   vram_free_bytes: 6,314,524,672
                    |   utilisation_percent: 39
                    |   temperature_c: 55
        VERDICT: PASS 1 ok, PASS 2 ok
```

Through the real `VramArbiter` reading real `nvidia-smi` (10.84 GiB free against a
7.00 GiB request, granted), the real grammar, the real 4B on the GPU, the real gate
and the real executor. `vram_used_bytes: 6,390,022,144` is the probe's own model —
the load the handoff asked about is visible in the number the window now displays,
which is a nice incidental proof that the tool read the machine rather than a fixture.

**The spine is now closed for a tool turn, which `Hello` never was.**

### The fix

One method, `_carry_through`, doing what `_voice_turn` already did. It posts the
proposal, sends it through `tool_call` with `channel="pc"`, and then reports
whichever of `tool_call`'s four legitimate outcomes came back:

| outcome | what the window says |
|---|---|
| ran | the real payload |
| `needs_confirmation` | the confirmation id, and that it is waiting on a human |
| `queued` | queued until you are back at the keyboard |
| refused / not built | which of the two it was |

The last row is D58's rule kept: "I will not do this" and "I have not built this yet"
are different facts about the world, and `handle` already distinguishes them with a
flag. Conflating them is how unfinished work gets reported as finished.

### Showing the data, not a sentence about the data

`_describe_result` was deliberately **not** reused. It is for a *spoken* answer and
prefers a human summary, because a dict read aloud is a log line. On screen with the
question still visible the situation is inverted — `Done: system.gpu_state.` is
indistinguishable from success and answers nothing — so `_render_result` shows the
payload, nested and with grouped separators, and truncates at 1200 characters with
the truncation stated.

This is D36 one layer up again, and it is worth being precise about the pattern,
because it has now appeared four times at four different heights: **the D68 window
would have passed a test that asserted the turn "handled" a tool.** What made it
findable was the probe asserting on the *payload* — that the VRAM figure the user
asked for is actually on screen — rather than on the fact that something ran.

### Ten tests, and the two that were hollow first

`tests/test_overlay_turn.py`. The whole defect is one assertion
(`test_a_tool_turn_reaches_tool_call`), and it is the transition that is asserted:
one overlay, one daemon-shaped `call`, `brain_turn` → `tool_call`, and the
conversation that comes out. That is the project's stated blind spot — *tests that
assert a property of a component in isolation cannot find a defect living in the
transition between two instances of it* — applied to the transition between two ops.

**Two of the ten passed against broken code on the first run, and both are worth
recording because they were found by trying to break them rather than by reading
them.**

1. `test_a_raise_mid_turn...` asserted `ov._busy is False` after a raise, but called
   `_run_turn` directly and so never set `_busy = True` first. The flag started
   `False`; the assertion passed whether or not the `finally` existed. It survived
   the mutation that removed the `finally` entirely.
2. `test_a_t3_proposal_waits_for_a_human...` asserted only that `c000007` appeared
   somewhere in the transcript. Dropping the whole confirmation branch still put the
   string there, because the pending envelope *has* a `confirmation_id` field and
   `_render_result` dutifully printed it. It survived the mutation that deleted the
   branch.

Both are the same failure D67's file records, one generation on: a test that cannot
fail is worse than no test, because it is a green light on nothing. Seven mutations
were applied to `overlay.py` to confirm the other eight are load-bearing:

```
1 drop the carry-through            8 failed    2 render like a spoken turn   1 failed
3 drop the finally                  1 failed    4 swallow a refusal           2 failed
5 drop the confirmation branch      1 failed    6 lose the channel            1 failed
7 print the payload as python repr  1 failed
```

Mutation 3 is the one worth noting: the `finally` around the turn body is three lines
and guards a flag whose failure mode is a window that looks entirely normal and
silently refuses every later question. No exception, no log line.

### What this says about the two questions that were open

- **D34 survives intact.** The grammar-first decision is load-bearing and it works:
  `tool:\n"system.gpu_state"` in 1.50 s, then a clean constrained call, no parse
  error. What D34 could not have told anyone is that the commit was being thrown
  away 40 lines later by the window.
- **The handoff's own warning was right, and for a narrower reason than stated.** It
  predicted a *silent* failure and got one. It attributed the silence to the model
  narrating; the model committed. It attributed the risk to a missing test; the
  suite was not missing a test of the decision, it was missing a test of the call
  *sequence*.

Suite after: **1090 passed, 3 skipped, 47 subtests** (1080 before, +10 new).

## D69 - The T3 confirmation dialog could not be built, and 24 tests said it was fine

The chain D68 left open was: drive a destructive turn from the window and watch
propose -> confirm -> dialog -> approve -> run. It fails on the third step, and the
cause is a colour.

    tk.Button(buttons, ..., activebackground="#3182ce6", ...)
    _tkinter.TclError: invalid color name "#3182ce6"

Seven hex digits. `#3182ce` is a colour; `#3182ce6` is not, and Tk says so by
raising. **`_Confirm.__init__` raised while constructing the dialog, so every T3
confirmation in the product died on that line.**

### Why it is invisible from a unit test

`test_overlay.py`'s `NoButton` ignored every colour it was handed:

```python
class NoButton:
    def __init__(self, *a, **kw):
        self.text = kw.get("text", "")
        self.command = kw.get("command")
```

So all 24 tests on the confirmation rules passed against a dialog that cannot be
constructed at all — the payload-hash binding, the expiry-denies behaviour, the
absence of an "always allow" button, all of it asserting on objects the fakes
invented rather than on a widget Tk had ever agreed to create.

This is D67's lesson, applied one level too high. D67 recorded that "fakes must
fail on behaviour, not on method coverage", and the fix then made `NoRoot` run a
real event loop. It did not ask whether the *widgets* were real. They were not,
and the part that was not real happened to be the only part Tk validates.

The general shape, now seen four times: **the fake agreed with the code about
everything the fake did not check.** `NoButton` recorded `text` and `command` and
ignored `bg`, `fg`, `activebackground`, `relief` and `padx` — so it agreed with
`#3182ce6` because it had no opinion about colours.

### And it failed *silently*, which is the part that matters

`_poll` calls `_present` from an `after` callback. A Tk exception in a callback is
printed to stderr and swallowed — the event loop keeps running. So on a real
desktop:

- the dialog never appeared,
- the poll kept firing every second and kept failing every second,
- the pending confirmation stayed pending forever,
- and the user was left holding an action they could not approve *or* refuse.

That is D67 defect 2's exact failure mode — "a destructive action queued from
Telegram would silently never raise a dialog" — reached again by a different
route, in the same method, two decisions later. The poll that D67 fixed the
arming of was firing perfectly; what it found could not be displayed.

### The second defect, in the same three lines

With the colour fixed the dialog built, appeared and worked. The probe then exited
**-2147483645** after a clean shutdown, which is D67's exit-code bug:

    Tcl_AsyncDelete: async handler deleted by the wrong thread

D67 diagnosed this precisely for the Overlay: `transcript`, `entry` and `status` are
widget objects, each holds `.tk`, and that is the Tcl interpreter, so they are
cleared in `_tk_main`'s `finally` **on the thread that created them**. The same
class of attribute was left in `_Confirm`: `top`, `body`, `clock`, `btn_once`,
`btn_class`, `btn_deny` — six of them, and `_settle` destroyed the Toplevel
without clearing any.

So a *settled* dialog kept the interpreter alive through attributes on an object
the caller may still be holding, and it was finally released at process shutdown on
the main thread. Measured three ways, which is what made the fix obvious:

| variant | result |
|---|---|
| references cleared on the Tk thread | exit 0 |
| dialog held past shutdown | **-2147483645** |
| references cleared before the caller drops its ref | exit 0 |

The third row is why `_release_widgets` is a method and not a line in `_settle`:
**order matters**, and only the Tk thread can do it. It runs after `_sender` is
started, because `_send` reads `self.item` and not the widgets, so the answer path
is unaffected.

Two wrong hypotheses first, as usual. `destroy()` on the Toplevel was already being
called, so it is not a missing destroy. And holding the reference alone is not the
cause — a settled dialog dropped by its owner exits 0 fine. It is specifically a
*settled* dialog whose widget attributes survive.

### The tests, and the one that is skipped when it matters

`tests/test_overlay_real_dialog.py`, five tests, and the first one is the whole
defect:

```python
def test_the_confirmation_dialog_can_actually_be_built(live_overlay):
    dlg = live_overlay._present(dict(ITEM))
    assert dlg is not None
```

There is nothing to assert *about* — the bug was not a wrong colour in a variable,
it was a dialog that never existed. It fails by raising, which is the honest shape.
The rest: the armed poll raises a pending confirmation with nobody calling
`_poll` or `show`; `-topmost` survives (D67 took it off the prompt window to stop
it covering the desktop, and it would have been easy to sweep it off the dialog in
the same pass); the dialog is transient against the prompt window; and a settled
dialog drops all six widget references.

Two changes to existing files:

- `test_overlay.py`'s `NoButton` now rejects a colour Tk would reject, with the
  reasoning in place. Restoring the seven-digit colour fails 22 of its 24 tests.
- `test_the_class_grant_is_the_number_the_user_chose` reads `btn_class.text`
  **before** settling, because `_settle` now clears that attribute. Asserting on a
  released object would fail with an `AttributeError` that says nothing about the
  grant — the failure mode this project has now been bitten by three times.

`test_overlay_real_dialog.py` skips without a display. On this machine it runs,
which is the only place the claim is checked; that is stated in the module
docstring rather than left for a reader to assume.

### The chain, closed

Re-run after both fixes, on the real model, through the real arbiter, gate and
executor, against a real file in a real approved workspace:

```
STEP 1  model commits to file.delete          5.9s
        | proposing file.delete (path='.../notes.txt')
        | waiting for your approval as c000001
STEP 2  gate: T3, "regardless of channel", preview action=quarantine count=1
        -> the id in the window matches the pending item: OK
STEP 3  dialog up with nobody driving it      topmost=1  transient=True
        | Action: move to quarantine (recoverable, not erased)
        | The model asked for: .../notes.txt
        | Targets: 1, 34 bytes
STEP 4  approve via the real button            "approved file.delete (once)"
        still pending after approval: 0
STEP 5  notes.txt still exists: False         workspace count: 0
```

`notes.txt` was 34 bytes of real content in a real directory the gate had approved,
and it is gone because the user-equivalent clicked the button. The script's exit
code is 0, which it was not before either fix.

Suite after: **1095 passed, 3 skipped, 47 subtests** (1090 before, +5). Both
defects were found by driving the chain, and neither was in the set of things the
handoff expected to be wrong.

## D70 - A mail client that never authenticated, and a fake that hid it

`email.*` is built: six tools, stdlib `imaplib`, no new dependency — which is the
point, because Plane A is deliberately dependency-free (D66) and a mail client is
exactly the kind of thing that arrives as a 40 MB package with its own TLS stack and
its own idea about where credentials live. 57 of 61 tools now have executors; the
four that do not are `reflect.*`.

Two of the traps PLAN.md predicted fired and are worth recording first, because both
were avoided by measuring rather than by being careful.

### The `folder` argument, measured

PLAN.md said to check `folder` / `mailbox` against the path screen. Checked:

    mailbox = 'INBOX' -> allow      folder = 'INBOX'            -> DENY
    mailbox = 'Sent'  -> allow      folder = 'Sent'             -> DENY
    mailbox = '[Gmail]/All Mail' -> allow   folder = '[Gmail]/All Mail' -> DENY

Every real IMAP mailbox name is refused, because `policy._check_paths` screens
argument keys containing "folder" as well as "dir". So the argument is `mailbox`,
and the registry says why. Carving "folder" out of the screen is the trade D63
already rejected for `subdir`.

There is a second-order consequence worth keeping: a test asserts that `folder` is
*still* refused for a genuine path argument, because if the screen stopped refusing
paths the mailbox tests would start passing for the wrong reason. D68 had two of its
ten tests pass against broken code for exactly that shape, and the general lesson is
now written into `HANDOFF.md` §6.

### The destructive-tool example, which had to stop being a lookup

`email.delete` was the last destructive T3 without an executor — which is exactly
what two tests used as their *example* of one. Both looked the example up in the
registry rather than naming it, so building it turned both into failures whose
message said the real thing: *"no destructive T3 tool lacks an executor any more, so
the case this covers is exercised by nothing."*

They had already been moved twice (`package.install`, then `file.empty_recycle_bin`)
and each move was a failure rather than a decision. **A lookup is a coincidence that
happens to be true today.** Both now register a synthetic destructive T3 the executor
table has never heard of, which is the *condition* rather than an instance of it, and
which keeps reproducing as the registry grows.

### The design: the credential is not a parameter

No `email.*` tool takes a password, and that is the sharpest difference from every
other tool in the project. A password that arrives as a tool argument is a password
that reaches the audit chain — append-only, hash-chained, the most-read file here.
`_redact` would catch a key containing "password", but the answer is that the
credential is never an argument: it lives in `state/email.json` or
`BUTLEROY_EMAIL_PASSWORD`, is held in `EmailConfig`, and `describe()`/`__repr__` omit
it. Three tests assert this in layers — the declaration, the log, and the repr — because
"no tool declares a credential" is invisible everywhere else.

Two related decisions:

- **`email.delete` files the message before it expunges it.** IMAP offers no undo:
  `\Deleted` + `EXPUNGE` is gone and the only copy is the client's. Fetching first is
  what makes the word mean here what it means in `file.delete`, and the result says so
  honestly — `recoverable: True` *and* "gone from the mail store", because a preview
  that claimed only the first would be describing a weaker action than the one taken.
- **A precheck runs before the gate.** Found by a test asserting a header injection
  would be refused: `email.send` with a newline in `to` was refused by the *preview*
  ("cannot send") while the request still came back with a confirmation id and a
  dialog for the user to approve. `op_tool_call` resolves the executor before CONFIRM
  precisely so nobody approves a no-op, and it did not cover a call that is malformed.
  So `_PRECHECKS` exists, and the refusal is audited with a reason — "the assistant
  tried to send to an injected address and was stopped" is a fact worth having.

### What the real server found that no fake could

`tests/test_email_live.py` runs an actual IMAP4rev1 server on a real localhost socket:
greeting, CAPABILITY, LOGIN, LIST, SELECT/EXAMINE, SEARCH, FETCH with genuine `{n}`
literals, STORE, EXPUNGE, APPEND, LOGOUT. `imaplib` parses it. It found four bugs,
every one of which the fake-based suite reported green:

1. **The client never authenticated at all.** `_imap_connect` returned a connection
   and issued SELECT; every IMAP server requires LOGIN first, so production answered
   `SEARCH illegal in state AUTH`. It survived because each fake's `connect` did its
   own login — the seam was hiding the absence. The first version of the live test's
   connector did the same thing and reproduced the hiding, which is how the shape
   became obvious.
2. **`MailAuthError` never fired.** `imaplib` raises a bare `IMAP4.error` when a
   server answers NO to LOGIN; the `("OK"|"NO", data)` pair a fake returns is a shape
   `imaplib` produces only when *it* is the parser. So `_check` — and the whole
   distinction between "wrong password" and "network is down", which is the entire
   reason the class exists — was unreachable in production while three fake-based tests
   asserted it worked.
3. **`append()` rejected the date.** IMAP's date_list is `dd-Mon-yyyy`, and `imaplib`
   raises `ValueError: date_time not of a known type` for anything that is not an aware
   `datetime` or a fully quoted string. Passing the message's own RFC 2822 `Date`
   header is the obvious mistake. Now an aware `datetime`, so `imaplib` formats it and
   no locale-dependent `%b` can produce "Mär".
4. **A `{n}` literal needs a `+ go ahead` first.** RFC 3501: the server answers a
   command line ending in a literal with a continuation request, and only then does the
   client send the bytes. Without it `imaplib` sits in `_get_response()` and the call
   times out — which is what the first version of the test server did.

TLS is not exercised, and that is stated in the module rather than implied: a test
server needs a certificate, a certificate needs a crypto dependency, and this project
has none by choice. The connector is injected; everything above the socket is genuine.

### The fakes, and one of their own bugs

`test_email_ops.py` uses a `FakeIMAP` that returns wire-shaped responses. It earned
its keep by proving the logic and by catching a falsy-default in itself:
`dict(messages or {"7": ...})` means an **empty** dict is falsy, so
`FakeIMAP(messages={})` silently became a mailbox holding one message — and the test
asserting "an empty mailbox reads as no mail" was asserting against a fixture that
could not be empty. The same shape as D63: a default that is wrong for exactly the
case you construct it for. `is None` is the only check that means what it looks like.

Eight mutations were applied to `email.py`, `daemon.py` and `tools.py` to confirm the
36 tests are load-bearing. All eight fail at least one test, including the one that
renames `mailbox` back to `folder`.

Suite after: **1138 passed, 3 skipped, 47 subtests** (1131 before, +43 across two
files). Still no destructive T3 tool without an executor, and that is now a
*statement* rather than a coincidence.

## D71 - Self-improvement that cannot happen, rather than self-improvement that is discouraged

`reflect.*` is built: four tools, and **every registered tool in the project now has
an executor**. This is the last group, and it is the one D21 says must be built last
and on purpose — so the interesting question was never how to make reflection work.
It was how to make it *unable to do damage*, which turned out to be the whole design.

### The load-bearing property: it cannot write its own code or policy

Not "is discouraged from". Not "the prompt says not to". **Cannot.** Three
independent mechanisms, and the first is the one everything else leans on:

1. **No `reflect.*` tool accepts a path argument.** They derive every file they
   touch from the daemon's own state directory. A tool that cannot be *pointed*
   anywhere cannot be pointed at `src/`. So "the assistant modified its own code"
   is not a rule being enforced; it is an operation with no representation.

   This is the same argument as `telegram.send_draft` — a T1 write tool that takes a
   path from its caller is an arbitrary-write primitive — except the stakes here are
   the whole thing. A write tool that reasons about changing the assistant, and that
   accepts a path from its caller, would be an arbitrary-write primitive wearing the
   most persuasive name in the registry.

2. **The gate denies the assistant's own tree** (`policy.SELF_PATHS`), independently.
   `tests/test_reflect_ops.py` promotes a lesson and then asks the gate to write into
   `src/`, and expects a refusal — so the independent check is asserted still working,
   not assumed.

3. **The overlay grants no authority.** D21 keeps behaviour and authority separate
   and this is the code that keeps them separate: the `### OPERATIONAL CONSTRAINTS`
   block reaches `default_system_prompt` and nothing else. Not the gate, not a tier,
   not an argument. A promoted lesson can change how the assistant *behaves*; it
   cannot change what it is allowed to do. Measured, not asserted — the probe
   promoted a lesson and then asked the gate to overwrite `daemon.py`:

       overlay says: - check the approved roots before proposing a read, and prefer a
                      root the user granted over one you remember from elsewhere
       gate says   : deny - path 'G:\localaized\src\assistant\daemon.py' resolves
                      inside a protected location

### The four anti-patterns, each with a mechanism rather than a warning

D21 names them, so each got a rule and each rule got a mutation:

| anti-pattern | mechanism | mutation |
|---|---|---|
| **overfitting** — one failure becomes a permanent rule | `distil` and `propose` refuse a candidate observed once | caught |
| **reflection without change** | `check` is required; a proposal without a falsifiable "what would be different" is refused | caught |
| **performative reflection** | the daemon witnesses its own friction, and a candidate the daemon did not witness is never promoted | caught |
| **endless analysis** | one change per cycle, a candidate cap, and a bounded overlay | caught |

The third one is the design decision worth defending, because it is the one that
makes reflection *possible* rather than merely safe. `op_tool_call` now records every
denial, every queue and every build gap into the ledger as it happens. A lesson the
model derived from its own account of a conversation is a lesson about what it thinks
happened; this is a lesson about what the system actually did. Without that
distinction every candidate is a hypothesis, and a promotion gate over hypotheses is
just a delay.

### The scores are computed, never supplied

`validation_score` and `confidence` come out of `scores()`, which reads the ledger.
No tool takes a score argument, and there is a test asserting that absence — because
a model that could score its own proposal would score it ten, every time, and the
gate would be arithmetic with a suggestion on the front.

**Two defects the mutation run found in my own gate, both of the same kind:**

- **The threshold was doing nothing.** `PROMOTION_THRESHOLD = 60` on a scale where
  the product runs 0..10,000, so every candidate passed it. The gate was carrying
  every promotion test on the witness check alone. It is now a *fraction* of the
  maximum, `0.45`, with the maximum derived from the two score ranges so the two
  cannot drift apart.
- **A score term was dead code.** `witnessed = 20 if system_observed` — removing it
  left the suite green, because `_gate` refuses a non-witnessed candidate outright,
  so the term could only ever have applied to candidates already refused. Removed,
  with the reasoning left in place: dead weight in a scoring function is worse than
  no function, because it looks like part of the evidence and is not.

A third term, `specificity`, was also untested. Rather than delete a genuine property
— a vague three-word grouping key should score below a specific one, because vagueness
is what lets a rule absorb every later exception — it got a test, and the mutation now
fails. Twelve mutations, twelve caught.

### Two bugs only the real model could find

The unit tests construct a `Brain` with a roots reader; production does too. So this
was invisible until the probe loaded the actual 4B and asked whether the promoted
lesson was in the prompt it would answer from. It was not, for two reasons:

1. **`current_prompt()` returned early when there was no roots reader**, so a `Brain`
   built without one kept whatever prompt it was constructed with — and the
   constructor built that prompt without the overlay. The whole Phase 6 feature was
   inert for every construction that did not pass a roots reader.
2. **`_overlay` was assigned after the system prompt was built**, and the prompt now
   reads the overlay, so the constructor raised `AttributeError: '_overlay'`. It was
   fine the moment before that, which is the worst possible moment to be fine.

Both were ordering mistakes in a constructor, and both are the kind that a test which
passes a reader takes the other branch past without noticing. The fix reorders the
constructor so the prompt is built last, from both.

### The honest-reporting property, which is unusual and which is tested

A reflection tool that can only report success is a vanity metric, and its failure
mode is not a crash — it is a system that appears to be learning. So
`reflect.report` is *required* to be able to say "nothing repeated yet", and
`test_an_empty_ledger_reports_nothing_and_does_not_call_it_a_failure` asserts it. The
commonest state is "no lesson yet" and it must read that way.

Related, and found by the mutation: a burst of identical rows inside a one-minute
window is one observation, so a retry loop cannot defeat `MIN_OBSERVATIONS` — while
the same complaint an hour later *is* a second observation, so a problem that
happens every day can still become a lesson. The first version keyed on the exact
timestamp, which meant the test asserting the burst collapsed passed or failed
depending on the millisecond.

### Proven end to end, on the real model

    4 genuine gate refusals, recorded by the daemon as it refused them
      -> 1 candidate after the two-observation floor
      -> validation 60 x confidence 100 = 6000 of 10000, above 45%
      -> promoted
      -> overlay present in the prompt of a RESIDENT 4B, with no reload
      -> a second, different lesson held: "one change per cycle"
      -> the gate still denies G:\localaized\src\assistant\daemon.py
      -> report: 8 events, 2 candidates, 1 promoted, overlay 73 of 500 tokens

Suite after: **1167 passed, 3 skipped, 47 subtests** (1164 before, +29). Every
registered tool has an executor, which is worth stating plainly rather than leaving to
be discovered: `test_daemon.py` and `test_confirm.py` both asserted "registered tools
outnumber executable ones", which was a coincidence that has now stopped carrying
information. They register a synthetic unbuildable tool instead, which is the
condition rather than an instance of it.

---

## D72 - Butleroy: a brand kit, and the bug only measuring pixels could find

The name is **Butleroy - LocalAIzed Concierge for Windows**, from the reference term
*butleraized*. The kit is `brand/`: a spec (`README.md`), `tokens.json`, four SVGs
(`mark`, `mark-mono`, `icon`, `logo`), a 27-glyph UI set, and a generated raster set
(3 families x 10 sizes, plus a 9-frame `.ico`).

**The concept.** Butleraized is a *state*, not a service: everything handled, nothing
asked twice. A good butler is invisible - the room is simply ready. So the brand is
about absence of friction, not presence of a servant, and the mark is a **hotel
concierge bell**, low and wide with a heavy rim. A bow tie would have read as costume;
a bell reads as *handled*. Sound waves were drawn and cut: they push the silhouette
outward, which costs legibility at 16 px, and a confident mark does not need to
announce that it makes a noise.

Palette is private-members'-club green-black with brass. It reads as service rather
than as utility, and it sits on the dark UI the project already had.

### The geometry lives in `src/assistant/brand.py`, not in `brand/`

This is the load-bearing decision, and it was nearly got wrong. The obvious layout is
to keep the renderer in `brand/tools/render_icons.py` - and then the tray cannot use
it, because **`pyproject.toml` packages `src/` only**. A tray importing from
`brand/tools/` works from a checkout and fails with `ImportError` for anyone who
actually installed the thing, which is the failure mode INSTALL.md's install-of-record
is supposed to make impossible.

So the geometry moved into the package. `render_icons.py` is now a thin CLI over it,
and `tray.py`'s `_icon_image` renders from it at runtime. One copy of the shape in
code. The tray's previous hand-drawn three-node icon is gone - it was reasoned, not
accidental (`"an .ico in the repo would be an opaque asset nobody can review"`), but
that reasoning is satisfied better by code in the package than by a PNG in a directory
that does not ship.

`import assistant.brand` pulls in **no third-party module**; Pillow is imported inside
the functions, so D1's stdlib-only daemon import survives. Checked in a subprocess,
because an in-process `sys.modules` assertion can only ever pass once pytest has
imported Pillow for something else.

### Three copies of the geometry, and the test that makes three safe

`mark.svg` is a third copy, deliberately: a designer needs a file they can open in a
vector editor, and parsing SVG would mean depending on a rasteriser this project
deliberately does not have (D66). Three copies is only safe because
`test_the_svg_the_tokens_and_the_generator_state_one_geometry` parses the SVG and
compares it to both - it does **not** restate the numbers, because a test comparing a
copy of itself against the file it was copied from keeps passing after either one
changes.

### Four bugs, and every one of them rendered perfectly

This is the part worth recording. **All four bugs in the icon generator produced
output that looked fine and was wrong.** None was found by looking; every one was
found by measuring the rendered pixels.

1. **The highlight rendered as a dark arc, not a light one.** The stroke is 50% alpha.
   `ImageDraw` *overwrites* pixels; it does not blend them. So drawing it straight
   onto the dome punched a semi-transparent hole and the obsidian plate showed
   through - while the identical stroke in `mark.svg`, where `opacity` genuinely
   blends, was light. **The two assets disagreed about the same mark.** Fixed by
   drawing on a separate layer and `alpha_composite`-ing it. This is precisely the
   drift the geometry cross-check *cannot* see, because opacity is not geometry.
   `test_the_highlight_is_lighter_than_the_dome_not_darker` now measures it, and it is
   the **only** one of the 53 brand tests that catches it - reverting the fix leaves the
   other 52 passing.
2. **The bell sat in the top-left corner of the plate.** `alpha_composite(im, (x, y))`
   passes the tuple as `source`, not `dest`. `dest=` is now named.
3. **Then it was clipped off the bottom-right.** `draw_icon` works in supersampled
   space and the mark was sized in final-pixel space, so a 128 px mark landed in a
   1024 px canvas at a final-scale offset - an 11 px bell, measured by finding the brass
   pixels. Both this and the previous one are the same class: two coordinate systems,
   neither named.
4. **`Path.replace()` is a move, not a copy**, so `tray.png` consumed
   `butleroy-tray-32.png` and the set lost its 32 px tray.

A fifth defect was in the *palette*, found by a contrast assertion rather than by
rendering: `rust` was `#C4553F` at **4.24:1** on obsidian, below WCAG AA. Corrected to
`#D9694F` (5.47:1). A colour reserved for *"this is destructive"* cannot be the one
that is hardest to read.

### What the tests are for

An icon set is the easiest thing in a repository to rot, because nothing breaks. So
the assertions are **cross-checks between files**, not checks of any single file:
three copies of one geometry must agree; the committed rasters must be byte-identical
to what the module draws now (`test_the_committed_rasters_are_not_stale`, which catches
both a stale set and a hand-edited PNG); the tray must draw the brand mark, compared as
pixels; every state colour must clear 4.5:1 on both grounds.

Deliberately **no** test asserts a colour *looks* right, nor that the lockup is
optically balanced. Judgement about whether brass reads as brass cannot be automated,
and a test that pretended to would be theatre - the D36 shape.

There is also **no SVG rasteriser in this project** - `cairosvg`, `svglib` and
`magick` are all absent, and adding one would breach D66 for a build step. So the
SVG assets cannot be measured at all, only parsed. This bounds what is checkable:
every statement above about the *rasters* is measured on real rendered pixels,
while the SVGs are only checked for internal consistency. One test that tried to
assert the lockup's optical balance was written and then deleted, because it
would have been asserting a number invented to match a file nobody could render.

What *is* checked is that the palette is internally consistent, legible on the
grounds it is used on, and not defined twice with different values.

Five mutations were run against the suite to prove the new tests bite. Each was caught
by exactly the test meant to catch it, and by nothing else.

Suite after: **1221 passed, 3 skipped, 47 subtests** (1167 before, +54 in
`test_brand.py`).

A correction, because the first version of this paragraph said 1217 and +50. That
figure was the count from the *start* of the session, before the `brand.py`
refactor and before the last four tests — so it was a number I had measured once,
written down twice, and let drift. The error is exactly this project's recurring
one: a value quoted from memory of an earlier run rather than from the run that
matters. Recorded here rather than quietly corrected, because a decision log whose
numbers are only right when nothing moved is worth nothing.

---

## D73 - Butleroy everywhere a user can type it, and not on the filesystem

The product is renamed from `locala` to **Butleroy** in every place a user can
reach: the package, the console script, the four named pipes, the pystray
identity, and the two credential variables. Fifteen new tests in
`tests/test_rename.py`.

### The part that is easy to get wrong

**A product name and a filesystem path are not the same string.** The project
lives in `G:\localaized`, and that path appears in `policy.SELF_PATHS` (which is
what stops the assistant editing its own code), in the model paths, and in
roughly fifteen tests. A blanket `locala` -> `butleroy` would repoint all of them
at a directory that does not exist.

Worse, it would have **looked correct**. Most of those tests assert a string
against another string, so renaming the constant renames both sides and they stay
green - while the thing they were protecting stops being protected. So:

- `policy.SELF_PATHS` still contains `G:\localaized`.
- `test_the_project_directory_is_still_called_localaized` asserts that a
  `SELF_PATH` resolves inside the actual checkout, and that `DECISIONS.md`,
  `ARCHITECTURE.md` and `PLAN.md` are each still covered. This is the test that
  would have caught it, and mutating the path to `G:\butleroy` fails exactly it.
- `test_windows_environment_variables_are_untouched` exists because
  **`LOCALAPPDATA` is Microsoft's**. It matches the rename pattern, sits next to
  project code, and a global replace takes it without asking. Renaming it would
  be catastrophic and silent. Mutating it fails exactly that test.

### A rename that breaks a credential is silent

`LOCALAIZED_EMAIL_PASSWORD` and `LOCALAIZED_TELEGRAM_TOKEN` were real, documented,
and quite possibly set on the machine that is running this. Renaming them without
a fallback gives a user an empty inbox and an unresponsive Telegram bridge, with
no error anywhere - which is the D70 shape in a different medium.

Both are kept working. New name first, legacy second, `DeprecationWarning` when
the legacy one is the source. The helper returns **the name it used** as well as
the value, so `token_source` / `password_source` report the spelling actually in
use - otherwise a user reading `mail_status` sees `BUTLEROY_EMAIL_PASSWORD` while
their machine is set up with the old one, and "fixes" it twice.

Two details that were only found by writing the tests:

- **Both names must be cleared in a test that means "no credential is set".**
  Three such tests deleted only the old variable, so after the rename the *new*
  one could leak in from the developer's own environment. Fixed by deleting both.
- **The new variable must win silently.** Warning when both are set would train
  the user to ignore the warning, which is how a deprecation stops being noticed.

### Two defects the rename tests found in existing tests

1. `test_a_token_from_the_environment_is_reported_as_such` asserted the literal
   `"LOCALAIZED_TELEGRAM_TOKEN"`. It therefore **kept passing against the old
   variable while the code had already moved on** - green, and wrong, which is the
   worst combination. Now compared against the constant, so it asserts *which
   variable supplied the token* rather than *which string the file once said*.
2. Three `delenv` sites, above.

Both were caught by running the full suite, which is the only reason to run it
after a rename: the change is mechanical, so the failures are not where you were
looking.

### What the rename is not

The **install is not the package name**. `uv pip install -e .` produced
`butleroy-0.3.0.dist-info` and `butleroy.exe`; verified by running
`butleroy.exe --help`, not by reading the entry point. `uv` left the old
`locala-0.3.0.dist-info` behind, which is harmless (importlib resolves the new
name) and is now reported rather than asserted against - a test that fails
because a package manager left a file is a test that will be "fixed" by deleting
evidence.

Suite after: **1236 passed, 3 skipped, 47 subtests** (1221 before, +15).

---

## D74 - Model configuration, and a promise that nothing here can stop the daemon

`state/models.json` is the file that makes the models surface possible. Before it,
the two tiers were literals at `brain_server.py:43-72` and the only thing that could
change one was a programmer with a text editor.

Four properties, each of which was a bug somewhere in this project's history:

1. **A missing file is not an error.** A machine that has never opened a settings
   window behaves exactly as it did before. `load()` returns the built-in defaults
   with `provenance="defaults"`.
2. **A corrupt file is not an error either, but it is *visible*.** `load()` degrades
   to the defaults and puts the reason in `provenance`, which reaches the window and
   becomes a sentence on screen. A settings window that silently discards what the
   user typed is worse than one that refuses to save, because the user believes it
   took. Nine malformed shapes are tested, each asserting the *reason* survives and
   not merely that the defaults came back.
3. **A model that is not on disk is refused at save time, not at load time.** By the
   time a load fails the user has a lease outstanding and a turn in flight. So the
   existence check is in `save()`, and it is what lets the window refuse the save
   while the user is still looking at the form.
4. **Per-tier degradation.** One bad tier falls back to itself; it does not cost the
   user the other one. Wholesale fallback is the easy implementation and the wrong
   one — a typo in `deep` silently resetting `fast` changes the user's model choice
   for a reason they cannot see.

Three more, from the code that exists:

- **The stored model is a filename relative to `models/`.** Never an absolute path.
  A config that hard-codes `G:\localaized` breaks when the tree moves, and D73
  established the tree may. A test asserts no `G:` appears in the written file.
- **`mmproj-*` is never offered as a model.** It is projector weights; picking it
  gives a child that leases several gigabytes, loads successfully, and then cannot
  speak, which is much harder to diagnose than a missing row.
- **The write is atomic** — temp file plus `replace()`. `replace()` rather than
  `rename()`, because on Windows `rename()` fails if the destination exists and this
  file always exists after the first save. A test asserts no `.tmp` is left behind,
  because a half-written `models.json` is one of the unreadable states `load()` has
  to degrade from, and not producing it is cheaper than recovering from it.

**D1 is why this is a separate module.** It is read by the daemon, so it imports
nothing but the standard library at module level, and the check is in a subprocess
because an in-process `sys.modules` assertion can only pass once pytest has already
imported `llama_cpp` for something else.

**The defaults are duplicated from `brain_server.TIERS` on purpose**, because
importing that module can pull a model runtime in. The cost is two sets of numbers,
and `test_the_defaults_match_the_literals_the_child_already_had` is what makes the
cost acceptable — it *imports* `brain_server` rather than restating the filenames,
which is the whole point. A restated copy compares a copy of itself with the file it
came from and passes forever after either changes.

Six mutations run against the module. Each was caught by exactly the intended test:
the traversal filter, the corrupt-file fallback, per-tier degradation, the save-time
existence check, in-place mutation in `to_brain_tiers`, and an absolute path leaking
into the written file.

**This section was missing.** `modelconfig.py` opened by citing "D74" as its own
provenance, and no D74 existed — a source file pointing at a decision that was never
written, which is a reader sent to look for something that does not exist. It was
found by `tests/test_decisions_log.py`, which asserts contiguity and that no source
file cites a decision number absent from the log. HANDOFF.md had been claiming
"contiguous, none missing" by hand, and nothing had ever checked it.

---

## D75 - A settings window, and the three bugs that only a real Tk found

Option A, as chosen: a **second window**, not tabs on the chat window. The chat window
is a transcript - append-only, one-directional, sized for reading. Configuration is
the opposite. One window means two scrollbars fighting and every settings bug becomes
a risk to the window D67 and D68 just made work.

Three files:

| File | What |
|---|---|
| `src/assistant/modelconfig.py` | which model answers which tier, in a file |
| `src/assistant/settings.py` | the window; the Models section |
| `src/assistant/daemon.py` | `model_config` / `model_config_set` ops |

The tray gets a "Settings..." item and owns the window, closing it with the overlay.

### The tiers were literals, and that was the actual blocker

`FAST_MODEL` and `TIERS` were Python literals at `brain_server.py:43-72`. Nothing
anywhere could edit them. They stay as the built-in default - a machine with no
config must still have two tiers - but `resolve_tier` now reads `state/models.json`
over the top.

The file stores a **filename relative to `models/`**, never an absolute path, and
`tests/test_model_config.py` asserts no `G:` appears in the written file. A config
that hard-codes the checkout path is a config that breaks when the tree moves, and
D73 established that the tree may.

### These are ops, not tools, and that is the safety property

If model configuration were a `system.*` tool then `brain.turn` could *propose* to
change which model it is, and `tool_call` would execute it through the gate. That is
D21's exact prohibition - "the assistant never rewrites its own code or policy" -
in a new medium. As ops they are reachable only over the pipe by a process holding
the authkey, which is the local user. `test_model_config_is_not_a_tool_the_assistant_may_propose`
asserts the registry stays closed against it.

The write is still audited, at T1. Reversible, and "the human changed the
configuration" is the first question anyone asks after something odd happens.

`model_config_set` takes **the whole table and refuses a partial one**. A patch would
delete a tier the window had not rendered, and the failure would surface much later
as "unknown tier deep" on a request about something the user never touched.

### Three bugs, all found by running the real thing

**D69's exit code, reproduced.** The first `settings.py` run made pytest exit
`-2147483645`: Tcl's async handler deleted by the wrong thread. `_release_widgets`
cleared `self._sections` but I had also stored `self._head`, `self._title_label`, and
each section's own `listbox`/`detail`/`status` - all widget references outliving the
root. Exactly D69, in the module written to prevent it. `_WIDGET_ATTRS` is now data,
and a test asserts the live instance against it so a fifth widget attribute cannot be
added and forgotten.

I then nearly mis-diagnosed it: a bare `Tk()` on a worker thread aborts at exit even
with no leak of mine, which made it look environmental. It was not - a baseline that
held the root in a module-level dict reproduced the same abort with *no* code of mine
involved, and dropping the reference made it clean. The leak was real; my first
"evidence" for it was a probe artefact. Measuring the baseline rather than arguing
about it is what separated the two.

**Save had never worked.** `_save` passed the tiers as a positional argument, and
`Tray.call` is `call(op, /, **args)` - keyword-only. Every save raised `TypeError`,
which the method's own `except Exception` turned into "Could not reach the daemon".
So the window reported a *connection failure* for a bug in the window, and had never
once been able to save. Found because the test asserts the daemon **was reached**,
rather than reading the status line - which is D68's lesson: assert on the effect, not
on what the UI says about the effect.

**Save reported nothing.** `_save` set "Saved" and then reloaded, and the reload
overwrote the status with the provenance line. The user pressed Save and no pixel
changed - the precise failure this method's own docstring says is worse than a
refusal. Reload now happens first.

### What is not built yet

One section. Voices, speech, workspaces, authority and advanced configuration are all
still in `brand/README.md` as specifications, and there is no code for them. The
`Section` contract is small on purpose - build widgets into `parent`, keep references
in `self.widgets` and `self.named` - so the next five are the same shape and one new
file each.

### The Tk tests run in a subprocess, and why that is not a workaround

`tests/test_settings_real.py` is run by `tests/test_settings_isolated.py`, in a
child process. The tests themselves are ordinary; the containment is about the
*process exit code*.

In this Python 3.11.14 / Tk build, a `Tk()` root created and destroyed on a worker
thread can leave Tcl's async handler to be deleted at interpreter shutdown — on the
main thread. Windows reports that as a debug break, so the message is

    Tcl_AsyncDelete: async handler deleted by the wrong thread

and the process exits `-2147483645` **after every test in the run has passed**. The
first two symptoms are identical, which is the whole problem.

What it was *not*, each checked rather than assumed:

  * **not a leak.** `test_every_widget_attribute_is_released` and
    `test_a_section_holds_no_widget_after_disposal` assert nothing is retained, and
    they pass. The first version of this window *did* leak — it kept `_head` and
    `_title_label` — and produced the same exit code, so the two had to be separated
    rather than assumed to be one thing.
  * **not a number of windows.** Bare tkinter created and destroyed forty sequential
    worker-thread roots in the same interpreter without complaint. The count at
    which the settings window tripped it was not stable between runs, which is the
    opposite of a limit.
  * **not the tests.** Driving the same code from a plain script, with no pytest at
    all, exits 0.
  * **not shared state.** A shared module-scoped window was tried and made things
    *worse*: a shared fixture must guarantee its own preconditions, and the symptom
    was `KeyError: 'models'` in whichever test ran next — an error naming the fixture
    rather than the cause. One window per test is boring and deterministic.

So it is an interpreter-lifetime quirk of this build, it is not the product's, and
`settings.py` should not be contorted to avoid it. What it must not do is turn a
green run red with no summary, which is what in-process execution does. Hence the
subprocess: a real failure fails there, with the child's own output, and a Tcl abort
is visible as a non-zero exit rather than as a mystery number.

Two smaller findings from the same session, both about hiding the cause of a failure:

  * **`_tk_main` swallowed its exception.** It set `_root_ready` and discarded the
    error, which is why a window that failed to build presented as an unexplained
    ten-second `_invoke` timeout in an unrelated test. It now records the error and
    `show()` raises with the reason.
  * **`_release_widgets` collects on the Tk thread.** Nulling the attributes is
    necessary but not sufficient: the `after` callbacks and the event queue leave
    reference cycles, and a cycle holding the Tcl interpreter is only broken by the
    cyclic collector. Left to process shutdown, the interpreter dies on the wrong
    thread. One line, and it is in the right place rather than in a test.

### A test that reads the UI's own report about itself

`test_a_successful_save_is_not_overwritten_by_its_own_reload` exists because the
first version of that test only asserted the word "Saved" appeared. It would have
passed on the broken build, where Save reported success and then immediately
overwrote the message — because the reload's provenance line can also contain the
word. The assertion had to be about the *ordering* of two messages, not about
whether one of them was present. This is D68's lesson one level down: assert on the
effect, not on what the UI says about the effect.

### The decision log is now checked, because it was not

`tests/test_decisions_log.py` exists because `modelconfig.py` cited "D74" as its own
provenance and no D74 existed. HANDOFF.md had been claiming `decisions D1..D73,
contiguous, none missing` by hand, and nothing had ever verified it — the same shape
as the stale test count in D73, one layer over: a figure in a document is a claim
about a moment, and nothing tells you when the moment passed.

Its first version asserted ascending decision order and failed on D29. The failure
was correct and the assertion was wrong: the log is organised into **Parts**, D29
opens Part IV, and it refers back to D25 at the end of Part III. Moving D29 out of
the section it introduces, or deleting a test that had found something real, would
both have been worse than recording why total order is not a property this document
has.

Its regex was also wrong twice, and the second failure is the interesting one: it
matched only `##` headings and only a hyphen, so it found 31 of 75 decisions and
reported D1 through D44 as dangling citations — while **passing** the citation
test, because the known set was nearly empty and so nothing looked missing. A check
that is too narrow does not fail loudly; it fails confidently, about the wrong thing,
and a green test is the most reassuring possible wrong answer.

Suite after: **1294 passed, 22 skipped, 47 subtests**, exit 0 (1236 before).
Nineteen of the 22 skips are `test_settings_real.py`, which is executed by
`test_settings_isolated.py` in a subprocess and passes there; the other three are the
speech slow tests. New tests: 29 for the config module, 21 for the ops, 19 for the
window (run out of process), 6 for the decision log, and 2 for the isolation itself.

---

---

## D76 - Voices, and the one op that reads a path you chose

The voices surface: `src/assistant/voiceconfig.py`, five ops, and a second section in
the settings window. `speech.py` had three voices in a `VOICE_PATHS` dict and a
`DEFAULT_VOICE` constant - the same shape the models had before D74, and the same
consequence: nothing could be chosen and nothing could be added.

### Three sources of truth, and why discovery rather than a registry

A voice can arrive three ways, and all three have to be visible:

  1. **Known** - the piper voices in `speech.VOICE_PATHS`, downloadable on demand.
     `speech.py` stays the authority on where each one lives in the Hugging Face repo,
     because that nesting is not uniform and the comment there records that the first
     version assumed it was and got a confident 404.
  2. **Installed** - a `.onnx` and `.onnx.json` pair in `models/voices/`, found by
     **reading the directory**. That is how an imported voice appears, and how one
     dropped in by hand appears, without being registered anywhere.
  3. **Chosen** - the default, in `state/voices.json`.

Reading the directory rather than a manifest is D74's argument again: a manifest is a
second place for the truth to live, and the copy that goes stale is the manifest.

The pair is matched **by suffix, not by stem**: `foo.onnx` goes with
`foo.onnx.json`. The first version used `Path.with_suffix`, which looks for
`foo.json`; every voice then read as incomplete, and it presented as a download
problem rather than a naming one.

### A voice that is not downloaded may still be chosen

Deliberately unlike the model config, where a missing model is refused. By the time a
model load fails the user has a lease outstanding and a turn in flight. Choosing a
voice you have not downloaded is the *normal* state on a fresh machine, and it is how
you say "this is the one I want" before spending 63 MB. Refusing would make the
download button unreachable. `provenance` says `not downloaded yet` instead, so the
window has something to warn with.

That check was wrong on the first pass in a way worth recording: it tested
membership of the catalogue, and every *known* voice is in the catalogue whether or
not it is on disk - so a chosen voice that had never been downloaded reported a clean
`file` and the window had nothing to say. The question is whether it is **here**, not
whether it is known.

### `voice_import` reads a path, and that is a real difference

Every other op in this project names a file inside a directory it owns. This one
takes a path the user picked in a Tk file dialog. That deserves more than "it is only
an op".

**What protects it is reachability, not a path check.** Ops are not tools, so
`brain.turn` cannot propose one. A model that could call `voice_import` would have an
arbitrary-file-read with no gate in front of it - D59's second door exactly. Only a
process holding the authkey can call it, and that is the local user.

On top of that: the source must be a file and end in `.onnx`; the matching
`.onnx.json` must exist beside it, because a lone model is a chipmunk-voiced
half-voice; the destination name is checked against the same `_SAFE_NAME` pattern the
model config uses, so the copy cannot escape `models/voices/`; the copy goes to a
`.part` name and renames; and the whole thing is audited **with the source path**,
because "where did this voice come from" is the question asked after something sounds
wrong, and an unrecorded import is an unanswerable one.

**Considered and rejected: routing the copy through the gated `file.copy` tool.** It
would put a T3 confirmation in front of "add my voice" - a dialog asking the user to
confirm the thing they just asked for, on their own machine, in their own settings
window. D60 already records where that leads: a confirmation that cannot be
meaningfully refused trains people to click through confirmations.

### Seven mutations, and one of them was refused by a guard that predates this work

Each mutation was caught by the intended test, and the mutation was **verified to have
actually changed the file** - a lesson from D73, where two attempts silently did not
apply and "passed".

The interesting one is registering `voice.import` as a tool. It does not fail a test;
it fails **at import time**, from the daemon's own table check:

    RuntimeError: executors with no registry entry: ['voice.import']
                  - these would run without a tier, which D5 does not permit

So the D21 boundary is not merely asserted by a test in this file. The daemon refuses
to start if an executor has no registered tool, which means the mistake cannot be made
by accident at all. That is what the table check was for, and this is the first time
something has tried to violate it.

Suite after: **1335 passed, 22 skipped, 47 subtests**, exit 0 (1294 before).
41 new tests in `tests/test_voice_config.py`. Seven mutations, each caught by
the intended test, each verified to have actually changed the file.

---

## D77 - Four of the tray's six menu items had never worked

Building the Workspaces section surfaced this, and it is the most serious defect found
this session. It is not in the new code; it has been there since the tray was written.

`tray.py` called four ops by names that do not exist:

| Menu item | Called | The op is |
|---|---|---|
| Summon | `session.set_attended` | `session_set_attended` |
| Workspaces | `workspace.list` | `workspace_list` |
| VRAM leases | `arbiter.summary` | `arbiter_summary` |
| Verify audit chain | `audit.verify` | `audit_verify` |

There was a **fifth**, found later and by a different route: `Presence` in the same
file builds `ipc.Request(op="session.set_attended", ...)` directly rather than going
through `Tray.call`, so it was not in the table above and the first version of the
op-name check - which scanned for `.call("...")` - reported the file clean. The suite
caught it only because `test_presence_marks_attended_when_someone_is_typing` asserted
the op it had recorded, and that assertion had been written against the wrong name
too, so fixing the name is what surfaced it. The check now scans both spellings, and
`test_the_check_would_notice_a_wrong_name` feeds it the `ipc.Request(op=...)` form
specifically so that widening cannot silently regress.

Ops are named after the methods with `op_` stripped, so they are underscored. Every
one of those four returned `unknown op 'workspace.list'`, and the tray's own `notify`
turned it into a toast - so the user saw a message about an unknown op and reasonably
concluded something transient had gone wrong. **The daemon's `handle` even returns
`known_ops` in that reply**, and nothing read it.

### Why no test caught it, which is the part worth keeping

`test_tray.py` drives the menu against a **fake daemon**, and a fake that answers any
string answers the wrong string too. The failure was not in the code the fake
replaced - it was in the *name*, and a name is precisely what a fake does not check.

This is D69's sentence again ("a fake agrees with the code about everything it does
not check") and D70's ("the fake did its own LOGIN"), one level up. The fake did the
**op lookup**, which is the daemon's job, and the daemon is the only thing that can
say whether a name exists.

### The fix is a check, not four edits

Four edits fix four menu items. `tests/test_ui_op_names.py` fixes the class: it
extracts every string a UI module passes to `call(...)` and asserts each is in
`Daemon._OPS`, the table the daemon actually dispatches on.

The generalisable rule: **any string a caller sends that a different module resolves
should be asserted against that module's own table, not against a stand-in.** The
stand-in cannot know. It also asserts the direction that cannot currently fail -
every entry in `_OPS` is callable - because that is the invariant which makes the
table worth trusting and it is one refactor away from not holding.

It reports *all* offenders rather than the first, because these arrive in batches:
four were wrong at once, and stopping at the first turns one fix into four round
trips.

### A too-narrow check, for the fourth and fifth time this session

The count of "ops with no UI" was wrong twice before it was right - first because it
matched `_call(` and missed `call(`, then because it matched exact op keys and missed
the dotted spelling the callers use. The second wrong version made the tray menu look
like five stubs when all five were calling the daemon. That wrong answer is what led
to reading the tray's methods, which is how the four broken names were found.

So: **a check that under-matches does not fail, it reports.** Four times now - the
decisions-log regex twice, this count twice. The lesson is not "write better regexes".
It is that a negative result from a check you wrote yourself is a claim about the
check as much as about the code, and the cheap thing to do is verify the check against
a case you know the answer to. The op-name check itself then missed the fifth broken
name, one paragraph after being written to catch exactly that - which is the argument
for the guard-on-the-guard test rather than an argument against writing the check. `test_the_check_would_notice_a_wrong_name` does that
for the op-name test, and the other two have been replaced by checks whose
under-matching would fail a different assertion.

### Workspaces, the section that found it

The most important surface in the window, because nothing outside `models/` is
reachable until a root is approved here: a manifest is the *only* source of
"approved" that `policy.Gate` has.

`workspace_list` returns a list of **strings** formatted for a person, and
`workspace_roots` is the op for machines - its own docstring says so. The first
version of the section filtered the list for dicts, discarded every row, and left the
screen saying "No workspace approved" while a workspace demonstrably existed. A
plausible and completely wrong answer, which is the worst kind. The section now counts
rows from the human string and takes every fact that decides anything from
`workspace_roots`, and it does not parse the prose for anything but a name.

The screen cannot widen authority, and the code makes sure of it rather than a
comment: `allow_tiers` is always intersected with the session ceiling at resolve time,
and `DangerLevel.TIER_CEILING` caps what a manifest may ask for at all - `high` danger
permits no more than T1. The pickers can only narrow.

Suite after D77: **1343 passed, 27 skipped, 47 subtests**, exit 0 (1335 before). Eight
more: five for the workspaces section, four for the op-name check (one of which is the
guard on the guard), and two tray tests whose assertions had been pinning the broken
names.

---

## D78 - Authority, and a screen that can only ever narrow

The fourth section: the tier ladder, the session ceiling, the attended state, what is
waiting on the user, and a "what would happen if" checker.

The reason it exists is that the whole tier model was **invisible**. Tiers, the
ceiling and the attended flag lived entirely inside the daemon: a user could be
refused at T3 and have no way to see why, or to see what the ceiling even was. D13's
authority model is only worth anything if the person it protects can see it.

### Two things the section deliberately cannot do

**It cannot approve a confirmation.** `confirm_list` is read to *show* what is
pending, and the section then says where to answer it - the prompt window. Two windows
able to answer the same dialog means one of them is the one the user was not looking
at, which is the same argument `tray.py` makes about its own window.

**It cannot claim presence.** The one button narrows: "Step back (unattended)". Going
the other way is the presence detector's job, and a button that asserted the user was
present would be a button that lies about the one fact the entire tier model rests on.
The daemon refuses it anyway - `session_set_attended` may only narrow - so a button
offering it would be a button that always fails. The test asserts the button is
disabled when already unattended, which is the shape that makes the claim checkable:
an enabled button in that state can only exist to go the other way.

**It cannot execute.** The checker calls `tool_check`, which predicts. A `tool_call`
from this window would make it a second way to run something, which is D59's defect -
and the test asserts `tool_call` never appears among the ops the section called.

### The ladder is shown as examples, not as numbers

"T2" means nothing to someone who has not read this file. "move, archive, bulk rename,
commit, build" does. So the four tiers are rendered with their examples, and the test
asserts the examples are on screen - because a ladder of bare tier names would pass a
test that only looked for "T0" through "T3".

### The coverage figure, corrected twice and left visible

The count of "ops with no UI" has been written into `brand/README.md` three times:
21, then 20, now 21 again. Every time it was written from the change just made rather
than from the measurement. It is left visible in the document rather than tidied,
because the whole section is about exactly that failure - a number that is a claim
about a moment, with nothing to tell you when the moment passed.

Reachable now: **21 of 44**. `settings.py` reaches 15, `overlay.py` 6, `tray.py` 5.

Suite after: **1343 passed, 36 skipped, 47 subtests**, exit 0. Nine new tests in the
window body (33 there now), one of which was wrong on the first attempt: it walked the
widget tree one level deep to find the tier examples, found nothing, and failed for a
reason that had nothing to do with the rendering. Made recursive.

---

## D79 - Advanced: the facts that had no home, and a formatter

The fifth section, and the last one that was specified. Read-only diagnostics: which
model is loaded, whether mail is configured, whether the vault bridge is up, what the
assistant has promoted, where everything lives, and the audit tail.

**Read-only on purpose.** Every *setting* has a screen where it can be changed with
its consequences next to it. This is the one place that answers "what is the state of
things", and a panel that mixed the two would be a panel where a stray click changes
something. The one action is Reload, and a test asserts the mutating ops never appear
among the ops this section calls - not merely that the text widget is disabled, which
a future edit could leave looking right.

It reached six ops that had no way in at all: `brain_status`, `mail_status`,
`vault_status`, `reflect_overlay`, `audit_tail` and a fuller reading of `status`.
Coverage goes from 21 of 44 to **26 of 44**.

### One op failing must not lose the other five

The vault is not installed on every machine, and a diagnostics panel that dies because
one subsystem is missing is useless exactly when it is most wanted. Each fact is asked
for independently through a helper that converts any exception into a line. That is
not defensive habit: `vault_status` on a machine with no vault is the *normal* case,
and the panel's whole job is to report it.

### The formatter, and what found it

The first version interpolated values straight into the text, so a reply missing a
field rendered `version          None`. The test asserting "no raw `None` reaches the
screen" caught it - and it caught it on the *fake*, which returned an empty `status`.
That is the interesting part: the fake was unrealistic and the test found a real
rendering defect anyway, because the assertion was about the screen rather than about
the fake.

A raw `None` in a diagnostics panel is worse than a wrong value. The panel exists to
be trusted when something *is* wrong, and a Python repr makes it look like the window
is broken rather than the subsystem. `_fmt` renders `None` as "unknown" and booleans
as yes/no.

Two smaller display defects found the same way, both from reading real output rather
than reasoning about it:

  * the audit record field is `timestamp`, not `at` - so every row rendered with a
    blank time, which looked like missing data rather than a misread field;
  * `idle_s` is `None` before any child has run, and rendered as "Nones of 90.0s".

### And a refusal worth keeping

`workspace_add` refused the temporary directory the probe was using, with:

    NotPermitted: p: root '...' contains the assistant's own state directory
    '...\\state\\workspaces'. Refusing, because that would make our own code
    reachable as an approved workspace.

That is D21 working without being asked, on a case nobody had thought about: the
manifest store protects itself against being made reachable by approving its own
parent. Worth recording because it is the only reason the probe's first assertion
failed and the second passed.

Suite after: **1343 passed, 43 skipped, 47 subtests**, exit 0. Six new tests in the
window body (40 there now).
