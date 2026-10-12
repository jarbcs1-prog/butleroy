#Requires -Version 5.1
<#
.SYNOPSIS
    Butleroy installer engine - the part that does the work.

.DESCRIPTION
    A GUI installer that shells out to PowerShell for the actual work is the norm,
    and it is the wrong shape here. The brain build takes ~20 minutes inside a
    vcvars64 shell with `CMAKE_CUDA_ARCHITECTURES=86` and a deliberately pinned
    nvcc (D29/D66), and a GUI wrapper around that hides every step and swallows
    every error. This engine is therefore a plain console script that an Inno
    Setup wrapper (or a human) invokes, and it is the *only* place install logic
    lives. The GUI adds a checkbox, not a second implementation.

    Two properties are load-bearing and are asserted by install_self_test.ps1:

      * Idempotent. Every step checks for its own postcondition before acting,
        so a second run is a no-op rather than a repair-by-damage.
      * Honest. It exits non-zero on failure and never reports success from a
        step it did not verify. `verify_install.py` is the real gate and this
        script's exit code tracks *it*, not its own optimism.

.PARAMETER Root
    Install root. Must be G:\localaized - policy.SELF_PATHS names that literal
    path, and moving the tree silently breaks the assistant's self-protection
    (D73). install_self_test.ps1 asserts this is refused anywhere else.

.PARAMETER WithBrain
    Build llama-cpp-python from source. Off by default: ~20 min, and the
    prebuilt wheel cannot execute on this CPU (D25).

.PARAMETER WithVoice
    Install the voice extras (piper TTS, faster-whisper STT). CPU-only.

.PARAMETER WithModel
    Download the fast-tier weights (~5.2 GB) via the repo's own verified script.

.PARAMETER WithAutostart
    Register the at-logon scheduled task. Persistent, so it asks.

.PARAMETER CudaPath
    CUDA toolkit root. Auto-detected from CUDA_PATH when omitted.

