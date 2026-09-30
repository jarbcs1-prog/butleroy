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

REM The fix for the crash. GGML_NATIVE=OFF stops -march=native baking the build
REM machine's instruction set in; GGML_AVX512=OFF is the explicit belt-and-braces
REM so the result does not depend on a default. AVX2 stays on: Zen 3 really has it.
REM
REM One CUDA architecture, not eight. The wheel ships sm_600..900; we need sm_86
REM for the RTX 3060 and nothing else. This is the single biggest lever on
REM compile time for this build.
set CMAKE_ARGS=-DGGML_NATIVE=OFF -DGGML_AVX512=OFF -DGGML_AVX2=ON -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=86 -DGGML_CUDA_ARCHITECTURES=86 -DGGML_CUDA_FA_ALL_QUANTS=ON -DLLAMA_CURL=OFF -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_BUILD_SERVER=OFF

REM Project-local cache: the shared uv cache is 37.6 GB and is contended.
set UV_CACHE_DIR=G:\localaized\.uvcache

cd /d G:\localaized

echo === cmake args: %CMAKE_ARGS% ===
echo === jobs: %CMAKE_BUILD_PARALLEL_LEVEL% ===
echo.

uv pip install --python .venv\Scripts\python.exe --no-binary llama-cpp-python --reinstall-package llama-cpp-python llama-cpp-python==0.3.35
set BUILD_EXIT=%ERRORLEVEL%

echo.
echo === Phase 0b build finished with exit code %BUILD_EXIT% ===
exit /b %BUILD_EXIT%
