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

### D6 — Native tool-calling, grammar only as a backstop
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
