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

## Phase 1 — Model bake-off — **FAST TIER PASSED, exit 0**

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

#### Phase 1 results — candidate 1 PASSED (`phase1_measure.py`, exit 0)

```
model   : Qwen3.5-4B-Uncensored-HauhauCS-Aggressive-Q8_0.gguf  (4.17 GiB, byte-exact)
loaded in 2.5s   offload report: 33/33 layers to GPU
vram at 4K ctx   4.92 GiB ours  (file 4.17 + 0.74 overhead) -> 2.08 GiB spare under the 7.0 ceiling
board total      6.91 GiB       (2.00 GiB of that was already the user's own software)
emitted/parsed/right tool/arg present: PASS.  Did not invent the decoy tool: PASS.
```

Three things this changed, all recorded as decisions rather than notes:

1. **D34 — the design is grammar-first, and D6 is superseded.** Under `tool_choice="auto"` the 4B
   **cannot be made to decide** to call a tool: it narrates the call in prose and returns no call, which
   is a silent failure. Pinned, it is perfect. **GBNF-constrained, it commits every time** — including
   with no `tools=` supplied at all, because prose is not a legal token sequence in a call position.
   The production grammar is generated from the live registry, and **the call arrives in `content`, not
   in `tool_calls`**. I had earlier dismissed the source document's GBNF grammar as a toy; that was
   wrong, and the correction is recorded rather than quietly overwritten.
2. **D35 — a VRAM ceiling is about what *we* consume.** The first gate compared the *board total*
   (6.97 GiB) to the ceiling and called it a near-failure. That reading was wrong: 2.00 GiB was already
   the user's own software. The constraint is about what the assistant takes. Every gate from here
   measures the **delta we caused** and also reports the board total, so the distinction survives.
3. **D36 — a silent no-op in a constraint API is the worst failure mode available.** `grammar` nested
   inside `response_format` is **silently ignored**: no error, and the model returns plausible output
   that ignores the constraint entirely. Any mechanism whose job is to constrain output must be
   *proved* active before it is relied on.

**Timing numbers in the run are not results.** CPU load was ~28% with `F:\Wan2GP-Tauri` still building,
so D28 applies: 4.5 s call latency and the tok/s figures are recorded but unusable. The structural
half — offload, footprint, competence — is unaffected by load and stands.

**Candidates 2 and 3 are not yet downloaded.** The 9B deep tier and the gemma-4 wildcard remain, and
the deep tier's 5.96 GB would need the fast tier evicted (they cannot co-reside).

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

**509 tests green inside the venv.** Built, all pure-stdlib except the tray:

| Component | File | State |
|---|---|---|
| Policy gate (D13) | `policy.py` | done — tiers, path allow/deny, unattended cap, channel rule |
| Hash-chained audit | `audit.py` | done — `verify()` recomputes hash, linkage and contiguity |
| Tool registry (D5) | `tools.py` | done — the full capability surface, gated, mostly unimplemented |
| VRAM arbiter | `arbiter.py` | done — leases, 90 s TTL, self-preemption only |
| Named-pipe transport | `ipc.py` | done — no TCP anywhere, enforced by an AST check |
| Workspace manifests (D10) | `manifest.py` | done — **no discovery method exists at all** |
| Obsidian bridge (D14) | `obsidian.py` | done — 426 real notes; 7.5 GB corpus excluded |
| Daemon | `daemon.py` | done — 36 ops auto-installed from `op_*`; **16 file/system executors** (8 file, 8 system) plus quarantine `file.delete`/`file.restore` |
| Tray + hotkey + presence | `tray.py` | done |
| Overlay (tkinter) | `overlay.py` | done — prompt, streamed answer, T3 dialog |
| Telegram bridge | `telegram.py` | done — long-poll, allowlist `[199251286]`, no inbound port (D40) |
| T3 confirmation | `daemon.py` | done — payload-bound, single-use, three-scope (D32) |
| Quarantine (D33) | `daemon.py` | done — delete is a recoverable *move*, never an unlink |

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

**What is not done:** the **brain is wired but only lightly exercised**. Sixteen of the registered
tools have executors and the rest of the surface is visible in `status` rather than hidden behind a
stub that returns success — which is the point: a tool listed but unimplemented is a gap the daemon
admits to, and a tool stubbed to succeed is a lie the model acts on.

**The vertical slice is now closed** (commit `1dec25d` + `overlay.py`). Measured end to end through a
real daemon, a real child process and the real 4B:

