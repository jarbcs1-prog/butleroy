"""Prove the install works, by running it.

Every other gate script in this project was written after something went wrong,
and this one starts from the same place: `pip install .` is not a claim that the
assistant runs, it is a claim that some files landed in a directory. The
difference between those two claims is what this script exists to close.

The check that matters is `inference`, and it is last in importance and first in
this file. **Importing `llama_cpp` proves nothing.** The prebuilt wheel's
`ggml-cpu.dll` is compiled with AVX-512 (D25); it imports perfectly on a Ryzen
5 5600G and then dies with `STATUS_ILLEGAL_INSTRUCTION` the moment it executes
an instruction. So a verifier that stops at the import would report a broken
install as healthy, and the user would be told everything is fine right up until
inference crashes. This one loads a model and generates a token.

`check_isa` exists so the diagnosis is available *before* the crash. The broken
wheel had 9,786 EVEX-shaped instruction sites against 12,745 `0x62` bytes
(76.7%); our own source build has 10 of 984 (1.02%), which is below a control
DLL known to be AVX-512-free (2.74%). A bare `0x62` byte is also BOUND, so it
appears in ordinary code and data - which is why the guard measures the *ratio*
of EVEX-shaped sites and not the byte count. D55's subject exactly: a check on a
count does not validate a unit, and the control DLL had more `0x62` bytes than
our build while being perfectly fine.

Usage:
    python verify_install.py            # run everything, exit 0 only if all pass
    python verify_install.py --quiet    # failures and the summary only
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
VENV_SITE = REPO_ROOT / ".venv" / "Lib" / "site-packages"
MODELS = REPO_ROOT / "models"

#: Above this fraction of EVEX-shaped sites, a ggml-cpu.dll was compiled for a
#: CPU with AVX-512 and will fault on one without. The gap is enormous on
#: purpose - 76.7% (broken wheel) against 2.74% (control DLL) - because a
#: threshold in the middle of that gap is a guess about someone else's build,
#: and a guess here produces a false all-clear on the one machine that matters.
EVEX_RATIO_LIMIT = 0.10


@dataclass(frozen=True)
class Result:
    name: str
    ok: bool
    detail: str
    skipped: bool = False


@dataclass(frozen=True)
class Check:
    name: str
    run: object


# --------------------------------------------------------------------------- #
# individual checks
# --------------------------------------------------------------------------- #


def check_python() -> Result:
    v = sys.version_info
    if v[:2] != (3, 11):
        return Result("python", False,
                      f"need 3.11.x for tomllib and the tier dataclasses; "
                      f"this is {v.major}.{v.minor}.{v.micro}")
    bits = 64 if sys.maxsize > 2**32 else 32
    if bits != 64:
        return Result("python", False, f"need a 64-bit interpreter; this one is {bits}-bit")
    return Result("python", True, f"{v.major}.{v.minor}.{v.micro}, 64-bit")


def check_imports() -> Result:
    """Plane A must import with no model runtime present.

    This is D1 as an install-time property. The daemon's whole argument for
    being cheap to keep resident is that it holds the gate, the leases and the
    chain with the model dead, so an install that cannot import `daemon` on its
    own is not this project.
    """
    if str(REPO_ROOT / "src") not in sys.path:
        sys.path.insert(0, str(REPO_ROOT / "src"))
    try:
        for module in ("daemon", "launcher", "tray", "overlay", "policy",
                       "ipc", "audit", "arbiter", "manifest", "grammar"):
            importlib.import_module(f"assistant.{module}")
    except Exception as exc:  # noqa: BLE001 - the reason is the point
        return Result("imports", False, f"assistant.* will not import: {exc!r}")
    leaked = sorted(m for m in sys.modules
                    if m.split(".")[0] in ("llama_cpp", "torch", "faster_whisper"))
    if leaked:
        return Result("imports", False,
                      f"Plane A pulled in a model runtime: {leaked}. The daemon "
                      f"must hold the gate with the model dead (D1)")
    return Result("imports", True, "assistant.* imports; no model runtime loaded")


def check_no_ports() -> Result:
    """No listening socket may exist anywhere in the assistant."""
    if str(REPO_ROOT / "src") not in sys.path:
        sys.path.insert(0, str(REPO_ROOT / "src"))
    try:
        from assistant import ipc

        ipc.assert_no_ports()
    except Exception as exc:  # noqa: BLE001
        return Result("no_ports", False, f"a listening socket exists: {exc}")
    return Result("no_ports", True, "no TCP listener; transport is named pipes only")


def count_evex(path: Path) -> tuple[int, int, int]:
    """Return ``(size, bare_0x62, evex_shaped)`` for a binary.

    EVEX is a 4-byte prefix: 0x62, then P0 with bit 3 clear and its low three
    bits selecting the opcode map (1=0F, 2=0F38, 3=0F3A), then P1 with bit 3 set,
    then P2. A bare 0x62 is also BOUND, so the constraint on the *following*
    bytes is the only thing that separates the two.
    """
    data = Path(path).read_bytes()
    bare = data.count(b"\x62")
    evx = 0
    for i in range(len(data) - 3):
        if data[i] != 0x62:
            continue
        p0, p1 = data[i + 1], data[i + 2]
        if (p0 & 0x08) == 0 and (p0 & 0x07) in (1, 2, 3) and (p1 & 0x08):
            evx += 1
    return len(data), bare, evx


def check_isa(path: Path | None = None) -> Result:
    """Flag a `ggml-cpu.dll` compiled for a CPU with AVX-512.

    The diagnosis, available before the crash. See the module docstring for why
    this measures a ratio rather than a count.
    """
    target = Path(path) if path is not None else VENV_SITE / "llama_cpp" / "lib" / "ggml-cpu.dll"
    if not target.exists():
        return Result("isa", False,
                      f"no ggml-cpu.dll at {target}. llama-cpp-python is not "
                      f"installed - see INSTALL.md, and note that the prebuilt "
                      f"wheel is NOT usable on this CPU (D25)")
    size, bare, evx = count_evex(target)
    ratio = evx / bare if bare else 0.0
    if ratio > EVEX_RATIO_LIMIT:
        return Result(
            "isa", False,
            f"{target.name}: {evx} EVEX-shaped sites in {size/1e6:.1f} MB, "
            f"{ratio*100:.2f}% of its 0x62 bytes. This library was compiled for "
            f"a CPU with AVX-512 and will die with STATUS_ILLEGAL_INSTRUCTION "
            f"here. Rebuild from source with GGML_NATIVE=OFF (D25, INSTALL.md).",
        )
    return Result("isa", True,
                  f"{target.name}: {evx}/{bare} EVEX-shaped ({ratio*100:.2f}%), "
                  f"under the {EVEX_RATIO_LIMIT*100:.0f}% limit")


def check_models() -> Result:
    """Report which weights are present. Absent is a skip, never a failure.

    The models are 4-12 GB and are not shipped. A verifier that failed on their
    absence would report a perfectly good install as broken, and the user would
    learn to ignore it.
    """
    if not MODELS.exists():
        return Result("models", True, "no models/ directory; nothing downloaded yet",
                      skipped=True)
    found = sorted(p.name for p in MODELS.glob("*.gguf") if p.stat().st_size > 0)
    if not found:
        return Result("models", True, "models/ holds no .gguf files", skipped=True)
    total = sum(p.stat().st_size for p in MODELS.glob("*.gguf"))
    return Result("models", True, f"{len(found)} model file(s), {total/1e9:.2f} GB: "
                                  + ", ".join(found[:4]))


def check_inference() -> Result:
    """Load a model and generate a token. The check that cannot be faked.

    Everything else in this file can pass on an install that cannot run. This one
    cannot: the AVX-512 wheel gets all the way through the import and dies here,
    which is the entire reason D25 cost an afternoon.
    """
    if str(REPO_ROOT / "src") not in sys.path:
        sys.path.insert(0, str(REPO_ROOT / "src"))
    try:
        from llama_cpp import Llama
    except Exception as exc:  # noqa: BLE001
        return Result("inference", False,
                      f"llama_cpp will not import: {exc!r}. Build it from source "
                      f"- see INSTALL.md (D25)")

    # The smallest file present, so the check is cheap. Ordering by size rather
    # than by name is deliberate: the 0.8B is the fast tier's ancestor and the
    # 9B is the deep tier, and neither is guaranteed to be on disk.
    candidates = sorted((p for p in MODELS.glob("*.gguf")
                         if p.stat().st_size > 50_000_000),
                        key=lambda p: p.stat().st_size)
    if not candidates:
        return Result("inference", True,
                      "no model on disk to execute; download one and re-run",
                      skipped=True)
    model = candidates[0]
    started = time.time()
    # Capture llama.cpp's own C-level stderr. It prints its offload report from
    # C++, so contextlib.redirect_stderr cannot see it, and `Llama.n_gpu_layers`
    # does not exist in 0.3.35 - asking for -1 puts 0x7FFFFFFF on
    # `model_params`, which is what we *requested*, not what we *got*, and so
    # could never detect partial offload. phase0_gate.py already solved this;
    # reusing it rather than reimplementing is the point of a shared helper.
    sys.path.insert(0, str(REPO_ROOT))
    from phase0_gate import CaptureFd, parse_offload

    try:
        with CaptureFd() as cap:
            llm = Llama(model_path=str(model), n_ctx=512, n_gpu_layers=-1,
                        verbose=True)
            out = llm("Hello", max_tokens=4)
        log = cap.text()
    except Exception as exc:  # noqa: BLE001
        return Result("inference", False,
                      f"{model.name} loaded but could not generate: "
                      f"{type(exc).__name__}: {exc}. If this is an illegal "
                      f"instruction, the runtime is the prebuilt AVX-512 wheel "
                      f"(D25).")

    offload = parse_offload(log)
    if offload is None:
        # A missing report is a failure, not something to paper over. Partial
        # offload means silent CPU spill, which on six cores is unusable, and a
        # verifier that reports "fine" when it simply could not tell is worse
        # than one that admits it.
        return Result("inference", False,
                      f"{model.name} generated text but llama.cpp printed no "
                      f"'offloaded N/N layers to GPU' line, so GPU offload could "
                      f"not be confirmed. Inference may have run entirely on the "
                      f"CPU, which the 7.0 GiB ceiling (D1) assumes it does not.")
    done, total = offload
    if done != total:
        return Result("inference", False,
                      f"{model.name}: only {done}/{total} layers offloaded to GPU. "
                      f"Partial offload means silent CPU spill on 6 cores.")
    detail = (f"{model.name}: generated {len(out['choices'][0]['text'])} chars in "
              f"{time.time()-started:.1f}s, {done}/{total} layers on GPU")
    return Result("inference", True, detail)


def check_daemon() -> Result:
    """`python -m assistant --check` must work with no display attached."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "assistant", "--check"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=120, env=env,
        )
    except subprocess.TimeoutExpired:
        return Result("daemon", False, "python -m assistant --check timed out")
    if proc.returncode != 0:
        return Result("daemon", False,
                      f"preflight exited {proc.returncode}: {proc.stderr[-400:]}")
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return Result("daemon", False, "preflight did not print JSON")
    if not report.get("audit_chain", {}).get("ok"):
        return Result("daemon", False, "the audit chain does not verify")
    if not report.get("no_listening_ports"):
        return Result("daemon", False, "preflight reports a listening port")
    return Result("daemon", True,
                  f"preflight ok: {report['audit_chain']['checked']} audit records "
                  f"verify, no listening ports")


