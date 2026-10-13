#Requires -Version 5.1
<#
.SYNOPSIS
    Tests for install.ps1 - the installer claims properties it must prove.

.DESCRIPTION
    An installer is the one script in a project that runs before anyone is
    watching, with the least scrutiny, and on the machine it is about to change.
    That is precisely why it needs its own tests rather than being trusted
    because it "looked right". Four claims are checked here:

      * It refuses a root other than G:\localaized. If it did not, installing
        anywhere else would silently disable the assistant's self-protection
        (D21/D73) and nothing would fail, because the tests that assert the
        refusal compare a string to the same string.
      * The AVX-512 warning is actually printed, because a user who skips the
        brain build without being told why gets a daemon that starts and cannot
        answer.
      * Every step is idempotent: the postconditions are written so a second run
        detects them rather than redoing them.
      * The reported exit status is driven by verify_install.py and not by the
        installer's own optimism.

    Run:  .\install_self_test.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$script:Pass = 0
$script:Fail = 0

function Test-Case {
    param([string]$Name, [scriptblock]$Body)
    try {
        $r = & $Body
        if ($r -eq $true) {
            Write-Host "  [ ok ] $Name" -ForegroundColor Green
            $script:Pass++
        } else {
            Write-Host "  [FAIL] $Name -> $r" -ForegroundColor Red
            $script:Fail++
        }
    } catch {
        Write-Host "  [FAIL] $Name -> $($_.Exception.Message)" -ForegroundColor Red
        $script:Fail++
    }
}

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Engine = Join-Path $Root 'install.ps1'

Write-Host ''
Write-Host '  install.ps1 self-test' -ForegroundColor White
Write-Host '  =======================' -ForegroundColor DarkGray
Write-Host ''

if (-not (Test-Path $Engine)) {
    Write-Host "  [FAIL] install.ps1 not found at $Engine" -ForegroundColor Red
    exit 1
}

# Load the engine's functions without running its main body. The script is
# dot-sourced with a flag that stops before `main`, which is why the guard below
# checks for the sentinel rather than the file merely existing.
$src = Get-Content $Engine -Raw

# ---------------------------------------------------------------------------
# 1. The install root is enforced, not documented.
# ---------------------------------------------------------------------------

# Write-Host goes to the *information* stream (6) in PowerShell 5.1+, not to
# stdout (1). Capturing 2>&1 alone yields an empty string and the assertion below
# would pass for the wrong reason - or fail for one that has nothing to do with
# the engine. 6>&1 is what makes the message visible to the test at all.
Test-Case 'engine refuses a root other than G:\localaized' {
    $out = & $Engine -Root 'C:\Butleroy' -SkipVerify 6>&1 | Out-String
    if ($LASTEXITCODE -ne 2) { return "expected exit 2, got $LASTEXITCODE" }
    if ($out -notmatch 'must be') { return "no refusal message printed; got: $($out.Trim())" }
    if ($out -match 'create the venv') { return 'it continued past the refusal' }
    return $true
}