| Step | Result |
|---|---|
| hotkey → prompt → streamed answer | live |
| grammar-constrained decision | `kind: tool`, 2.7 s |
| the call itself | `tool: file.list`, `arguments: {'path': 'C:/Users/Public/Documents'}` |
| T3 confirmation dialog | built in tkinter, `confirm.approve` wired |
| VRAM after the turn | +4.96 GiB, under the D1 ceiling |
| VRAM after the 90 s idle TTL | returned to the 1.23 GiB baseline exactly |
| audit chain | verifies; lease, proposal and idle stop all recorded |

What this exposed is worth more than the slice itself, and is recorded as **D38**: three separate
instances of one grammar bug, none of which any shape assertion caught. In GBNF the quotes around a
literal are *syntax* — the model never emits them — so `toolname ::= "file.list"` produces `file.list`
with no JSON quotes, and `json.loads` stops at character 9. The same bug hit the argument keys and then
the string values. Each compiled cleanly, each passed every "does the grammar look right" assertion, and
each produced text that would not parse. The check that holds is running the *emitted* text through
`json.loads` in the tests.

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

## Phase 5 — Voice · **done, without hands-free**

Voice works, and it works by push-to-talk. The wake word was built, measured,
and dropped for a stated reason (D57).

| Component | File | State |
|---|---|---|
| TTS | `speech.py` | done — piper, 3 voices, measured **2.6 words/sec** |
| STT | `speech.py` | done — faster-whisper `small.en` CPU/int8, **RTF 1.30, WER 0.000, 0.27 GiB** |
| Capture & endpointing | `audio.py` | done — native-rate open then resample, hysteresis band, **measured** calibration |
| Push-to-talk | `audio.py` + `daemon.py` | **the voice input.** `voice.listen`, T3, `audible`, measured end to end |
| Wake word | `audio.py` (`WAKE_WORD = "hey_jarvis"`) | **abandoned (D57).** 0.993 on piper, **0.290 on the user's mouth** against a 0.5 threshold. Threshold stays: lowering it to 0.3 admits the D56 "hey + name" family at 0.96–0.99 |
| The loop | `voice_loop.py` | built, not enabled — `voice.watch` refuses rather than open a mic on a model that does not fire on this user |
| Registry | `tools.py` / `policy.py` | done — `voice.speak` / `voice.listen` (T3, `audible`) / `voice.status` (T0) |
| Daemon | `daemon.py` | done — executors, previews, `voice.watch` on its own thread |

**The gate is closed, in the direction the measurement pointed.** D54 asked
whether a trained wake word works on a mouth rather than on a synthesised
corpus. It does not, on this one: piper's rendering of "hey jarvis" scores
0.993 through the production code path and the user's own voice scores 0.290,
minutes apart, through the same scorer with a 0.993 control printed beside it.
Upstream's numbers were someone else's noise.

Four things that were not the model, and cost most of the afternoon: the USB
mic's **power switch was off**; `default_input()` opens the **44100 Hz copy** of
a device whose 48000 Hz copy carries the voice, ~100× louder; device indices
**do not survive replugging**; and `-9999 Blocking API` is a **host-API
limitation, not another program holding the mic** — which is what the
`NoInputDevice` message used to tell the user to go and fix. All four now
corrected or documented (D57).

Three plan lines are now wrong, and being wrong is worth recording:

* **D18's "promoted to GPU while listening" is withdrawn, not built (D50).** The measurement
  contradicted it: CPU whisper is real-time and costs 0.27 GiB of RAM out of 16. An
  `onnxruntime-gpu` install competing with `llama-cpp-python` for the card is not worth
  1.3× on something already faster than the user speaks. **Voice costs no VRAM at all.**
* **`openWakeWord` was chosen over Silero VAD**, but Silero ships *inside* openWakeWord
  (`silero_vad.onnx`), so the endpointing upgrade from `F:\axiom-voice-agent` is available
  without their checkout. Worth revisiting: it discriminates better than RMS and is
  gain-invariant. Not adopted because as written it is worse — it discards onset frames
  (the initial consonant) and has no minimum-utterance guard.
