"""Evidence for the illegal-instruction diagnosis.

Windows STATUS_ILLEGAL_INSTRUCTION (0xc000001d) is a hard CPU fault, not a
bug in llama.cpp's logic. The wheel's ggml-cpu.dll advertises AVX512=1 while
this Ryzen 5 5600G (Zen 3) has none. If the AVX-512 kernels really were
compiled in, we should find EVEX-encoded instructions in the binary.

EVEX is the 4-byte AVX-512 prefix: 0x62, then P0 whose bit 3 is 0 and whose
low 3 bits select the opcode map (1=0F, 2=0F38, 3=0F3A), then P1 with bit 3
set, then P2. A bare 0x62 byte is also a BOUND instruction, so the constraint
on the following byte is what separates the two.

We count against a control DLL that is certainly not AVX-512 compiled, to
calibrate how much of the signal is just the 0x62 byte appearing in ordinary
code and data.
"""
import os
import sys

LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".venv", "Lib",
                   "site-packages", "llama_cpp", "lib")

CONTROL = r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.8\bin\cudart64_12.dll"


def count_evex(path):
    with open(path, "rb") as fh:
        data = fh.read()
    bare = data.count(b"\x62")
    evex = 0
    for i in range(len(data) - 3):
        if data[i] != 0x62:
            continue
        p0 = data[i + 1]
        p1 = data[i + 2]
        if (p0 & 0x08) == 0 and (p0 & 0x07) in (1, 2, 3) and (p1 & 0x08):
            evex += 1
    return len(data), bare, evex


for path in (os.path.join(LIB, "ggml-cpu.dll"), CONTROL):
    if not os.path.exists(path):
        print(f"missing: {path}")
        continue
    size, bare, evex = count_evex(path)
    label = "control (no AVX-512)" if path == CONTROL else "SUSPECT"
    print(f"{label:22} {os.path.basename(path):24} {size/1e6:8.2f} MB  "
          f"0x62 bytes={bare:7d}  EVEX-shaped={evex:7d}  "
          f"ratio={evex/bare*100 if bare else 0:.2f}%")