#: Advertised order. `inference` is last because it is the only one that needs
#: a model on disk, and a verifier that stops early hides the checks that would
#: have explained why.
CHECKS: tuple[Check, ...] = (
    Check("python", check_python),
    Check("imports", check_imports),
    Check("no_ports", check_no_ports),
    Check("isa", check_isa),
    Check("daemon", check_daemon),
    Check("models", check_models),
    Check("inference", check_inference),
)


def run_all() -> list[Result]:
    """Run every check. One failing check does not hide the ones after it.

    A verifier that stops at the first failure is a verifier that tells you one
    thing at a time, and the interesting failures here are related - a wrong
    build shows up in both `isa` and `inference`.
    """
    results: list[Result] = []
    for check in CHECKS:
        try:
            results.append(check.run())
        except Exception as exc:  # noqa: BLE001 - a crash is a failed check
            results.append(Result(check.name, False,
                                  f"check raised {type(exc).__name__}: {exc}"))
    return results


def main(argv: list[str] | None = None) -> int:
    quiet = "--quiet" in (argv if argv is not None else sys.argv[1:])
    results = run_all()
    width = max(len(r.name) for r in results)
    for r in results:
        mark = "skip" if r.skipped else (" ok " if r.ok else "FAIL")
        if quiet and r.ok and not r.skipped:
            continue
        print(f"[{mark}] {r.name:<{width}}  {r.detail}")
    failed = [r for r in results if not r.ok]
    skipped = [r for r in results if r.skipped]
    print(f"\n{len(results) - len(failed) - len(skipped)} passed, "
          f"{len(failed)} failed, {len(skipped)} skipped")
    if failed:
        print("\nSee INSTALL.md. The llama-cpp-python source build is not "
              "optional on this CPU (D25).")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
