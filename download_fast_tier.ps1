# Downloads a model file and VERIFIES its exact byte count before declaring success.
#
# The original version of this was a batch file that curled two files in
# sequence and echoed "ALL DOWNLOADS COMPLETE" regardless of what happened. It
# did exactly that while the 4.17 GB model had died at 216 MB (curl exit 18,
# partial transfer) - producing a GGUF that loads and then generates nonsense
# rather than an error. Expected sizes come from the Hugging Face tree API.

$ErrorActionPreference = 'Continue'

$Repo = 'HauhauCS/Qwen3.5-4B-Uncensored-HauhauCS-Aggressive'
$Out = 'G:\localaized\models'
$Log = 'G:\localaized\download_fast_tier.log'

# filename -> exact expected byte count, from https://huggingface.co/api/models/<id>/tree/main
$Wanted = [ordered]@{
    'Qwen3.5-4B-Uncensored-HauhauCS-Aggressive-Q8_0.gguf' = 4482402656
    'mmproj-Qwen3.5-4B-Uncensored-HauhauCS-Aggressive-BF16.gguf' = 675568768
}

if (-not (Test-Path $Out)) { New-Item -ItemType Directory -Path $Out | Out-Null }

function Write-Log([string]$line) {
    $line | Out-File -FilePath $Log -Append -Encoding utf8
}

"=== START $Repo ===" | Out-File -FilePath $Log -Encoding utf8

$failed = $false

foreach ($name in $Wanted.Keys) {
    $want = [int64]$Wanted[$name]
    $dest = Join-Path $Out $name
    $url = "https://huggingface.co/$Repo/resolve/main/$name"

    $have = 0L
    if (Test-Path $dest) { $have = (Get-Item $dest).Length }
    Write-Log "--- $name"
    Write-Log "    have=$have want=$want"

    if ($have -eq $want) {
        Write-Log "    already complete, skipping"
        continue
    }

    Write-Log "    fetching with resume (-C -)"
    & curl.exe -L --fail --retry 8 --retry-delay 10 --retry-all-errors -C - `
        -o $dest $url *>> $Log
    $rc = $LASTEXITCODE
    Write-Log "    curl exit = $rc"

    $have = 0L
    if (Test-Path $dest) { $have = (Get-Item $dest).Length }

    if ($have -eq $want) {
        Write-Log "    OK ($have bytes)"
    } else {
        $failed = $true
        Write-Log "    INCOMPLETE: have=$have want=$want  (delta=$($want - $have))"
    }
}

Write-Log ''
if ($failed) {
    Write-Log 'RESULT: INCOMPLETE - do not use these files in Phase 1'
} else {
    Write-Log 'RESULT: ALL FILES VERIFIED against the Hugging Face byte counts'
}
Write-Host "done. failed=$failed"
exit ([int]$failed)