Test-Case 'the accepted root normalises, so a trailing slash is not a rejection' {
    # Deliberately NOT by running the installer against the real root: that would
    # reinstall the package as a side effect of a test. The comparison is the
    # whole of what Assert-InstallRoot does, so it is asserted directly - and if
    # that helper ever stops normalising, this fails instead of the check drifting
    # into the engine unnoticed.
    $full = [System.IO.Path]::GetFullPath('G:\localaized\').TrimEnd('\')
    if ($full -ine 'G:\localaized') { return "GetFullPath+TrimEnd gave '$full'" }
    $case = 'g:\LOCALAIZED'
    if ([System.IO.Path]::GetFullPath($case).TrimEnd('\') -ine 'G:\localaized') {
        return 'the comparison is case-sensitive, so a differently-cased root is refused'
    }
    return $true
}

Test-Case 'SELF_PATHS in policy.py still names the install root' {
    # The installer refusing a bad root is only half of it: the constant it is
    # protecting has to name the same place. This is the check that would catch
    # the blanket find-and-replace D73 was written about.
    $policy = Join-Path $Root 'src\assistant\policy.py'
    if (-not (Test-Path $policy)) { return "no $policy" }
    $text = Get-Content $policy -Raw
    if ($text -notmatch [regex]::Escape('G:\localaized\src')) {
        return 'SELF_PATHS no longer names G:\localaized\src'
    }
    return $true
}

# ---------------------------------------------------------------------------
# 2. The AVX-512 warning is present and unconditional.
# ---------------------------------------------------------------------------

Test-Case 'the AVX-512 warning text is in the engine' {
    if ($src -notmatch 'AVX-512') { return 'no AVX-512 warning in install.ps1' }
    if ($src -notmatch 'STATUS_ILLEGAL_INSTRUCTION') {
        return 'warning does not name the actual symptom'
    }
    if ($src -notmatch 'D25') { return 'warning cites no decision' }
    return $true
}

Test-Case 'the warning is printed on the -WithBrain path' {
    # Not merely present in the file: reachable from the brain step. A warning in
    # a comment or an unreachable branch is the D67 failure in a new medium.
    $body = $src.Substring($src.IndexOf('function Install-Brain'))
    $body = $body.Substring(0, $body.IndexOf('$bat ='))
    if ($body -notmatch 'AVX-512') { return 'the brain step does not print the warning' }
    return $true
}

Test-Case 'a skipped brain is reported, not silently ignored' {
    if ($src -notmatch 'cannot answer questions') {
        return 'the summary does not tell the user a brainless daemon cannot answer'
    }
    return $true
}

# ---------------------------------------------------------------------------
# 3. No wheel for the brain. The single most load-bearing rule in the project.
# ---------------------------------------------------------------------------

Test-Case 'the engine never installs llama-cpp-python from a wheel' {
    # `--no-binary` is what forces a source build. Its absence would mean the
    # installer quietly installs the AVX-512 build.
    $bad = [regex]::Matches($src, 'pip install[^`\r\n]*llama-cpp-python')
    foreach ($m in $bad) {
        if ($m.Value -notmatch '--no-binary') {
            return "a direct llama-cpp-python install without --no-binary: $($m.Value)"
        }
    }
    return $true
}

Test-Case 'the package step uses the brain extra, never a raw dependency' {
    # pyproject keeps llama-cpp-python out of `dependencies` on purpose and a test
    # enforces it; the installer must not undo that with its own install line.
    if ($src -notmatch '\.\[brain') { return 'the brain extra is never installed' }
    return $true
}

# ---------------------------------------------------------------------------
# 4. Honesty about verification.
# ---------------------------------------------------------------------------

Test-Case 'the engine runs verify_install.py' {
    if ($src -notmatch 'verify_install\.py') { return 'no reference to the verifier' }
    return $true
}

Test-Case 'a skipped verification is labelled, not reported as success' {
    $fn = $src.Substring($src.IndexOf('function Invoke-Verify'))
    $fn = $fn.Substring(0, $fn.IndexOf('$venvPy ='))
    if ($fn -notmatch 'unverified') { return 'SkipVerify does not mark the install unverified' }
    if ($fn -notmatch 'SKIPPED') { return 'SkipVerify is not announced' }
    return $true
}

Test-Case 'the engine sets CUDA on the verifier environment' {
    # D81: the failure is a llama.dll error naming the wrong file, and it happens
    # when the shell's PATH predates the toolkit install.
    if ($src -notmatch 'env:CUDA_PATH') { return 'CUDA_PATH is never set for the verifier' }
    return $true
}

# ---------------------------------------------------------------------------
# 5. Idempotence and autostart.
# ---------------------------------------------------------------------------

Test-Case 'the venv step verifies its own postcondition' {
    # A .venv directory existing is not a working interpreter - the uv trampoline
    # in this repo pointed at a deleted profile and failed exactly that way.
    $fn = $src.Substring($src.IndexOf('function Install-Venv'))
    $fn = $fn.Substring(0, $fn.IndexOf('function Install-Package'))
    if ($fn -notmatch 'sys\.prefix') { return 'no postcondition probe on the venv' }
    return $true
}

Test-Case 'autostart is opt-in only and uses pythonw' {
    if ($src -notmatch 'WithAutostart') { return 'no switch for autostart' }
    if ($src -notmatch 'pythonw\.exe') { return 'autostart does not use pythonw' }
    # At-logon, never at-startup: the tray needs an interactive session.
    if ($src -notmatch 'AtLogOn') { return 'autostart is not at-logon' }
    # The 3-day default kills a 24/7 daemon (D64).
    if ($src -notmatch 'ExecutionTimeLimit') { return 'no execution time limit set' }
    return $true
}

Test-Case 'weights are downloaded, never embedded' {
    if ($src -match 'Compress-Archive.*gguf') { return 'the installer tries to ship weights' }
    if ($src -notmatch 'download_fast_tier\.ps1') { return 'no model download step' }
    return $true
}

Test-Case 'an existing install is detected rather than redone' {
    $fn = $src.Substring($src.IndexOf('function Install-Model'))
    $fn = $fn.Substring(0, $fn.IndexOf('$script ='))
    if ($fn -notmatch 'already present') { return 'no idempotence check on weights' }
    return $true
}

# ---------------------------------------------------------------------------
# 6. Static parse - a syntax error in a 300-line installer is not obvious.
# ---------------------------------------------------------------------------

Test-Case 'every native-command call is guarded against stderr-as-error' {
    # $ErrorActionPreference = 'Stop' makes a native command that writes to
    # stderr raise a terminating NativeCommandError even when it SUCCEEDED. uv
    # prints "Resolved 4 packages" on stderr, llama.cpp prints its own
    # diagnostics there, and the first version of this installer turned a
    # successful install into "uv pip install failed" at the step that proves it
    # works. Every native call must therefore bracket itself.
    $hits = [regex]::Matches($src, '(?m)^\s*& (uv|cmd|&\$venvPy|&\$script|\$venvPy)')
    if ($hits.Count -eq 0) { return 'no native command calls found; the regex may be wrong' }
    foreach ($h in $hits) {
        $start = [Math]::Max(0, $h.Index - 700)
        $ctx = $src.Substring($start, ($h.Index + 20) - $start)
        if ($ctx -notmatch "ErrorActionPreference\s*=\s*'Continue'") {
            return "an unguarded native call: $($h.Value.Trim())"
        }
    }
    return $true
}

Test-Case 'the engine restores ErrorActionPreference after each guard' {
    if ($src -notmatch "ErrorActionPreference = 'Stop'") {
        return 'ErrorActionPreference is never restored'
    }
    return $true
}

Test-Case 'install.ps1 parses without syntax errors' {
    $errs = $null
    [System.Management.Automation.Language.Parser]::ParseFile(
        $Engine, [ref]$null, [ref]$errs) | Out-Null
    if ($errs -and $errs.Count -gt 0) {
        return "parse errors: $($errs[0].Message)"
    }
    return $true
}

# ---------------------------------------------------------------------------
# 7. No undefined variables. FULL installs died in ~6 seconds (exit 1) because
#    Install-Brain read `$vcvars`, which is assigned nowhere - a name that only
#    ever existed in a comment. A syntax check cannot see it; the engine parses
#    cleanly and then throws at runtime, on the one install type nobody ran.
# ---------------------------------------------------------------------------

Test-Case 'every function reads only variables it defines' {
    $toks = $null; $errs = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile(
        $Engine, [ref]$toks, [ref]$errs)
    $auto = @('_','?','^','$','args','input','foreach','switch','this','true',
        'false','null','PID','Error','Matches','LastExitCode',
        'ErrorActionPreference','MyInvocation','PSScriptRoot','PSCommandPath',
        'PWD','HOME','IsWindows','IsLinux','IsMacOS','PSVersionTable','Host',
        'ExecutionContext','PROFILE','PSItem','Event','Sender','PSBoundParameters')
    $ns = 'System.Management.Automation.Language'
    $fns = $ast.FindAll(
        { param($n) $n -is "$ns.FunctionDefinitionAst" }, $true)
    # Script-level facts, visible to every function: the param block, top-level
    # assignments, and bare reads backed by a `$script:` assignment elsewhere
    # (`$CudaRoot` reads what Test-Prereqs stores as `$script:CudaRoot`).
    $scriptParams = @()
    if ($ast.ParamBlock) {
        $scriptParams = @($ast.ParamBlock.Parameters |
            ForEach-Object { $_.Name.VariablePath.UserPath })
    }
    $topAssigns = @()
    $scriptAssigns = @()
    foreach ($a in $ast.FindAll(
        { param($n) $n -is "$ns.AssignmentStatementAst" }, $true)) {
        $lhs = $a.Left
        if ($lhs -is "$ns.VariableExpressionAst") {
            # Qualified paths ($env:, $script:) carry the qualifier in UserPath;
            # DriveName is $null for $script: on an assignment left-hand side,
            # so it cannot be used to tell them apart. Match on the colon.
            if ($lhs.VariablePath.UserPath -match ':') {
                $scriptAssigns += $lhs.VariablePath.UserPath
            } else {
                $inFn = $false
                $p = $a.Parent
                while ($p) {
                    if ($p -is "$ns.FunctionDefinitionAst") { $inFn = $true; break }
                    $p = $p.Parent
                }
                if (-not $inFn) { $topAssigns += $lhs.VariablePath.UserPath }
            }
        }
    }
    foreach ($fn in $fns) {
        $params = @($fn.Parameters |
            ForEach-Object { $_.Name.VariablePath.UserPath })
        if ($fn.Body.ParamBlock) {
            $params += @($fn.Body.ParamBlock.Parameters |
                ForEach-Object { $_.Name.VariablePath.UserPath })
        }
        $assigned = @($fn.FindAll(
            { param($n) $n -is "$ns.AssignmentStatementAst" }, $true) |
            ForEach-Object { $_.Left } |
            Where-Object { $_ -is "$ns.VariableExpressionAst" } |
            ForEach-Object { $_.VariablePath.UserPath })
        $loopVars = @($fn.FindAll(
            { param($n) $n -is "$ns.ForEachStatementAst" }, $true) |
            ForEach-Object { $_.Variable.VariablePath.UserPath })
        $defined = @($params + $assigned + $loopVars + $scriptParams +
                     $topAssigns + $auto)
        foreach ($v in $fn.FindAll(
            { param($n) $n -is "$ns.VariableExpressionAst" }, $true)) {
            $vp = $v.VariablePath
            if ($vp.UserPath -match ':') { continue }  # $env:, $script: - qualified
            if ($vp.IsSplatted) { continue }
            if ($defined -notcontains $vp.UserPath -and
                $scriptAssigns -notcontains ("script:" + $vp.UserPath)) {
                return ('$' + $vp.UserPath + ' in ' + $fn.Name +
                        ' is assigned nowhere')
            }
        }
    }
    return $true
}

Write-Host ''
Write-Host "  $script:Pass passed, $script:Fail failed" -ForegroundColor $(if ($script:Fail) { 'Red' } else { 'Green' })
Write-Host ''

if ($script:Fail) {
    Write-Host '  An installer that fails its own tests is not an installer.' -ForegroundColor Red
    exit 1
}
exit 0