* **`piper` is not tiny enough to be uninteresting** and the voice is fixed-timbre: 3
  voices. `F:\voicestudio` showed the axis that matters is *speaker* diversity (~9,600
  English configurations from OmniVoice's voice design), which is also exactly what a
  synthetic wake-word corpus is starved on.

**The measurement that closed this phase, and what it overturned.** The plan
assumed the last thing left was a mouth test on a wake word that already
worked. It did not work. `probe_wakeword_real.py` and the framing probe
together say the pretrained model scores **0.993 on piper's rendering of the
phrase and 0.290 on the user's actual voice**, same scorer, same day, same
code path, 0.5 threshold, with the 0.993 control printed on the same screen.
The mouth test was worth running precisely because D54 predicted that
upstream's numbers, taken on someone else's noise, would not survive contact
with one specific mouth.

That measurement also cost four wrong turns before the right one, all recorded
in D57 and all about the hardware rather than the model. Two are worth carrying
into any future audio work here: **a peak is one sample and clipping is a
proportion** (8 of 240,000 samples at the rail, not a wall), and
**`default_input()` is not the loudest copy of a device** - the same USB
microphone enumerates at 44100 Hz (near-silent) and 48000 Hz (usable), and the
default picks the quiet one.

> A wake word that misses you is an inconvenience; one that fires on your
> television trains you to ignore it. `voice.watch` **refuses** rather than
> falling back to an always-open microphone, and that refusal is now correct
> for a reason it could not previously state: the model does not fire on this
> user. Push-to-talk carries voice instead, and it is measured working.

**Hardware notes that cost two sessions, neither of which was the code.** Every
input first read peak ≤54 of 32767 — the noise floor of a quiet room. It was
the **USB port**: after the user moved it the device read peak 20154 and the
round trip returned `'as we test if it works like that.'` Ask a device for its
own rate; four of the nine fail a 16 kHz request outright with `Invalid sample
rate`, which reads exactly like broken hardware.

Then the same device went silent again, and the cause was a **physical power
switch on the microphone**. From inside the process, a switched-off USB mic and
a broken one are the same number. Three more things, all D57:

* The port move also changed the enumeration (`Microphone (3-` → `(4-`), so
  **no device index survives replugging**. Re-enumerate every session; never
  bake an index into a command or a config.
* **The same physical mic enumerates twice** — 44100 Hz and 48000 Hz — and the
  44100 Hz copy is ~100× quieter. `default_input()` picks the quiet one.
* **`-9999 Blocking API not supported yet` is a PortAudio host-API limitation,
  not another program holding the device.** It fires on every input at once,
  including the onboard Realtek mic, while nothing holds anything. `audio.py`
  used to tell the user to close other applications; it now says what is
  actually wrong.

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
| Model too slow for usable interaction | **Resolved** | D37, measured on a verified-quiet machine: 57.9 tok/s generation, **1540 tok/s prefill**. Prefill is 27x generation, so long-context retrieval is the fast path and the architecture should lean into tight retrieval with short answers |
| The 4B fast tier is not actually competent | **Resolved** | Phase 1 gate passed, exit 0 — but only *with* grammar constraint (D34). Without it the model narrates instead of calling, which is a silent failure |
| A constraint silently does nothing | Medium | D36: `grammar` nested in `response_format` is ignored with no error. Every constraining mechanism must be proved active by inspecting output shape, never trusted from the flag being set |
| Tier swap latency annoys in practice | Medium | ~5–15 s, only on hard planning turns; measure in Phase 1 and revisit if frequent |
| Assistant over-reaches on a destructive tool | Low | Tiers are code, not prompt (D13); deny-path absolute; unattended cap |
| Scope creep — brief is ~5× v1 plan | **High** | Gates prevent forward motion on unproven ground; each phase is independently shippable |
| Obsidian vault too large to index | **Resolved** | ~430 indexable notes; `datasets/` hard-excluded (D14) |
| "22 GB VRAM available" from a tool | **Resolved** | llm-checker double-counts the 3060; recorded and rejected in D23. One GPU, 12 GB |
| A `uv pip install` of llama-cpp-python silently reverts to a wheel | Medium | Install with `--no-binary :all:`; `probe_isa.py` is the standing smoke test for this CPU (D25) |

---

## Immediate next action

**Capability priority order, as the user set it — this governs which gap gets built next:**

1. **System knowledge** — done. Eight `system.*` T0 reads, measured end to end (below).
2. **Tech support** — carried by the same reads plus `system.diagnose`; nothing further owed.
3. **Administration** — **the next unimplemented slice.** The assistant must be able to *do*, not only report.
4. **Coding** — the brain writes code when asked; `delegate.*` worker dispatch is the unwired part.
5. **Pedagogy** — `teach.*`, six T0 tools, entirely unbuilt.

Current state: **842 tests pass, 3 skipped, 47 subtests. Phases 0-5 closed** (voice closed by D57 without hands-free; push-to-talk is the voice input). The tray, hotkey, presence, T3 confirmation, quarantine, grammar-first brain as a disposable leased child, the tkinter overlay, the Telegram bridge, the Obsidian bridge, 35 tool executors including the whole of administration, and the whole tool registry are done.

**The real gap is breadth, not depth.** Measured, not estimated:

```
registered tools: 61        executors on the Daemon: 35        unimplemented: 26
```

**Capability item 3, administration, is complete.** What is left is none of it:

| group | missing | what it would buy |
|---|---|---|
| `delegate` | 8 | worker dispatch and review — coding item |
| `email` | 6 | IMAP, the one transport besides Telegram in scope |
| `teach` | 6 | **pedagogy, item 5, nothing built** |
| `reflect` | 4 | Phase 6 self-improvement |
| `file` | 2 | `write_notes` (T1), `empty_recycle_bin` (T3, destructive) |

**Done since this list was written:** `app.launch`, `app.close`, `shell.run` (T2, D58); `telegram.poll`, `telegram.send_draft`, `telegram.send` (D59); `registry.write`, `service.start_stop`, `package.install`, `package.uninstall`, `power.shutdown` (T3, D60). The T3 set took the shape the D58 section above argues for — an explicit preview of the exact argv, a payload-bound confirmation, and a **refusal in the grammar rather than in a warning** (D49): persistence registry keys, services whose stopping cannot be undone, and any shutdown delay under 30 seconds.

**Next, in order** — following the capability order the user set, not my own
preference for the largest gap:

1. **`delegate.*`** — item 4 (coding). The brain writes code; what it cannot yet do is hand a unit of work to a worker subprocess and review the result. Eight tools: `plan`, `brainstorm`, `status`, `review`, `audit` are T0 reads over a worker's own state; `assign`, `intervene`, `accept` are T2, because handing a subprocess the ability to act is a consequence, not a read.
2. **`teach.*`** — item 5 (pedagogy), six T0 tools, entirely unbuilt.
3. **`file.write_notes`** and **`file.empty_recycle_bin`** — two small tools. `empty_recycle_bin` is T3 destructive and deserves the D60 undo journal treatment rather than a bare `Remove-Item`.
4. **`email.*`** — six tools, IMAP. The only transport besides Telegram in scope. `email.send` is T3 external; `email.delete` is T3 destructive; the three reads are T0.
5. **`reflect.*`** — Phase 6, last. D21 forbids the assistant writing its own code or policy permanently, at every phase.

### The machine can now be read (D40–D44)

Eight `system.*` executors, all T0 reads, all delegating to `assistant.sysinfo`:
`system.info`, `processes`, `gpu_state`, `disk_usage`, `package_list`,
`service_status`, `event_log`, `diagnose`. Measured end to end — question in plain
English, model picks the tool and its arguments, gate authorises, real data back:

```
Q: how much VRAM am I using right now     -> NVIDIA GeForce RTX 3060: 6.26 GiB used of 12.0, 34.0% util, 57.0 C
Q: check whether this machine is healthy  -> verdict=ok complete=True checked=[memory, disk, gpu, event_log]
Q: is my disk almost full                 -> C: 255.6 GiB free of 930.4 (72.5% used) ... E: 34.1 GiB free
Q: how long has the machine been up       -> AMD Ryzen 5 5600G, 6 cores, up 18h, load 3.1%, RAM 38% used
audit chain verifies: True
brain.load=allow, brain.turn:system.gpu_state=allow, system.gpu_state=allow, ...
```

Getting there found four more instances of D43 — generator output that is legal
in the generator's own language and unusable by its consumer — including a
`bool` emitted as bare `true` (a rule reference, which segfaults the sampler), a
leading comma on all-optional tools, and an unbounded integer that let the model
spend a whole token budget on a 500-digit `limit`. The two that mattered most were
structural: `parse_action` did **not** validate arguments despite a comment
saying it did, and the flat `args` alternation let the model pair `system.diagnose`
with the `file.create` arguments. Fixed by validating against the registry, and by
settling the tool name in pass 1 so pass 2 has exactly one shape in scope (D42).
2 of 5 questions produced a call before; 5 of 5 after.

**One measurement task discharged, one still owed.** D28 is **satisfied** for speed: the user shut
down `Wan2GP`, the machine was verified quiet first (load 4–10 %, the lingering Wan2GP python process
accumulated 0 s of CPU across three 3-second samples), and `phase1_measure.py` was re-run three times
— **57.85 tok/s generation, 1540 tok/s prefill, 2.5 s load, 4.92 GiB VRAM** (D37). The script now
samples CPU load itself and prints `VALID` or `CONTENDED` rather than hardcoding the caveat, because a
stale caveat reads exactly like a true one.

Still owed: download candidates 2 and 3 — the 9B deep tier at 5.96 GB and the gemma-4 wildcard at
5.37 GB. The deep tier **must evict**
the fast tier; they cannot co-reside.
