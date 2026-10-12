#Requires -Version 5.1
<#
.SYNOPSIS
    Run the llama-cpp-python source build as a transaction, with rollback.

.DESCRIPTION
    WHY THIS IS POWERSHELL AND NOT LINES IN build_phase0b.bat
    ---------------------------------------------------

    `uv pip install --reinstall-package` removes the installed package BEFORE it
    builds the replacement, so a build failing for any reason unrelated to the
    build leaves a half-uninstalled runtime. That is not hypothetical: on
    2026-10-13 build_phase0b.bat ran against a live venv with pytest going, and
    failed on `Access is denied` for ggml-base.dll. Six .py files of llama_cpp
    were gone, every DLL still present, `import llama_cpp` still succeeding, and
    verify_install.py passing every check it had. Twenty tests then failed on an
    AttributeError naming the *test's* own probe line.

    So the rollback has to exist. It could not live in the batch file. Measured,
    not guessed:

      * cmd mis-parses parenthesised blocks containing xcopy paths with trailing
        backslashes. The script ran to the end, exited with the build's own code,
        and printed not one restore line. A rollback that silently does nothing
        is worse than none, because it is believed.
      * Rewritten with goto labels and CRLF endings, cmd STILL stopped executing
        immediately after the `uv` line. Every statement after it was skipped, so
        no amount of restructuring inside the batch was going to work. Only one
        line of the batch is therefore now allowed to follow `uv`: the exit code.

    The transaction lives here so the batch has exactly one cmd->PowerShell
    boundary and no post-build logic at all.

.EXAMPLE
    powershell -File build_rollback.ps1 -Mode build -Root G:\localaized

.NOTES
    -Root is REQUIRED. $PSScriptRoot as a parameter default is evaluated too early
    under `powershell -File` and arrives empty, which made every derived path
    empty and raised "Cannot bind argument to parameter 'Path' because it is an
    empty string". A defaulted-and-hoped-for path is how a rollback becomes a no-op.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('snapshot', 'restore', 'cleanup', 'build')]
    [string] $Mode,

    [Parameter(Mandatory = $true)]
    [string] $Root,

    [string] $Package = 'llama-cpp-python==0.3.35',

    [switch] $NoCache
)

$ErrorActionPreference = 'Stop'

$site   = Join-Path $Root '.venv\Lib\site-packages'
$pkg    = Join-Path $site 'llama_cpp'
$dist   = Join-Path $site 'llama_cpp_python-0.3.35.dist-info'
$backup = Join-Path $Root '.uvcache\llama_backup'
$bkPkg  = Join-Path $backup 'llama_cpp'
$bkDist = Join-Path $backup 'dist-info'
$venvPy = Join-Path $Root '.venv\Scripts\python.exe'

function Say([string] $m) { Write-Host "  [rollback] $m" }

# Same measurement and same 10% limit as probe_isa.py and verify_install.py's
# `isa` check, so the three agree rather than three tools disagreeing.
$script:EvexLimitPct = 10.0

function Get-EvexRatio([string] $Path) {
    $data = [IO.File]::ReadAllBytes($Path)
    $bare = 0; $evex = 0
    for ($i = 0; $i -lt $data.Length; $i++) {
        if ($data[$i] -ne 0x62) { continue }
        $bare++
        if ($i + 2 -ge $data.Length) { continue }
        $p0 = $data[$i + 1]; $p1 = $data[$i + 2]
        if (($p0 -band 0x08) -eq 0 -and ($p0 -band 0x07) -in @(1, 2, 3) -and ($p1 -band 0x08) -ne 0) { $evex++ }
    }
    if ($bare -eq 0) { return $null }
    return ($evex / $bare) * 100
}

function Test-PoisonedRuntime {
    # True when the installed ggml-cpu.dll looks like the prebuilt AVX-512 wheel.
    $dll = Join-Path $site 'llama_cpp\lib\ggml-cpu.dll'
    if (-not (Test-Path $dll)) { return $null }
    $pct = Get-EvexRatio $dll
    if ($null -eq $pct) { return $null }
    return [bool] ($pct -gt $script:EvexLimitPct)
}

function Write-PoisonWarning([double] $pct) {
    Write-Host ''
    Write-Host '  ################################################################'
    Write-Host '  # THE RUNTIME YOU JUST RESTORED IS THE PREBUILT AVX-512 WHEEL. #'
    Write-Host '  # It imports cleanly and then dies on first token with         #'
    Write-Host '  # STATUS_ILLEGAL_INSTRUCTION on this Zen 3 CPU. See D25.      #'
    Write-Host '  # This counts as restored, but it is NOT a working brain.      #'
    Write-Host '  #                                                              #'
    Write-Host '  # Build it properly:                                          #'
    Write-Host '  #   cd G:\localaized                                           #'
    Write-Host '  #   $env:BUTLEROY_FORCE_REBUILD = ''1''                         #'
    Write-Host '  #   .\build_phase0b.bat                                        #'
    Write-Host '  ################################################################'
    Write-Host ''
}

