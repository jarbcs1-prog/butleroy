# Windows AI Assistant (local)

Fully local Windows 11 assistant. No cloud inference, no hosted APIs, no external
runner (no Ollama / LM Studio / `llama-server`). Inference is embedded in our own
process, against the local RTX 3060.

Nothing is built yet. This repo currently contains the plan.

## Documents

- [`DECISIONS.md`](./DECISIONS.md) — architecture decisions and their rationale,
  including where this supersedes [`AI_Assistant_Local.md`](./AI_Assistant_Local.md).
- [`PLAN.md`](./PLAN.md) — phased execution plan with exit gates.
- `AI_Assistant_Local.md` — the original proposal, retained for reference.

## The governing constraint

The assistant must never be the reason the machine is slow. VRAM and RAM are shared
with the user's actual work.

- Assistant VRAM: ≤ 7.0 GiB of 12.0 GiB
- Process tree RAM: ≤ 16 GiB of 32 GiB
- Models take **turns** on the GPU; they do not coexist
- The assistant returns the GPU when idle

This is a design invariant, not a tuning preference. See D1 and D3 in `DECISIONS.md`.

## Hardware this targets

| | |
|---|---|
| GPU | NVIDIA RTX 3060, 12 GB, driver 616.92 |
| CPU | Ryzen 5 5600G — 6 cores / 6 threads |
| RAM | 32 GB DDR4 |
| Toolchain | Python 3.11.9, `uv`, CUDA Toolkit 13.3, VS BuildTools (no `cl.exe` on PATH) |

Note the 6-core CPU: any layer that spills off the GPU will be painfully slow. Full GPU
offload is a requirement, not an optimization.

## Start here

Step 0 in `PLAN.md` — prove native CUDA inference from a prebuilt wheel, using a small
model, before downloading anything large. It is the cheapest possible moment to discover
the foundation does not work.
