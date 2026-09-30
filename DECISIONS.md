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

### D2 — Embedded inference from prebuilt CUDA wheels
Use `llama-cpp-python` from the `abetlen` prebuilt index (`win_amd64`, CUDA **cu124**,
sm_86 covers the 3060). Available through **v0.3.35**; no MSVC build required.
Fallback if unavailable: source build using VS BuildTools + `vcvars64`.
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

### D9 — Uncensored models, load compatibility unproven
Candidates verified to exist on Hugging Face as GGUF with paired `mmproj`:
`DavidAU/Qwen3.5-9B-The-Defiant-Fable-Uncensored-Heretic-NEO-IMATRIX-MAX-MTP-GGUF` (885 likes,
1.85 M downloads), `mradermacher/Huihui-Qwen3.5-9B-abliterated-GGUF`,
`mradermacher/Qwen3.8-9B-heretic-uncensored-GGUF`.
**Open risk:** a model newer than the bundled llama.cpp may fail to load due to an unknown
architecture. This is why Step 0 exists and why it is a hard gate.

Model ladder, ordered by VRAM headroom:
| Quant | ~VRAM | Verdict |
|---|---|---|
| 9B IQ4_XS / Q4_K_S | 5.2–5.5 GB | **Safest.** ~1.5 GB headroom |
| 9B Q4_K_M | 5.7–6.2 GB | Fits only if audio stays on CPU |
| 9B Q5_K_M | 6.6–7.0 GB | **REJECTED** — violates D1 |

All figures must be **measured** with `nvidia-smi`, never estimated.

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

| Requirement | Verdict on one RTX 3060 + 9B |
|---|---|
| Knowledge of the system | **Strong.** Mostly tool calls, not intelligence (D15). |
| Tech support / maintenance | **Strong**, for the same reason. Diagnosis = read state + reason. |
| Administration (files, mail) | **Strong.** Deterministic code executes; the model selects the tool. |
| Pedagogy | **Strong and cheap.** Small models teach well with tight retrieval and Socratic prompting. |
| Coding | **Weak.** Competent single-file work, explanation, review. Multi-file refactors of a large codebase will be wrong. Not to run unsupervised on production code. |
| Orchestrating real projects | **Not as the thinker.** A 9B cannot plan a 113 KB-scale project well. |

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