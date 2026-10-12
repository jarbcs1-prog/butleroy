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

    [string] $Package = 'llama-cpp-python==0.3.35'
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
    Say "snapshotted llama_cpp ($((Get-ChildItem $bkPkg -Recurse -File).Count) files)"
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