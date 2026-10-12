@echo off
REM Phase 0b: build llama-cpp-python from source with a portable CPU baseline.
REM The prebuilt wheel's ggml-cpu.dll is compiled with AVX-512, which a Ryzen 5
REM 5600G (Zen 3) cannot execute -> STATUS_ILLEGAL_INSTRUCTION. See DECISIONS.md D25.
REM
REM -j3 is deliberate: two uv processes for F:\Wan2GPT own the other cores right
REM now, and that work is not ours to disturb.

echo === Phase 0b source build starting ===
echo.

call "C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
if errorlevel 1 (
  echo FAILED: vcvars64.bat did not initialise the MSVC environment
  exit /b 1
)

where cl
where nvcc
cl 2>&1 | findstr /C:"Version"
nvcc --version
echo.

REM Three jobs, not six. See header.
set CMAKE_BUILD_PARALLEL_LEVEL=3

REM ATTEMPT 2 (see D29). The first attempt died in CMake configure with
REM   "No CUDA toolset found" at ggml/src/ggml-cuda/CMakeLists.txt:59 (enable_language(CUDA)).
REM Cause: CMake picked the *Visual Studio* generator, and that generator does not
REM drive nvcc itself - it looks for a "CUDA toolset" registered inside the VS
REM installation. nvcc being on PATH is not sufficient. That is why the log showed
REM "Found CUDAToolkit ... 13.3.73" immediately before failing.
REM Fix: the Ninja generator, which invokes nvcc directly and needs no VS CUDA
REM toolset. ninja.exe ships inside this same VS BuildTools install.
set VSCMD_ROOT=C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools
set "PATH=%VSCMD_ROOT%\Common7\IDE\CommonExtensions\Microsoft\CMake\Ninja;%PATH%"
set CMAKE_MAKE_PROGRAM=%VSCMD_ROOT%\Common7\IDE\CommonExtensions\Microsoft\CMake\Ninja\ninja.exe

REM Pin both compilers explicitly. "Newest on PATH" is how attempt 1 ended up with
REM nvcc 13.3 from a PATH that also listed 13.2, and an unpinned CUDA compiler
REM makes a build unreproducible for no benefit.
REM
REM Honour an already-pinned compiler, then CUDA_PATH, then the historical v13.3.
REM An installer (install.ps1 -WithBrain) sets CMAKE_CUDA_COMPILER to what it
REM detected, and this hard-coded v13.3 was silently overriding it on a machine
REM whose toolkit is v13.4 - the build would have failed on a path that no longer
REM exists, with an error that says nothing about a stale constant.
if not defined CMAKE_CUDA_COMPILER (
  if defined CUDA_PATH (
    if exist "%CUDA_PATH%\bin\nvcc.exe" (
      set CMAKE_CUDA_COMPILER=%CUDA_PATH%\bin\nvcc.exe
    ) else (
      echo FAILED: CUDA_PATH is set to "%CUDA_PATH%" but there is no bin\nvcc.exe there
      exit /b 1
    )
  ) else (
    set CMAKE_CUDA_COMPILER=C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v13.3\bin\nvcc.exe
  )
)
if not exist "%CMAKE_CUDA_COMPILER%" (
  echo FAILED: nvcc not found at "%CMAKE_CUDA_COMPILER%"
  echo   Set CUDA_PATH or CMAKE_CUDA_COMPILER and re-run.
  exit /b 1
)
echo === nvcc: %CMAKE_CUDA_COMPILER% ===

REM The fix for the original crash. GGML_NATIVE=OFF stops -march=native baking the
REM build machine's instruction set in; GGML_AVX512=OFF is explicit
REM belt-and-braces. AVX2 stays on: Zen 3 really has it.
REM
REM One CUDA architecture, not eight. The wheel ships sm_600..900; we need sm_86
REM for the RTX 3060 and nothing else. This is the single biggest lever on
REM compile time for this build.
REM
REM CMAKE_BUILD_TYPE=Release is NOT optional. Attempt 1 logged
REM "CMAKE_BUILD_TYPE=" (empty) because the VS generator is multi-config and
REM ignores it. Under Ninja an empty build type means an unoptimised build:
REM several times slower and far larger. Must be explicit.
set CMAKE_ARGS=-G Ninja -DCMAKE_BUILD_TYPE=Release -DGGML_CCACHE=OFF -DGGML_NATIVE=OFF -DGGML_AVX512=OFF -DGGML_AVX2=ON -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=86 -DGGML_CUDA_ARCHITECTURES=86 -DGGML_CUDA_FA_ALL_QUANTS=ON -DLLAMA_CURL=OFF -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_BUILD_SERVER=OFF

REM Project-local cache: the shared uv cache is 37.6 GB and is contended.
set UV_CACHE_DIR=%~dp0.uvcache

REM Relative to this script, not a hard-coded literal. install.ps1 calls this, and
REM a hard-coded G:\localaized meant the build ran in the wrong tree if the
REM checkout moved. D82 fixed the same class of staleness in the download script.
cd /d "%~dp0"

echo === cmake args: %CMAKE_ARGS% ===
echo === jobs: %CMAKE_BUILD_PARALLEL_LEVEL% ===
echo.

REM ===========================================================================
REM The build is a TRANSACTION and it lives in build_rollback.ps1, not here.
REM
REM `uv pip install --reinstall-package` removes the installed package BEFORE it
REM builds the replacement, so a build failing for any reason unrelated to the
REM build - a locked DLL, a full disk, a killed process - leaves a
REM half-uninstalled runtime. On 2026-10-13 that happened for real here: six
REM .py files of llama_cpp gone, every DLL present, import still fine,
REM verify_install.py still green, and 20 tests failing on an AttributeError
REM naming the TEST's own probe line. (D82.)
REM
REM It could not be written in batch. Measured, not assumed:
REM   * cmd mis-parses parenthesised blocks holding xcopy paths with trailing
REM     backslashes - the script ran to the end and printed not one restore line.
REM   * Rewritten with goto labels and proper CRLF endings, cmd STILL stopped
REM     executing the instant `uv` returned. Everything after it was skipped, so
REM     no restructuring inside this file could ever have worked.
REM
REM Hence exactly one line of batch follows, and it only propagates the code.
REM A rollback that silently does nothing is worse than none, because it is
REM believed - so if you add build logic below this line, verify it runs.
REM
REM -Root carries a trailing "." on purpose: %~dp0 ends in a backslash, and a
REM backslash immediately before a closing quote escapes it, so "-Root "%~dp0""
REM arrives in PowerShell as G:\localaized"\ - a mangled path.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_rollback.ps1" -Mode build -Root "%~dp0."
exit /b %ERRORLEVEL%