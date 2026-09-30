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
import tempfile
import threading
import time

MODEL = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "models",
    "Qwen3.5-0.8B-Q4_0.gguf",
)
PROMPT = "Name three uses for a paperclip. Answer in one short sentence."


class CaptureFd:
    """Capture C-level stderr.

    llama.cpp prints its offload report from C++, not from Python, so
    contextlib.redirect_stderr cannot see it. We have to dup2 the real file
    descriptor. This is not gold-plating: it is the difference between reading
    what llama.cpp *reports it did* and reading what we *asked it to do*. Those
    are different numbers, and only the first one is evidence.
    """

    def __init__(self):
        self.path = None
        self._fd = None
        self._saved = None

    def __enter__(self):
        self._fd, self.path = tempfile.mkstemp(prefix="phase0_", suffix=".log")
        sys.stderr.flush()
        self._saved = os.dup(2)
        os.dup2(self._fd, 2)
        return self

    def __exit__(self, *exc):
        try:
            sys.stderr.flush()
            os.dup2(self._saved, 2)
            os.close(self._saved)
        finally:
            os.close(self._fd)
        return False

    def text(self):
        try:
            with open(self.path, "r", errors="replace") as fh:
                return fh.read()
        except OSError:
            return ""


def parse_offload(log):
    """Pull (offloaded, total) out of llama.cpp's own load report.

    Matches the two shapes llama.cpp emits:
        offloaded 25/25 layers to GPU
        offloaded 24 repeating layers to GPU   (older builds count differently)
    """
    m = re.search(r"offloaded (\d+)/(\d+) layers to GPU", log)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"offloaded (\d+) repeating layers to GPU", log)
    if m:
        return int(m.group(1)), None
    return None, None


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
        # NOT `self._stop`. threading.Thread has its own private `_stop()` method,
        # which CPython calls from _wait_for_tstate_lock during join(). Assigning
        # an Event over that name shadows a real method and turns join() into
        # `'Event' object is not callable` - and only on join, so it hides until
        # you actually wait for the thread.
        self._halt = threading.Event()

    def run(self):
        while not self._halt.is_set():
            row = nvidia_smi("memory.used,utilization.gpu")
            if row:
                used, util = [x.strip() for x in row.split(",")]
                self.samples.append((int(used), int(util)))
            self._halt.wait(self.interval)

    def stop(self):
        self._halt.set()
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
    with CaptureFd() as cap:
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
    # The request value is NOT evidence. `-1` is stored as 0x7FFFFFFF, so asking
    # for full offload tells us nothing about whether we got it. Read the report.
    log = cap.text()
    n_gpu, n_total = parse_offload(log)
    requested = getattr(getattr(llm, "model_params", None), "n_gpu_layers", None)
    print(f"requested n_gpu_layers : {requested}")
    print(f"llama.cpp reported     : {n_gpu}/{n_total} layers offloaded to GPU")
    if n_gpu is None:
        for line in log.splitlines():
            if "offload" in line.lower() or "CUDA_Host" in line or "buffer size" in line:
                print(f"  | {line.strip()}")

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

    # 2. full offload - as REPORTED by llama.cpp, not as requested by us.
    #    No report at all is itself a failure: we would be guessing.
    if n_gpu is None:
        failures.append(
            "llama.cpp printed no offload report - cannot prove GPU residency"
        )
    elif n_total is None:
        failures.append(
            f"llama.cpp reported {n_gpu} layers offloaded but not a total"
        )
    elif n_gpu < n_total:
        failures.append(
            f"partial offload {n_gpu}/{n_total} -> CPU spill on 6 cores"
        )

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

    print(f"offloaded     = {n_gpu}/{n_total} (llama.cpp's own report)")
    print(f"vram delta    = {(loaded[0]-base[0]) if (base and loaded) else 'n/a'} MiB")
    print(f"peak util     = {peak_util}%")

    if failures:
        print("GATE: FAIL")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("GATE: PASS - CUDA offload works and Qwen3.5 is supported")
    return 0


if __name__ == "__main__":
    sys.exit(main())
