"""The install path, and the trap that lives in it.

Two things are asserted here, and both are about preventing a silent failure
rather than about convenience.

**The packaging manifest.** `pyproject.toml` did not exist, so the assistant
could not be installed - only run from a directory someone happened to have set
up. Worse, the obvious way to write one is also the dangerous way: declare
`llama-cpp-python` as a dependency and any `pip install .` fetches the *prebuilt
wheel*, whose `ggml-cpu.dll` is compiled with AVX-512. This Ryzen 5 5600G is Zen
3 and has none, so the runtime dies with `STATUS_ILLEGAL_INSTRUCTION` - on a
machine where everything else is correct and the failure names nothing useful.

D25 already paid for that lesson once. The manifest must therefore make the
wrong thing impossible rather than merely discouraged, and that is worth a test,
because a comment is not a constraint and D36's whole subject is a constraint
that was set and did nothing.

**The verifier.** A verifier that only *imports* `llama_cpp` would report a
broken install as healthy: the AVX-512 wheel imports perfectly and only dies when
it executes. So the ISA guard is tested against a synthetic AVX-512 DLL built
byte by byte in a temp directory - the guard has to be shown catching the real
failure mode, not just present.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import verify_install  # noqa: E402


@pytest.fixture(scope="module")
def pyproject() -> dict:
    path = REPO_ROOT / "pyproject.toml"
    if not path.exists():
        pytest.fail("pyproject.toml does not exist; the assistant cannot be installed")
    return tomllib.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# the manifest
# --------------------------------------------------------------------------- #


def test_the_manifest_declares_the_project(pyproject: dict) -> None:
    project = pyproject["project"]
    assert project["name"]
    assert project["version"]
    # 3.11 is not a preference: `tomllib` and the dataclass patterns in policy.py
    # need it, and the venv on this machine is 3.11.9.
    assert project["requires-python"].startswith(">=3.11")


def test_llama_cpp_python_is_not_a_plain_dependency(pyproject: dict) -> None:
    """The D25 guard, enforced rather than commented.

    A wheel dependency here is not a slow install, it is a runtime that cannot
    execute on this CPU. The failure surfaces as a crash inside llama.cpp with no
    hint that the install was the cause.
    """
    deps = " ".join(pyproject["project"].get("dependencies", [])).lower()
    assert "llama-cpp-python" not in deps, (
        "llama-cpp-python must not be a plain dependency: the prebuilt wheel is "
        "compiled with AVX-512 and dies with STATUS_ILLEGAL_INSTRUCTION on this "
        "CPU (D25). It belongs behind the 'brain' extra, built from source - see "
        "INSTALL.md."
    )


def test_the_core_dependency_set_stays_stdlib_plus_the_tray(pyproject: dict) -> None:
    """Plane A is pure stdlib, and the manifest must not pretend otherwise.

    ARCHITECTURE.md's load-bearing property is that importing the daemon pulls in
    no model runtime, so the resident process stays at a few hundred MB and no
    VRAM (D1). A core dependency on torch or faster-whisper would break that
    property at install time even though the imports are lazy, and would make
    `pip install butleroy` a multi-gigabyte download for a tray icon.
    """
    deps = " ".join(pyproject["project"].get("dependencies", [])).lower()
    for heavy in ("torch", "faster-whisper", "faster_whisper", "transformers",
                  "onnxruntime", "librosa", "openwakeword", "piper"):
        assert heavy not in deps, (
            f"{heavy} must be an extra, not a core dependency - the daemon must "
            f"stay importable without a model runtime (D1)"
        )


def test_the_tray_dependencies_are_declared(pyproject: dict) -> None:
    """tray.py needs pystray and pillow, so the core set is not empty."""
    deps = " ".join(pyproject["project"].get("dependencies", [])).lower()
    assert "pystray" in deps
    assert "pillow" in deps


def test_there_is_a_console_script_and_it_resolves(pyproject: dict) -> None:
    """`butleroy` must be a real command, and its target must actually exist.

    A console script pointing at a function that was renamed is the packaging
    equivalent of a door that opens onto a wall, and it fails only at the moment
    a user types the command.
    """
    scripts = pyproject["project"].get("scripts", {})
    assert "butleroy" in scripts, "the assistant needs one command to start it"
    for name, target in scripts.items():
        module_name, _, attr = target.partition(":")
        module = __import__(module_name, fromlist=[attr])
        assert callable(getattr(module, attr)), (
            f"{name} -> {target} does not resolve to a callable"
        )


def test_llama_cpp_python_is_named_by_no_dependency_list(pyproject: dict) -> None:
    """It must appear in NO dependency list - not core, not any extra.

    This test previously asserted the opposite: that `llama-cpp-python` WAS in
    the `brain` extra. That assumption was wrong in a way that broke installs,
    and the test enforced it, so the bug was protected rather than caught.

    llama-cpp-python 0.3.35 publishes no wheel on PyPI - one sdist, zero wheels.
    So naming it as a dependency does not install a prebuilt binary; it makes
    the package manager COMPILE it, with llama-cpp-python's defaults and not
    ours. That produced a ggml-cpu.dll at 76.73% EVEX-shaped - AVX-512, which
    this Zen 3 CPU cannot execute (D25).

    Worse, it broke the installer outright. install.ps1 runs
    `uv pip install -e .[brain,voice]` and deliberately does not initialise
    MSVC, so with no wheel to download that install has to compile with no
    compiler available and fails at `CMAKE_C_COMPILER not set`. Measured
    2026-10-13 by deleting .venv and reinstalling from scratch. It had only
    ever appeared to work because uv's cache held a wheel built without our
    flags.

    The single flag-controlled path is build_phase0b.bat: it calls vcvars64
    itself and passes GGML_NATIVE=OFF, GGML_AVX512=OFF, AVX2=ON and
    CMAKE_CUDA_ARCHITECTURES=86. Nothing that merely names a dependency can
    reproduce those flags.
    """
    project = pyproject["project"]
    extras = project.get("optional-dependencies", {})

    lists: dict[str, list[str]] = {"dependencies": project.get("dependencies", [])}
    for name, items in extras.items():
        lists[f"optional-dependencies.{name}"] = items

    offenders = [
        f"{where}: {item}"
        for where, items in lists.items()
        for item in items
        if "llama-cpp-python" in item.lower()
    ]
    assert not offenders, (
        "llama-cpp-python must not be named by any dependency list - PyPI has no "
        f"wheel for it, so any package manager asked to install it will compile it "
        f"with the wrong flags and fail on a machine without MSVC. Offending: "
        f"{offenders}. Build it with build_phase0b.bat instead (D25, D84, D87)."
    )

    # The extra itself must stay, because install.ps1 passes `.[brain]` and a
    # missing extra is a hard error. It is a marker, not a package list.
    assert "brain" in extras, (
        "the brain extra must still exist - install.ps1 installs `.[brain]`, and a "
        "missing extra is a hard error. It is intentionally empty."
    )
    assert not extras["brain"], (
        "the brain extra must stay empty; the brain is built by build_phase0b.bat, "
        "not installed by name"
    )


def test_no_dependency_requires_a_compiler_to_install(pyproject: dict) -> None:
    """A dependency that has no wheel turns `pip install` into a build step.

    install.ps1 does not initialise MSVC before installing dependencies, and
    deliberately so (install.ps1:155 - a probe that initialised it would mutate
    the shell's environment). So the manifest must only name packages that
    install from a wheel. llama-cpp-python is the one that violated this; this
    test exists so the next one is caught by a name and a rule rather than by
    deleting a working .venv.
    """
    project = pyproject["project"]
    extras = project.get("optional-dependencies", {})
    all_reqs = list(project.get("dependencies", []))
    for items in extras.values():
        all_reqs.extend(items)

    # Packages with no PyPI wheel for cp311/win_amd64, which therefore must not
    # be named as dependencies. Verified against the PyPI JSON API 2026-10-13.
    source_only = {"llama-cpp-python"}
    named = sorted({r for r in all_reqs if r.split(">")[0].split("=")[0].split("[")[0].strip().lower() in source_only})
    assert not named, (
        f"{named} publish no wheel on PyPI, so naming them turns dependency install "
        f"into a compile step that needs MSVC, which install.ps1 does not provide. "
        f"Install them via their build script instead."
    )


def test_the_optional_extras_cover_what_the_modules_import(pyproject: dict) -> None:
    """voice_loop.py and audio.py are Plane B/D work and must be optional."""
    extras = pyproject["project"].get("optional-dependencies", {})
    voice = " ".join(extras.get("voice", [])).lower()
    for pkg in ("sounddevice", "numpy", "faster-whisper"):
        assert pkg in voice, f"{pkg} is imported by audio.py/speech.py and belongs in [voice]"


# --------------------------------------------------------------------------- #
# the verifier
# --------------------------------------------------------------------------- #


def test_the_isa_guard_flags_a_real_avx512_library(tmp_path: Path) -> None:
    """The guard must catch the exact bytes that killed Phase 0.

    A synthetic `0x62`-prefixed EVEX sequence, repeated, is what the prebuilt
    wheel contained at 76.7% of its 0x62 bytes. If the guard cannot see this, it
    is decoration and `verify_install` would wave a broken install through.
    """
    # 62 F1 7F 28 ... is a genuine EVX.128 prefix: 0x62, P0 with bit3 clear and
    # map 1 (0F), P1 with bit3 set, P2.
    evx = b"\x62\xf1\x7f\x28\x00"
    planted = evx * 400
    suspect = tmp_path / "ggml-cpu.dll"
    suspect.write_bytes(b"\x00" * 64 + planted)

    result = verify_install.check_isa(suspect)

    assert not result.ok, "an AVX-512-compiled library must not pass the ISA guard"
    assert "AVX-512" in result.detail


def test_the_isa_guard_passes_a_library_without_them(tmp_path: Path) -> None:
    """The other half: a guard that always fails is not a guard either.

    `0x62` also encodes BOUND, and appears in ordinary code and data, so a
    library with plenty of 0x62 bytes but no EVEX-shaped ones is normal. This is
    exactly the calibration D25 recorded against a control DLL.
    """
    library = tmp_path / "ggml-cpu.dll"
    library.write_bytes(b"\x62\x11\x22\x33" * 500 + b"\x00" * 64)

    result = verify_install.check_isa(library)

    assert result.ok, f"a clean library must pass: {result.detail}"


def test_the_isa_guard_ignores_the_fraction_not_the_byte_count(tmp_path: Path) -> None:
    """D55, in the verifier: a check on a count does not validate a unit.

    A big library legitimately contains more 0x62 bytes than a small one. What
    distinguishes the broken wheel is the *ratio* of EVEX-shaped sites, not how
    many 0x62 bytes exist in total - which is why the control DLL in D25 had more
    0x62 bytes than our own build and was still fine.
    """
    mostly_clean = tmp_path / "big-but-clean.dll"
    mostly_clean.write_bytes((b"\x00" * 8 + b"\x62\x11\x22\x33") * 5000)

    result = verify_install.check_isa(mostly_clean)

    assert result.ok, "a large library with no EVEX sites is still clean"


def test_the_verifier_executes_the_runtime_rather_than_only_importing_it() -> None:
    """D36 applied to the verifier itself.

    A check that imports `llama_cpp` and declares victory passes on the broken
    wheel, because the AVX-512 library imports cleanly and only faults when it
    runs. So the inference check has to exist, and has to be part of the report
    rather than an optional extra.
    """
    names = {c.name for c in verify_install.CHECKS}
    assert "inference" in names, (
        "the verifier must actually execute llama.cpp; importing it is not proof "
        "(the AVX-512 wheel imports fine and dies on first token)"
    )


def test_the_verifier_reports_every_check_it_advertises() -> None:
    """A report that silently omits a failing check is worse than no report."""
    results = verify_install.run_all()
    reported = {r.name for r in results}
    assert reported == {c.name for c in verify_install.CHECKS}, (
        f"run_all() reported {sorted(reported)} but advertises "
        f"{sorted(c.name for c in verify_install.CHECKS)}"
    )


def test_a_failing_check_is_not_a_passing_check() -> None:
    """The shape every check must be able to take: a failure is reportable."""
    bad = verify_install.Result(name="synthetic", ok=False,
                                detail="deliberately failed")
    assert not bad.ok
    assert "deliberately failed" in bad.detail
