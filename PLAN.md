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

### Phase 0 results — 2026-09-30 — **GATE NOT PASSED**

| Question | Verdict | Evidence |
|---|---|---|
| Does llama.cpp understand Qwen3.5? | **YES** | `arch = qwen35`, 24 layers, hybrid SSM tensors present. **D9's open risk is closed.** |
| Does the model fully offload to the GPU? | **YES** | `offloaded 25/25 layers to GPU`, 526.50 MiB on CUDA0, 11255 MiB free at load |
| Can this runtime actually run on this CPU? | **NO** | `0xc000001d` `STATUS_ILLEGAL_INSTRUCTION` at context construction, on CPU *and* CUDA |

The third question is the gate, and it fails. `phase0_gate.py` exits 1.

**Root cause, proven rather than assumed (D25).** The `abetlen` prebuilt wheel ships a
`ggml-cpu.dll` compiled with **AVX-512**, which a Ryzen 5 5600G (Zen 3) cannot execute. 9,786
EVEX-prefixed instruction sites in that DLL against 17 in a control DLL. A `n_gpu_layers=0` run
fails identically, so CUDA is not involved.

**Consequence:** D2's "no MSVC build required" was wrong. The prebuilt wheels are unusable on this
machine. We build `llama-cpp-python` from source with a portable baseline
(`GGML_NATIVE=OFF`, `GGML_AVX512=OFF`, single CUDA arch `86`) — MSVC 14.51.36231 is installed in
VS 18 BuildTools, so the build is possible.

Two side findings worth keeping: the CUDA **runtime** must be on PATH or `import llama_cpp` fails
outright (D26), and Qwen3.5's **hybrid SSM architecture invalidates every closed-form KV-cache
estimate in these documents** (D27) — VRAM must be measured from here on, which promotes Phase 1's
context-length measurement to a load-bearing gate.

**All timing numbers taken during Phase 0 are invalid** (D28): two `uv` processes belonging to
`F:\Wan2GPT` were saturating the network and all 6 cores throughout.

### Phase 0b — Build a runtime this CPU can execute — **PASSED**

The one blocking task. Nothing else can be honestly planned until inference works at all.

1. Invoke the build from a `vcvars64.bat` shell (VS 18 BuildTools, MSVC 14.51.36231).
2. `CMAKE_CUDA_ARCHITECTURES=86` only — the wheel compiles eight architectures, we need one. This
   is the biggest single lever on a 6-core build.
3. `GGML_NATIVE=OFF` and `GGML_AVX512=OFF` are the fix; `GGML_AVX2=ON` keeps the speed Zen 3 earns.
4. Re-run `phase0_gate.py` **verbatim**. An unchanged gate script is what makes the second run
   comparable to the first.

**Gate: `phase0_gate.py` exits 0** — real text generated, N/N layers on GPU, VRAM delta measured,
GPU utilisation observed.

#### Phase 0b results

Built in **20 min 29 s** at `-j3`, exit code 0, replacing the prebuilt wheel with a source build.

| Evidence | Result |
|---|---|
| EVEX (AVX-512) sites in the new `ggml-cpu.dll` | **10 of 984 `0x62` bytes = 1.02 %** — was 9,786 (76.7 %) in the wheel, and *lower* than the 2.74 % of a control DLL known to be AVX-512-free |
| `ggml-cuda.dll` size | 66 MB, was 819 MB — one CUDA arch instead of nine |
| `llama.cpp` own offload report | **25/25 layers to GPU** |
| VRAM moved on load | **+1154 MiB** (1189 → 2343) |
| Peak GPU utilisation while generating | **21 %** |
| Generation | 28 tokens, 223 tok/s, 99 tok/s prefill — *and the machine was contended* (D28) |
| `phase0_gate.py` | **exit 0** |
| Test suite inside the venv | **106 passed, 40 subtests** |

