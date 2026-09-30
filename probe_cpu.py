"""Isolate the 0xc000001d illegal instruction: CUDA path or CPU backend?

Runs the same load with n_gpu_layers=0 (pure CPU) and, if that survives,
with n_gpu_layers=-1 (CUDA). The first one to crash tells us which backend
is emitting an instruction this Zen 3 CPU cannot execute.
"""
import os
import sys

# The CUDA 12.x runtime is required on PATH or llama.dll fails to load at all.
CUDA_BIN = r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.8\bin"
if os.path.isdir(CUDA_BIN):
    os.environ["PATH"] = CUDA_BIN + os.pathsep + os.environ.get("PATH", "")

from llama_cpp import Llama

MODEL = r"G:\localaized\models\Qwen3.5-0.8B-Q4_0.gguf"

# 1. Prove the CPU lacks AVX-512, which llama_print_system_info claims to support.
try:
    import numpy as np

    longdouble = np.longdouble(1)
    probe = longdouble.nbytes
    print(f"[cpu] sizeof(long double) = {probe} bytes "
          f"({'AVX-512 capable' if probe > 8 else 'NO AVX-512, max 80-bit x87'})")
except Exception as exc:  # pragma: no cover
    print(f"[cpu] long-double probe failed: {exc}")

mode = sys.argv[1] if len(sys.argv) > 1 else "cpu"
layers = 0 if mode == "cpu" else -1
print(f"\n=== run: mode={mode} n_gpu_layers={layers} ===", flush=True)

llm = Llama(
    model_path=MODEL,
    n_ctx=2048,
    n_batch=512,
    n_gpu_layers=layers,
    verbose=True,
    use_mmap=True,
)
print("[ok] context constructed", flush=True)
out = llm.create_completion(
    prompt="The capital of France is",
    max_tokens=16,
    temperature=0.0,
)
text = out["choices"][0]["text"]
print(f"[ok] generated: {text!r}")
print(f"[ok] n_gpu_layers reported = {llm.n_gpu_layers}")
print("RESULT=PASS")