function Save-Snapshot {
    if (Test-Path $backup) { Remove-Item $backup -Recurse -Force }
    New-Item -ItemType Directory -Path $backup -Force | Out-Null

    if (-not (Test-Path $pkg)) {
        Say 'no llama_cpp installed - nothing to snapshot. This is a first build,'
        Say 'so a failure will leave no runtime, exactly as before the build.'
        return $false
    }
    Copy-Item $pkg $bkPkg -Recurse -Force
    if (Test-Path $dist) { Copy-Item $dist $bkDist -Recurse -Force }

    # Know what we just saved. On a FRESH install the only llama_cpp present is
    # the wheel that `.[brain]` pulled, and restoring that is restoring a
    # runtime that cannot run. Recording it here means Restore-Snapshot can say
    # so out loud instead of reporting a cheerful success.
    $poisoned = Test-PoisonedRuntime
    if ($poisoned) {
        $pct = Get-EvexRatio (Join-Path $site 'llama_cpp\lib\ggml-cpu.dll')
        Say ("snapshot is the PREBUILT AVX-512 WHEEL ({0:N2}% EVEX > {1:N0}% limit)." -f $pct, $script:EvexLimitPct)
        Say 'A rollback would restore a runtime that cannot execute on this CPU.'
        Set-Content (Join-Path $backup 'POISONED') $pct
    } else {
        Say "snapshotted llama_cpp ($((Get-ChildItem $bkPkg -Recurse -File).Count) files)"
    }
    return $true
}

function Restore-Snapshot {
    if (-not (Test-Path (Join-Path $bkPkg '__init__.py'))) {
        Say 'nothing to restore - the venv has no llama_cpp, as before the build.'
        return $false
    }
    # Remove whatever the interrupted build left, THEN put the snapshot back.
    # Both halves matter: restoring over a partial tree leaves the partial files
    # in place, which is how a "restored" install stays broken.
    if (Test-Path $pkg) { Remove-Item $pkg -Recurse -Force }
    Copy-Item $bkPkg $pkg -Recurse -Force
    if (Test-Path $bkDist) {
        if (Test-Path $dist) { Remove-Item $dist -Recurse -Force }
        Copy-Item $bkDist $dist -Recurse -Force
    }
    Say "restored llama_cpp ($((Get-ChildItem $pkg -Recurse -File).Count) files). Verify with: .venv\Scripts\python.exe verify_install.py"

    # A restore is only a success if what came back can actually run. During a
    # fresh install the snapshot is the AVX-512 wheel, so "restored 38 files"
    # would be true and useless. Re-measure rather than trust the copy.
    $wasPoisoned = Test-Path (Join-Path $backup 'POISONED')
    if ($wasPoisoned) {
        $pct = Get-Content (Join-Path $backup 'POISONED')
        Write-PoisonWarning ([double] $pct)
    } else {
        $now = Test-PoisonedRuntime
        if ($now) {
            $pct = Get-EvexRatio (Join-Path $site 'llama_cpp\lib\ggml-cpu.dll')
            Write-PoisonWarning $pct
        } else {
            Say 'restored runtime measures clean (under the AVX-512 limit).'
        }
    }
    return $true
}

function Remove-Snapshot {
    if (Test-Path $backup) { Remove-Item $backup -Recurse -Force }
    Say 'snapshot removed'
}

switch ($Mode) {
    'snapshot' { $null = Save-Snapshot; exit 0 }
    'restore'  { $null = Restore-Snapshot; exit 0 }
    'cleanup'  { Remove-Snapshot; exit 0 }

    'build' {
        Push-Location $Root
        try {
            $had = Save-Snapshot

            # A cache hit here is silent and total. uv caches the *built wheel*, so
            # a second identical build resolves and installs in about four seconds
            # and exits 0 without invoking a compiler once. Measured 2026-10-13:
            # "Prepared 1 package in 11ms", every DLL byte-identical afterwards.
            # That reads as a successful build and is not one.
            #
            # Risk to be aware of when trusting a cached wheel: uv's cache key is
            # derived from the source and its build requirements. The CMake flags
            # in CMAKE_ARGS (GGML_NATIVE, CMAKE_CUDA_ARCHITECTURES, AVX512 off)
            # are ours, not llama-cpp-python's, so whether they participate in that
            # key is UNVERIFIED here. If they do not, a cached wheel can be reused
            # across a change of build flags - which would reintroduce exactly the
            # D25 AVX-512 failure a flags change is meant to prevent.
            if ($NoCache) {
                $env:UV_NO_CACHE = '1'
                Say 'cache DISABLED (UV_NO_CACHE=1). Expect ~20 min of real compiling.'
            } else {
                Say 'cache enabled - if this finishes in seconds, no compilation happened.'
                Say 'Use -NoCache to force a genuine rebuild.'
            }

            Write-Host '  [build] compiling llama-cpp-python from source (this takes ~20 min)'
            & uv pip install --python $venvPy --no-binary llama-cpp-python `
                --reinstall-package llama-cpp-python $Package
            $code = $LASTEXITCODE

            if ($code -eq 0) {
                Remove-Snapshot
                Say "build succeeded (exit 0)."
                exit 0
            }

            Write-Host ''
            Write-Host "=== build FAILED, exit $code - restoring the previous runtime ==="
            if ($had) {
                $null = Restore-Snapshot
            } else {
                Say 'there was no previous runtime to restore.'
            }
            Remove-Snapshot
            exit $code
        }
        finally {
            Pop-Location
        }
    }
}