**Two build attempts were needed, and the first is the useful record.** *Attempt 1* configured CMake
successfully, printed `Found CUDAToolkit ... 13.3.73`, and then died with `No CUDA toolset found`. It
had selected the **Visual Studio** generator, which does not drive `nvcc` itself — it looks for a
CUDA toolset registered *inside the VS installation*. `nvcc` merely being on PATH is not sufficient,
which is exactly why the toolkit was found one line before the failure. Fixed by switching to the
**Ninja** generator, which invokes `nvcc` directly. The VS generator is also multi-config, so it
silently ignored `CMAKE_BUILD_TYPE`; under Ninja an empty build type would have meant an unoptimised
build, so it is now pinned to `Release`. *Attempt 2* compiled cleanly. Logs kept as
`build_phase0b_attempt1.log` and `build_phase0b.log`.

**The gate script itself was corrected before the passing run, and the correction made it stricter.**
It had been asserting on `Llama.n_gpu_layers`, which does not exist in 0.3.35 — the value lives on
`model_params` as `0x7FFFFFFF` for any `-1` request. That is the value we *asked for*, not the value
we *got*, so it could never have caught partial offload. The gate now captures the C-level stderr
(`contextlib.redirect_stderr` cannot see C++ output) and asserts on llama.cpp's own
`offloaded N/N layers to GPU` line, treating a **missing** report as a failure rather than falling
back to something weaker. Separately, `GpuSampler` assigned `self._stop`, shadowing
`threading.Thread._stop`, which CPython calls from inside `join()` — a `TypeError` that only appeared
once the thread was actually joined.

**What Phase 0 established:** CUDA inference works on this machine, llama.cpp 0.3.35 understands
Qwen3.5, and every remaining phase may now be planned on a real foundation. **What it did not:**
nothing about the 4B or the 9B fits the budget — that is Phase 1.

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

### Phase 2 results — **substantially built; the gate is not yet fully closed**

**213 tests green inside the venv.** Built, all pure-stdlib except the tray:

| Component | File | State |
|---|---|---|
| Policy gate (D13) | `policy.py` | done — tiers, path allow/deny, unattended cap, channel rule |
| Hash-chained audit | `audit.py` | done — `verify()` recomputes hash, linkage and contiguity |
| Tool registry (D5) | `tools.py` | done — the full capability surface, gated, mostly unimplemented |
| VRAM arbiter | `arbiter.py` | done — leases, 90 s TTL, self-preemption only |
| Named-pipe transport | `ipc.py` | done — no TCP anywhere, enforced by an AST check |
| Workspace manifests (D10) | `manifest.py` | done — **no discovery method exists at all** |
| Obsidian bridge (D14) | `obsidian.py` | done — 426 real notes; 7.5 GB corpus excluded |
| Daemon | `daemon.py` | done — 24 ops, **3 executors** (`file.list/read/create`) |
| Tray + hotkey + presence | `tray.py` | done |

**Two design decisions worth recording, because both are cases where the obvious choice was wrong:**

1. **The hotkey is Win32 `RegisterHotKey` via ctypes, not the `keyboard` library.** `keyboard` is
   installed and Phase 5 will use it for the wake word, but not for this: it requires administrator
   rights (silent elevation is out of scope), it installs a low-level hook so a 24/7 daemon observes
   every keystroke always, and it is a dependency for a single integer. A process that can only see
   one specific chord has a far smaller blast radius than one that can see everything.
2. **Presence is read from the OS (`GetLastInputInfo`), never claimed over the transport.** Asking the
   caller "are you there?" would be worthless — whoever sends the request could just say yes. The
   daemon's `session.set_attended` may only ever be *narrowed* by a remote channel, and only
   `CHANNEL_PC` may assert it.

**What is not done, and why the gate is not closed:** the **prompt overlay is not built** — the tray's
"Open prompt" item says so plainly rather than faking it, because a menu item that appears to work
and shows stale information is worse than one that admits the feature does not exist. The
**Telegram bridge is not built**. Only **3 of ~58 registered tools have executors**, by design: the
gap is visible in `status` rather than hidden behind a stub that returns success. So the vertical
slice — hotkey → prompt → streamed answer → execute, with no terminal visible — is still outstanding,
and it is the real Phase 2 gate.

