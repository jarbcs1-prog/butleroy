# Architecture Decisions

Status: proposed, 2026-09-30. Source proposal: [`AI_Assistant_Local.md`](./AI_Assistant_Local.md) (retained for reference; superseded where noted below).

Target: fully local Windows 11 assistant. No cloud inference, no hosted API, no external
runner (no Ollama / LM Studio / `llama-server`). Inference is embedded in our own process.

---

## D1 — Hard resource budget (the governing constraint)

**The assistant must never be the reason the machine is slow.** VRAM and RAM are shared
with the user's actual work: browser, IDE, video calls, games, compiles.

- Target steady-state assistant VRAM: **≤ 7.0 GiB of 12.0 GiB** (~58%).
- Target process-tree RAM ceiling: **≤ 16 GiB of 32 GiB**.
- The assistant **returns the GPU when idle** (see D3). Idle is the normal state.

Corollary: we do **not** run a resident fleet of models. Audio, vision, and reasoning
models take turns on the GPU; they do not coexist. This is a design invariant, not a
tuning knob.

Consequence for model choice: a 9B at Q4_K_M (~5.2–6.0 GB weights) plus ~0.5–0.8 GB
compute buffers plus KV cache lands close to the ceiling. It fits — but only if nothing
else is resident. The ladder in `PLAN.md` Step 1 is ordered by how much headroom it
leaves, not by raw capability.

> All VRAM figures in this repo are *estimates until measured*. Step 1 measures actual
> allocation with `nvidia-smi` and records it. The original proposal's VRAM table was
> wrong by roughly 2x on KV cache because it was estimated rather than measured.

## D2 — Embedded inference via prebuilt CUDA wheel

`llama-cpp-python`, prebuilt `cu124` `win_amd64` wheel from the `abetlen` index
(versions through 0.3.35). **No C++ source build.** `cl.exe` is not on PATH on this
machine, and MSVC + CMake + CUDA compile is a multi-hour detour we get nothing from —
prebuilt wheels include `sm_86` (Ampere), which covers the RTX 3060.

Toolkit 13.3 is installed but is *not* used by the wheel. Driver 616.92 supports
CUDA 13, and CUDA 12.x runtime is backward compatible, so this is safe.

Fallback if the wheel is unusable: source build using the already-installed VS
BuildTools, entering `vcvars64.bat` first.

## D3 — One process per model, with idle eviction

The engine runs in a **child process per model**, spawned on demand and killed after an
idle TTL (start at 90 s, tune later).

- Killing a process is the only reliable way to return VRAM. `del llm_object` inside a
  long-lived process fragments and leaks CUDA allocations.
- Process isolation means a model crash takes down the worker, not the overlay.
- The tray/overlay process stays resident and light (~200 MB) so the hotkey always
  responds instantly, even with no model loaded.

This is why the original proposal's "IPC complexity" con is not optional — it is a direct
consequence of needing to give VRAM back. Cross-process transport: Windows named pipes.
No sockets, no ports, nothing listening.

## D4 — One multimodal model, not a fleet

Rejected: separate text LLM + separate vision model + separate audio model.

Instead: a single 9B-class GGUF with a **paired `mmproj`** projector, so the same
weights do reasoning and screen reading. This eliminates a whole second multi-GB
resident model. Audio (Whisper *tiny/base* via `faster-whisper`) stays in **CPU RAM
permanently** and is promoted to GPU only while actively listening — it is small enough
that CPU is acceptable on this box.

## D5 — Safety is deterministic code, never a model

The proposal routes every generated PowerShell command through Laya (a 421M model) as a
destructive-command gate. **Rejected**, on evidence:

- The published PyPI README for `laya` (v0.3.22, Convai Innovations) reports base
  checkpoints scoring **0.36 / 0.35 against a 0.318 random baseline** — near chance.
  It only reaches 0.766 *after fine-tuning*, which we are not doing.
- It declares `torch>=2.0.0` and `transformers>=4.48.0` as hard dependencies, which
  drags a multi-GB PyTorch runtime into a stack that otherwise does not need it
  (`faster-whisper` = CTranslate2, Piper/openWakeWord = onnxruntime).
- All releases are dated 2026-09-18 → 2026-09-29. It is roughly one week old and
  pre-1.0.

Replaced by a **static, auditable policy gate** in the executor: per-tool `destructive`
flag, path-pattern and verb-pattern rules, three tiers (`allow` / `confirm` / `deny`),
and an append-only audit log. No model participates in the decision to execute
something destructive. This matters *especially* because we chose an uncensored model
that will not refuse anything on its own.

## D6 — Native tool-calling, with grammar as backstop

The proposal's GBNF grammar admits exactly one action shape with a single `"target"`
string — no optional arguments, no multi-step plans, no error path. Unusable as a
protocol.

Instead: native OpenAI-style tool calling (Qwen3.5-class models support it), tool
schemas validated with pydantic before anything executes. GBNF is retained only as a
last-mile backstop on a single free-text argument where the model is otherwise
unreliable.

## D7 — Wake word must be offline

Rejected: `pvporcupine`, which requires a cloud-issued access key to provision models —
directly violates the "no cloud" constraint.

Use `openWakeWord` (ONNX, offline) or Silero VAD, with hotkey as the primary trigger.

## D8 — Hotkey is `Ctrl+Alt+Space`

`Win+Space` is reserved by the Windows shell for input-language switching, and
`Win+Alt+Space` collides with some vendor utilities. `Ctrl+Alt+Space` is unbound by
default.

## D9 — Uncensored model, verified heretic/abliterated GGUF

Confirmed present on the Hub as of 2026-09-30:

- `DavidAU/Qwen3.5-9B-The-Defiant-Fable-Uncensored-Heretic-NEO-IMATRIX-MAX-MTP-GGUF`
  — Q4_K_M / Q5_K_M plus `mmproj-F16.gguf` and a `plusIQ-TOOLS` variant
- `mradermacher/Huihui-Qwen3.5-9B-abliterated-GGUF` — Q4_K_M plus `mmproj-Q8_0.gguf`
- `mradermacher/Qwen3.8-9B-heretic-uncensored-GGUF` — newer, tagged for function calling

The original proposal's Qwen2.5-7B/14B choice is roughly two generations stale and costs
VRAM for less capability.

**Open risk:** a model newer than the bundled llama.cpp build may fail to load (unknown
architecture). This is the first thing Step 0/Step 1 must prove, before anything else is
built on top of it.
