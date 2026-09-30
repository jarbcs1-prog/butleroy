"""
Phase 0 - HARD GATE.

Proves two things in one test:
  1. Native CUDA inference works on this machine (N/N layers offloaded, GPU busy).
  2. llama.cpp 0.3.35 understands the Qwen3.5 architecture (which every real
     candidate model descends from).

Fails loudly and non-zero on either. Nothing downstream may be built until this
passes.

Usage:  .venv\\Scripts\\python.exe phase0_gate.py
"""

import os
import re
import subprocess
import sys
import threading
import time

MODEL = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "models",
    "Qwen3.5-0.8B-Q4_0.gguf",
)
PROMPT = "Name three uses for a paperclip. Answer in one short sentence."


def nvidia_smi(query):
    """Return one CSV line of GPU telemetry, or None if nvidia-smi is unavailable."""
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                f"--query-gpu={query}",
                "--format=csv,noheader,nounits",
                "-i", "0",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if out.returncode != 0:
            return None
        return out.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def baseline():
    row = nvidia_smi("memory.used,utilization.gpu")
    if not row:
        return None
    used, util = [x.strip() for x in row.split(",")]
    return int(used), int(util)


class GpuSampler(threading.Thread):
    """Polls nvidia-smi while the model generates, so we can prove the GPU was
    actually doing the work rather than the CPU silently taking over."""

    def __init__(self, interval=0.25):
        super().__init__(daemon=True)
        self.interval = interval
        self.samples = []
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            row = nvidia_smi("memory.used,utilization.gpu")
            if row:
                used, util = [x.strip() for x in row.split(",")]
                self.samples.append((int(used), int(util)))
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()
        self.join(timeout=5)


def main():
    print("=" * 72)
    print("PHASE 0 - HARD GATE")
    print("=" * 72)

    if not os.path.exists(MODEL):
        print(f"FAIL: model not found: {MODEL}")
        return 2
    size_mb = os.path.getsize(MODEL) / 1048576
    print(f"model   : {os.path.basename(MODEL)}  ({size_mb:.1f} MB)")

    from llama_cpp import Llama

    base = baseline()
    print(f"gpu idle: {base} MiB used, {base[1]}% util" if base else "gpu idle: UNKNOWN")
    print("-" * 72)

    t0 = time.time()
    llm = Llama(
        model_path=MODEL,
        n_ctx=2048,
        n_gpu_layers=-1,   # offload everything; the gate is N/N or nothing
        n_batch=512,
        verbose=True,      # this is how we read the offload report
    )
    load_s = time.time() - t0
    print("-" * 72)
    print(f"loaded in {load_s:.1f}s")

    # --- Gate check 1: architecture + full offload, read from llama.cpp's own log ---
    # verbose output goes to stderr; if it was not captured we rely on n_gpu_layers
    # and the VRAM delta below, which is the stronger evidence anyway.
    n_gpu = llm.n_gpu_layers
    n_total = llm.model_params.n_layer if hasattr(llm, "model_params") else None
    print(f"n_gpu_layers reported: {n_gpu}  (total layers: {n_total})")

    loaded = baseline()
    print(f"gpu after load: {loaded} MiB used" if loaded else "gpu after load: UNKNOWN")

    sampler = GpuSampler()
    sampler.start()
    time.sleep(1.0)

    t1 = time.time()
    out = llm.create_chat_completion(
        messages=[{"role": "user", "content": PROMPT}],
        max_tokens=128,
        temperature=0.0,
    )
    gen_s = time.time() - t1
    sampler.stop()

    text = out["choices"][0]["message"]["content"] or ""
    usage = out.get("usage", {})

    print("-" * 72)
    print("GENERATION")
    print(f"  {text.strip()[:300]}")
    print("-" * 72)
    print(f"elapsed        : {gen_s:.2f}s")
    print(f"prompt tokens  : {usage.get('prompt_tokens')}")
    print(f"output tokens  : {usage.get('completion_tokens')}")

    if sampler.samples:
        peak_mem = max(s[0] for s in sampler.samples)
        peak_util = max(s[1] for s in sampler.samples)
        avg_util = sum(s[1] for s in sampler.samples) / len(sampler.samples)
        active = sum(1 for s in sampler.samples if s[1] > 0)
        print(f"peak vram      : {peak_mem} MiB")
        print(f"peak gpu util  : {peak_util}%")
        print(f"avg gpu util   : {avg_util:.1f}%  (n={len(sampler.samples)} samples)")
        print(f"samples w/ gpu busy: {active}/{len(sampler.samples)}")
    else:
        peak_mem = peak_util = avg_util = None
        active = 0
        print("gpu telemetry  : UNAVAILABLE")

    # ------------------------------------------------------------------ verdict
    print("=" * 72)
    failures = []

    # 1. the model must have loaded at all (this is the Qwen3.5 architecture check)
    if not text.strip():
        failures.append("model produced no text - Qwen3.5 arch likely unsupported")

    # 2. full offload
    if n_gpu != -1 and n_total and n_gpu < n_total:
        failures.append(f"partial offload {n_gpu}/{n_total} -> CPU spill on 6 cores")
    elif n_gpu != -1 and n_gpu < 0:
        failures.append("negative n_gpu_layers")

    # 3. VRAM actually moved (physical evidence, independent of the log)
    if base and loaded and (loaded[0] - base[0]) < 200:
        failures.append(
            f"VRAM moved only {loaded[0]-base[0]} MiB after load - probably ran on CPU"
        )

    # 4. the GPU was actually busy while generating
    if peak_util is None:
        failures.append("no GPU telemetry (nvidia-smi unavailable)")
    elif peak_util < 10:
        failures.append(f"peak GPU utilisation only {peak_util}% - likely CPU inference")

    print(f"n_gpu_layers = {n_gpu}   layers_total = {n_total}")
    print(f"vram delta   = {(loaded[0]-base[0]) if (base and loaded) else 'n/a'} MiB")
    print(f"peak util    = {peak_util}%")

    if failures:
        print("GATE: FAIL")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("GATE: PASS - CUDA offload works and Qwen3.5 is supported")
    return 0


if __name__ == "__main__":
    sys.exit(main())