**Bugs found in my own code along the way**, recorded because each was invisible until tested
against the real thing rather than a mock:

- The **policy gate failed open with zero approved roots** — a fresh install, the state in which the
  user has approved *nothing*, was the single most permissive configuration possible. Now empty
  denies.
- The **authkey was written in Windows text mode**, translating `0x0A` to `0x0D 0x0A`. Measured 280
  corruptions in 2000 round-trips; ~1 daemon start in 8 would have failed to authenticate on restart.
- A **dangling symlink escaped the Obsidian write jail**: the guard was
  `if resolved.exists() and resolved.is_symlink()`, and a symlink to a non-existent target reports
  `is_symlink()==True` but `exists()==False`. A dangling symlink is precisely what one plants to
  redirect a write.
- The **presence monitor flooded**: `_held` only updated on success, so a refused transition
  re-attempted every poll — 13 calls in 0.4 s at test speed, which in production is a permanent
  refusal every 2 seconds burying the audit chain.
- The **presence monitor desynced**: `_held` started at `False` while the daemon starts *attended*,
  so a first observation of "nobody present" concluded the state already matched and never spoke.

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
| Qwen3.5 GGUF fails to load on this llama.cpp build | **Resolved** | Phase 0 passed: 25/25 layers on GPU, Qwen3.5 understood |
| Prebuilt wheels are unusable on this CPU | **Resolved** | D25; we build from source. Recorded so nobody reinstalls a wheel and "fixes" a working runtime |
| Delegated workers contend for VRAM | Medium | Arbiter leases; one worker at a time; preemption favours you |
| Model too slow for usable interaction | Medium | Tight context (3-level allocation), measured prefill in Phase 1, TTL keeps the GPU free |
| The 4B fast tier is not actually competent | **Medium** | Phase 1 gates on it, not on the 9B. Discovered in Phase 1, not month three |
| Tier swap latency annoys in practice | Medium | ~5–15 s, only on hard planning turns; measure in Phase 1 and revisit if frequent |
| Assistant over-reaches on a destructive tool | Low | Tiers are code, not prompt (D13); deny-path absolute; unattended cap |
| Scope creep — brief is ~5× v1 plan | **High** | Gates prevent forward motion on unproven ground; each phase is independently shippable |
| Obsidian vault too large to index | **Resolved** | ~430 indexable notes; `datasets/` hard-excluded (D14) |
| "22 GB VRAM available" from a tool | **Resolved** | llm-checker double-counts the 3060; recorded and rejected in D23. One GPU, 12 GB |
| A `uv pip install` of llama-cpp-python silently reverts to a wheel | Medium | Install with `--no-binary :all:`; `probe_isa.py` is the standing smoke test for this CPU (D25) |

---

## Immediate next action

**Phase 1 — the model bake-off**, but the *first* thing to do is finish Phase 2's zero-dependency
surface, because it needs no model and can be finished in a sitting: the Obsidian bridge and the
daemon wiring that ties the policy gate, the arbiter, the audit log and the manifest store together.
Then download candidate 1 (`HauhauCS/Qwen3.5-4B-Uncensored-HauhauCS-Aggressive` Q8_0, 4.17 GB) and
measure it on a quiet machine.

Current state: **Phase 0 passed** (exit 0, foundation proven). **Phase 2 substantially built** — 213
tests green inside the venv, tray/hotkey/presence done, but the **prompt overlay and Telegram bridge
are not built** and only 3 of ~58 registered tools have executors. The Phase 2 vertical slice is
therefore still open.

**In flight:** the 4.17 GB fast-tier model is downloading (`download_fast_tier.ps1`, resumable, with
byte-exact verification against the Hugging Face sizes). It must reach exactly **4,482,402,656** bytes
before Phase 1 touches it — a truncated GGUF loads and then generates nonsense rather than raising.