.PARAMETER SkipVerify
    Skip verify_install.py. For CI only - a successful install without it is a
    claim, not a result (see verify_install.py's module docstring).
#>
[CmdletBinding()]
param(
    [string] $Root        = 'G:\localaized',
    [string] $CudaPath,
    [switch] $WithBrain,
    [switch] $WithVoice,
    [switch] $WithModel,
    [switch] $WithAutostart,
    [switch] $SkipVerify
)

$ErrorActionPreference = 'Stop'
$script:Failed = $false
$script:Steps  = @()

function Write-Step  ([string]$m) { Write-Host "`n=== $m ===" -ForegroundColor Cyan }
function Write-Ok    ([string]$m) { Write-Host "  [ ok ] $m" -ForegroundColor Green }
function Write-Warn2 ([string]$m) { Write-Host "  [warn] $m" -ForegroundColor Yellow }
function Write-Err   ([string]$m) { Write-Host "  [FAIL] $m" -ForegroundColor Red }
function Write-Info  ([string]$m) { Write-Host "  [info] $m" -ForegroundColor Gray }

function Add-Result {
    param([string]$Name, [string]$Status, [string]$Detail)
    $script:Steps += [pscustomobject]@{ Step = $Name; Status = $Status; Detail = $Detail }
}

# ---------------------------------------------------------------------------
# 0. The install root is not a preference.
# ---------------------------------------------------------------------------

# policy.SELF_PATHS is (G:\localaized\src, ...DECISIONS.md, ...ARCHITECTURE.md,
# ...PLAN.md) and it is what makes "the assistant cannot rewrite its own code"
# structural rather than promised (D21). If the tree is installed anywhere else,
# the deny-list names a directory that does not exist and self-protection stops
# applying to the assistant's own source - and *nothing fails*, because the tests
# that assert the refusal compare a string to the same string. This is exactly the
# failure D73 was written about.
$SELF_ROOT = 'G:\localaized'

function Assert-InstallRoot {
    $full = [System.IO.Path]::GetFullPath($Root).TrimEnd('\')
    if ($full -ine $SELF_ROOT) {
        Write-Err "Install root must be '$SELF_ROOT', not '$full'."
        Write-Info "policy.SELF_PATHS names that literal path; installing anywhere"
        Write-Info "else would silently disable the assistant's self-protection (D73)."
        Write-Info "Making it dynamic is a deliberate change to a security-critical"
        Write-Info "constant, not something an installer may decide."
        return $false
    }
    return $true
}

# ---------------------------------------------------------------------------
# 1. Prerequisites. Checked, not assumed: twice in this project's history a
#    "missing toolchain" was really "the toolchain looked installed" (D2/D29).
# ---------------------------------------------------------------------------

function Resolve-Cuda {
    if ($CudaPath -and (Test-Path $CudaPath)) { return $CudaPath }
    $m = [Environment]::GetEnvironmentVariable('CUDA_PATH', 'Machine')
    if ($m -and (Test-Path $m)) { return $m }
    $u = [Environment]::GetEnvironmentVariable('CUDA_PATH', 'User')
    if ($u -and (Test-Path $u)) { return $u }
    return $null
}

function Test-Prereqs {
    Write-Step 'Prerequisites'
    $missing = @()

    # uv: the venv here has no pip at all, which reads like a broken venv rather
    # than a deliberate choice.
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if (-not $uv) {
        $missing += 'uv  (https://docs.astral.sh/uv/)'
        Write-Err 'uv not on PATH'
    } else { Write-Ok "uv $((& uv --version) -replace '^uv ', '')" }

    # Python 3.11.x only: tomllib and the tier dataclasses need it, and
    # pyproject pins requires-python = ">=3.11,<3.12".
    $py = Get-Command python -ErrorAction SilentlyContinue
    $pyOk = $false
    if ($py) {
        $v = & $py.Source --version 2>&1
        if ($v -match '3\.11\.') { $pyOk = $true; Write-Ok "system python $v" }
        else { Write-Warn2 "system python is $v - a 3.11.x is required (pyproject pins <3.12)" }
    }
    if (-not $pyOk) {
        $cand = @('C:\Program Files\Python311\python.exe',
                  'C:\Python311\python.exe') | Where-Object { Test-Path $_ } | Select-Object -First 1
        if ($cand) { Write-Ok "found 3.11 at $cand (will use it for the venv)" }
        else { $missing += 'Python 3.11.x 64-bit' }
    }

    # CUDA: needed to *build* the brain and to load it at runtime (D26). A machine
    # with no toolkit gets an import error that names llama.dll rather than the
    # cublas dependency that is actually missing - D43 again.
    $cuda = Resolve-Cuda
    if ($cuda) {
        Write-Ok "CUDA toolkit: $cuda"
        $script:CudaRoot = $cuda
    } else {
        Write-Warn2 'CUDA toolkit not found (CUDA_PATH unset and not in the default location)'
        if ($WithBrain) { $missing += 'CUDA toolkit (required to build the brain)' }
    }

    if ($WithBrain) {
        # vcvars64: MSVC. Probed by existence - actually initialising it is what
        # the build script does, and a probe that initialises it here would leave
        # this shell's environment mutated.
        $vc = @(
            'C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools\VC\Auxiliary\Build\vcvars64.bat'
            'C:\Program Files\Microsoft Visual Studio\18\BuildTools\VC\Auxiliary\Build\vcvars64.bat'
            'C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat'
            'C:\Program Files\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat'
        ) | Where-Object { Test-Path $_ } | Select-Object -First 1
        if ($vc) { Write-Ok "MSVC: $vc" } else { $missing += 'Visual Studio Build Tools (MSVC)' }

        $ninja = Get-ChildItem 'C:\Program Files*\Microsoft Visual Studio\*\BuildTools\Common7\IDE\CommonExtensions\Microsoft\CMake\Ninja\ninja.exe' `
                    -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($ninja) { Write-Ok "Ninja: $($ninja.FullName)" }
        else { $missing += 'Ninja (ships inside VS BuildTools)' }

        $nvcc = if ($cuda) { Join-Path $cuda 'bin\nvcc.exe' } else { $null }
        if ($nvcc -and (Test-Path $nvcc)) { Write-Ok "nvcc: $nvcc" }
        else { $missing += 'nvcc (CUDA toolkit)' }
    }

    if ($missing.Count -gt 0) {
        Write-Host ''
        Write-Err "Missing prerequisites:"
        $missing | ForEach-Object { Write-Host "        - $_" -ForegroundColor Red }
        return $false
    }
    Write-Ok 'all prerequisites present'
    return $true
}

# ---------------------------------------------------------------------------
# 2. The checkout and the venv
# ---------------------------------------------------------------------------

function Test-SourceTree {
    Write-Step 'Source tree'
    if (-not (Test-Path $Root)) { Write-Err "no checkout at $Root"; return $false }
    $need = @('src\assistant\daemon.py', 'pyproject.toml', 'verify_install.py', 'install.ps1')
    $miss = $need | Where-Object { -not (Test-Path (Join-Path $Root $_)) }
    if ($miss.Count -gt 0) {
        Write-Err "checkout at $Root is incomplete; missing: $($miss -join ', ')"
        return $false
    }
    Write-Ok "checkout present at $Root"
    return $true
}

function Install-Venv {
    Write-Step 'Virtual environment'
    $venvPy = Join-Path $Root '.venv\Scripts\python.exe'
    $venvOk = $false
    if (Test-Path $venvPy) {
        $v = & $venvPy --version 2>&1
        if ($v -match '3\.11\.') {
            $prefix = & $venvPy -c "import sys; print(sys.prefix)" 2>&1
            Write-Ok "existing venv works: $v (prefix $prefix)"
            $venvOk = $true
        } else {
            Write-Warn2 "existing .venv\Scripts\python.exe is broken: $v"
            Write-Info 'recreating it - the site-packages are rebuilt, nothing else is touched'
        }
    }
    if (-not $venvOk) {
        Write-Info 'creating .venv with uv'
        $ErrorActionPreference = 'Continue'
        & uv venv --python 3.11 (Join-Path $Root '.venv') 2>&1 | Out-Null
        $venvExit = $LASTEXITCODE
        $ErrorActionPreference = 'Stop'
        if ($venvExit -ne 0) { Write-Err "uv venv failed ($venvExit)"; return $false }
    }
    # Postcondition: the interpreter starts and reports the venv prefix. A venv
    # directory that exists is not a venv that works - the uv trampoline in this
    # repo pointed at another user's profile and failed exactly that way.
    $probe = & $venvPy -c "import sys; assert sys.prefix.lower().endswith('.venv'); print('ok')" 2>&1
    if ($probe -notmatch 'ok') { Write-Err "venv unusable after creation: $probe"; return $false }
    Write-Ok 'venv verified'
    Add-Result 'venv' 'ok' $v
    return $true
}

function Install-Package {
    param([switch]$Brain, [switch]$Voice)
    Write-Step 'Package'
    $venvPy = Join-Path $Root '.venv\Scripts\python.exe'

    # Core only by default. Plane A - daemon, gate, arbiter, audit, pipes - is
    # pure stdlib, and that is what keeps 24/7 resident cost at a few hundred MB
    # with no VRAM (D1). The always-on process must never pull a model runtime:
    # verify_install.py's `imports` check fails the install if it does.
    $spec = '.'
    if ($Brain)  { $spec = '.[brain]' }
    if ($Voice)  { $spec = '.[voice]' }
    if ($Brain -and $Voice) { $spec = '.[brain,voice]' }

    Write-Info "uv pip install -e `"$spec`"  (core is pystray + pillow only)"
    # $ErrorActionPreference = 'Stop' makes a native command that writes to stderr
    # raise a terminating NativeCommandError, even on success - uv prints progress
    # on stderr. That turned a successful install into a failure with the message
    # "Resolved 4 packages". Redirecting stderr into stdout and reading
    # $LASTEXITCODE is what actually distinguishes the two.
    $ErrorActionPreference = 'Continue'
    $out = & uv pip install --python $venvPy -e $spec 2>&1
    $code = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
    $out | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
    if ($code -ne 0) { Write-Err "uv pip install failed ($code)"; return $false }
    Write-Ok 'package installed (editable)'
    Add-Result 'package' 'ok' $spec
    return $true
}

# ---------------------------------------------------------------------------
# 3. The brain build. Off by default, and never from a wheel.
# ---------------------------------------------------------------------------

function Install-Brain {
    Write-Step 'Brain (llama-cpp-python, source build)'
    if (-not $CudaRoot) { Write-Err 'no CUDA toolkit; cannot build'; return $false }
    $venvPy = Join-Path $Root '.venv\Scripts\python.exe'

    # The warning is not decoration. The prebuilt wheel imports cleanly and then
    # dies with STATUS_ILLEGAL_INSTRUCTION on this CPU, because its ggml-cpu.dll
    # is compiled with AVX-512 that a Ryzen 5 5600G (Zen 3) cannot execute (D25).
    # A user who skips this step gets a daemon that starts and cannot answer.
    Write-Host ''
    Write-Host '  WHY THIS IS NOT OPTIONAL AND NOT FROM A WHEEL:' -ForegroundColor Yellow
    Write-Host '  The prebuilt llama-cpp-python wheel ships a ggml-cpu.dll compiled' -ForegroundColor Yellow
    Write-Host '  with AVX-512. A Ryzen 5 5600G (Zen 3) has none of it, so that' -ForegroundColor Yellow
    Write-Host '  library imports perfectly and then dies with STATUS_ILLEGAL_' -ForegroundColor Yellow
    Write-Host '  INSTRUCTION on the first real instruction - on CPU and CUDA' -ForegroundColor Yellow
    Write-Host '  alike. It must be built from source. (DECISIONS.md D25)' -ForegroundColor Yellow
    Write-Host '  Roughly 20 minutes.' -ForegroundColor Yellow
    Write-Host ''

    $bat = Join-Path $Root 'build_phase0b.bat'
    if (-not (Test-Path $bat)) { Write-Err "missing $bat"; return $false }

    # The tracked script pins CUDA v13.3 and this repo path. Rather than duplicate
    # its logic here (two implementations of a build is exactly the D61 failure:
    # "a second copy of a test is not a belt and braces"), the engine calls it and
    # passes the detected paths through the environment the script already reads.
    $env:CMAKE_CUDA_COMPILER = Join-Path $CudaRoot 'bin\nvcc.exe'
    $env:VSCMD_ROOT = Split-Path (Split-Path (Split-Path (Split-Path $vcvars 2>$null)))
    Write-Info "nvcc pinned to $($env:CMAKE_CUDA_COMPILER)"

    Push-Location $Root
    try {
        Write-Info 'running build_phase0b.bat - expect ~20 minutes'
        # Same stderr-raises-on-success trap as the package step.
        $ErrorActionPreference = 'Continue'
        & cmd /c "`"$bat`"" 2>&1 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
        $code = $LASTEXITCODE
        $ErrorActionPreference = 'Stop'
    } finally { Pop-Location }

    if ($code -ne 0) {
        Write-Err "brain build failed ($code). See build_phase0b.log in $Root"
        Write-Info 'a first attempt failing is normal; attempt 1 of the original build'
        Write-Info 'is kept for that reason (D29). Check the log before retrying.'
        return $false
    }
    Write-Ok 'brain built from source'
    Add-Result 'brain' 'ok' 'source build'
    return $true
}

# ---------------------------------------------------------------------------
# 4. Weights. 13 GB is not something an installer ships.
# ---------------------------------------------------------------------------

function Install-Model {
    Write-Step 'Weights'
    $models = Join-Path $Root 'models'
    if (-not (Test-Path $models)) { New-Item -ItemType Directory -Path $models -Force | Out-Null }

    # Already present and non-trivial? Then this is the idempotent path and the
    # 4.2 GB download is skipped.
    $gguf = Get-ChildItem $models -Filter '*.gguf' -ErrorAction SilentlyContinue
    if ($gguf -and ($gguf | Where-Object { $_.Length -gt 50MB })) {
        $tot = ($gguf | Measure-Object -Property Length -Sum).Sum
        Write-Ok ("weights already present: {0} file(s), {1:N1} GB" -f $gguf.Count, ($tot/1GB))
        Add-Result 'models' 'ok' 'already present'
        return $true
    }

    $script = Join-Path $Root 'download_fast_tier.ps1'
    if (-not (Test-Path $script)) {
        Write-Err "missing $script"
        Write-Info 'weights are ~5.2 GB and are not in git; see INSTALL.md step 6'
        return $false
    }

    Write-Info 'downloading the fast tier (~4.2 GB + projector). Verified byte counts.'
    $sw = [Diagnostics.Stopwatch]::StartNew()
    Push-Location $Root
    try {
        $ErrorActionPreference = 'Continue'
        & $script 2>&1 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
    } finally { Pop-Location; $ErrorActionPreference = 'Stop' }
    $sw.Stop()

    $gguf = Get-ChildItem $models -Filter '*.gguf' -ErrorAction SilentlyContinue
    if (-not ($gguf | Where-Object { $_.Length -gt 50MB })) {
        Write-Err "download produced no usable weights after {0:N0}s" -f $sw.Elapsed.TotalSeconds
        return $false
    }
    Write-Ok ("weights present: {0} file(s), {1:N1} GB in {2:N0}s" -f $gguf.Count, `
              (($gguf | Measure-Object -Property Length -Sum).Sum/1GB), $sw.Elapsed.TotalSeconds)
    Add-Result 'models' 'ok' "$($gguf.Count) gguf"
    return $true
}

# ---------------------------------------------------------------------------
# 5. Autostart. Persistent, so it is opt-in and never silent.
# ---------------------------------------------------------------------------

function Install-Autostart {
    Write-Step 'Autostart'
    $TaskName = 'Butleroy'
    $pythonw = Join-Path $Root '.venv\Scripts\pythonw.exe'

    if (-not (Test-Path $pythonw)) {
        Write-Err "missing $pythonw - cannot register a launcher that does not exist"
        return $false
    }
    if (-not $pythonw.EndsWith('pythonw.exe')) {
        Write-Err 'must be pythonw.exe, not python.exe'
        return $false
    }

    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Write-Ok "task '$TaskName' already registered - left alone"
        Add-Result 'autostart' 'ok' 'already registered'
        return $true
    }

    # At-logon, not at-startup: the tray and the tkinter window need an
    # interactive session and a service session has none.
    # ExecutionTimeLimit = 0: the 3-day default would kill a 24/7 daemon, which
    # is the silent-death mode D64 was about.
    $action  = New-ScheduledTaskAction -Execute $pythonw -Argument '-m assistant' -WorkingDirectory $Root
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
              -ExecutionTimeLimit ([TimeSpan]::Zero)
    try {
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
                               -Settings $set -ErrorAction Stop | Out-Null
    } catch {
        Write-Err "Register-ScheduledTask failed: $($_.Exception.Message)"
        Write-Info 'this needs elevation; run it from an elevated shell, or register it'
        Write-Info 'by hand from INSTALL.md step 9'
        return $false
    }
    Write-Ok "registered '$TaskName' (at logon, pythonw, no time limit)"
    Add-Result 'autostart' 'ok' 'registered'
    return $true
}

# ---------------------------------------------------------------------------
# 6. The real gate.
# ---------------------------------------------------------------------------

function Invoke-Verify {
    Write-Step 'Verifying the install'
    if ($SkipVerify) {
        Write-Warn2 'verify_install.py SKIPPED - this install is unverified'
        Add-Result 'verify' 'warn' 'skipped'
        return $true
    }
    $venvPy = Join-Path $Root '.venv\Scripts\python.exe'
    if (-not (Test-Path (Join-Path $Root 'verify_install.py'))) {
        Write-Err 'verify_install.py not found; refusing to call this a success'
        Add-Result 'verify' 'fail' 'verifier missing'
        return $false
    }

    # CUDA on the child's environment, not just this process. The loader reads
    # os.environ at import time, so a venv that works in one shell and not the
    # next is a shell whose PATH predates the toolkit install (D81).
    if ($CudaRoot) {
        $env:CUDA_PATH = $CudaRoot
        $env:PATH = "$CudaRoot\bin;$CudaRoot\bin\x64;" + $env:PATH
    }

    Write-Info 'running verify_install.py - this executes a model on the GPU'
    # Same stderr trap. llama.cpp prints its own diagnostics on stderr, so this
    # would otherwise abort the install at the very step that proves it works.
    $ErrorActionPreference = 'Continue'
    & $venvPy (Join-Path $Root 'verify_install.py') 2>&1 |
        ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
    $code = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'

    if ($code -ne 0) {
        Write-Err "verify_install.py exited $code"
        Write-Info 'the install is NOT working. See INSTALL.md and the lines above.'
        Add-Result 'verify' 'fail' "exit $code"
        return $false
    }
    Write-Ok 'verify_install.py exit 0 - all seven checks pass'
    Add-Result 'verify' 'ok' 'exit 0'
    return $true
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

function Show-Summary {
    Write-Host ''
    Write-Host '  Summary' -ForegroundColor White
    Write-Host '  -------' -ForegroundColor White
    foreach ($s in $script:Steps) {
        $col = switch ($s.Status) { 'ok' { 'Green' } 'warn' { 'Yellow' } default { 'Red' } }
        $mark = switch ($s.Status) { 'ok' { '[ ok ]' } 'warn' { '[warn]' } default { '[FAIL]' } }
        Write-Host ("  {0} {1,-12} {2}" -f $mark, $s.Step, $s.Detail) -ForegroundColor $col
    }
}

Write-Host ''
Write-Host '  Butleroy installer' -ForegroundColor White
Write-Host '  LocalAIzed Concierge for Windows' -ForegroundColor DarkGray
Write-Host '  ================================' -ForegroundColor DarkGray
Write-Host ''
Write-Host "  Root      : $Root"
Write-Host "  Brain     : $(if ($WithBrain) { 'build from source (~20 min)' } else { 'no - see the warning below' })"
Write-Host "  Voice     : $(if ($WithVoice) { 'yes' } else { 'no' })"
Write-Host "  Model     : $(if ($WithModel) { 'download fast tier' } else { 'no' })"
Write-Host "  Autostart : $(if ($WithAutostart) { 'register at-logon task' } else { 'no' })"
Write-Host ''

if (-not (Assert-InstallRoot)) { exit 2 }

$ok = $true
foreach ($step in @(
    @{ Name = 'prereqs';  Fn = { Test-Prereqs } },
    @{ Name = 'tree';     Fn = { Test-SourceTree } },
    @{ Name = 'venv';     Fn = { Install-Venv } },
    @{ Name = 'package';  Fn = { Install-Package -Brain:$WithBrain -Voice:$WithVoice } },
    @{ Name = 'brain';    Fn = { if ($WithBrain) { Install-Brain } else { Write-Step 'Brain'; Write-Info 'skipped - the daemon will run without a model until you build it'; Add-Result 'brain' 'warn' 'not built'; $true } } },
    @{ Name = 'models';   Fn = { if ($WithModel) { Install-Model } else { Write-Step 'Weights'; Write-Info 'skipped'; Add-Result 'models' 'warn' 'not downloaded'; $true } } },
    @{ Name = 'autostart';Fn = { if ($WithAutostart) { Install-Autostart } else { Write-Step 'Autostart'; Write-Info 'skipped - not requested'; Add-Result 'autostart' 'warn' 'not requested'; $true } } },
    @{ Name = 'verify';   Fn = { Invoke-Verify } }
)) {
    if (-not (& $step.Fn)) {
        $ok = $false
        if ($step.Name -in @('prereqs', 'tree', 'venv')) {
            # These have no meaningful continuation: without a venv there is no
            # runtime, and without prerequisites the rest would fail confusingly.
            Add-Result $step.Name 'fail' 'blocking'
            break
        }
        Add-Result $step.Name 'fail' ''
    }
}

Show-Summary
Write-Host ''
if ($ok) {
    Write-Host '  INSTALLED.' -ForegroundColor Green
    Write-Host ''
    Write-Host "  Start it:  & '$Root\.venv\Scripts\python.exe' -m assistant"
    Write-Host "  Preflight: & '$Root\.venv\Scripts\python.exe' -m assistant --check"
    Write-Host ''
    if (-not $WithBrain) {
        Write-Host '  Note: no brain was built, so the assistant cannot answer questions.' -ForegroundColor Yellow
        Write-Host '  Re-run with -WithBrain on this machine (see the AVX-512 warning).' -ForegroundColor Yellow
    }
    exit 0
} else {
    Write-Host '  NOT fully installed. See the FAIL lines above.' -ForegroundColor Red
    Write-Host '  Nothing was rolled back - the steps that succeeded are idempotent.' -ForegroundColor DarkGray
    exit 1
}