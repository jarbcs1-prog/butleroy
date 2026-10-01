# Downloads the 9B deep tier, resuming and verifying against exact byte counts.
#
# A truncated GGUF loads anyway and then produces nonsense rather than an error,
# so the byte count is checked before the file is used for anything. That check
# is the whole point of this script existing rather than a bare curl.
#
# D22: IQ4_XS is 5.96 GiB. Measured overhead on the 4B was 0.75 GiB, putting the
# 9B near 6.7 GiB against a 7.0 GiB ceiling (D1). It runs alone - the arbiter
# evicts the fast tier to make room, which is the only way two tiers can coexist
# on a 12 GiB card.

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$base = 'https://huggingface.co/DavidAU/Qwen3.5-9B-The-Defiant-Fable-Uncensored-Heretic-NEO-IMATRIX-MAX-MTP-GGUF/resolve/main'
$dest = 'G:\localaized\models'

$Wanted = @(
    @{
        Name = 'Qwen3.5-9B-The-Defiant-Fable-Uncnr-Heretic-NEO-MAX-IQ4_XS.gguf'
        Bytes = 6396325152
    },
    @{
        Name = 'mmproj-F16.gguf'
        Bytes = 918166080
    }
)

foreach ($f in $Wanted) {
    $out = Join-Path $dest $f.Name
    if (Test-Path $out) {
        $have = (Get-Item $out).Length
        if ($have -eq $f.Bytes) {
            Write-Output "SKIP (already complete): $($f.Name)"
            continue
        }
        Write-Output "RESUME $($f.Name): have $have of $($f.Bytes)"
    }
    else {
        Write-Output "GET $($f.Name) - $($f.Bytes) bytes"
    }
    & curl.exe -L --fail --retry 8 --retry-delay 10 --retry-all-errors -C - `
        -o $out "$base/$($f.Name)"
    if ($LASTEXITCODE -ne 0) {
        Write-Output "FAILED $($f.Name) with curl exit $LASTEXITCODE"
        exit 1
    }
}

$bad = @()
foreach ($f in $Wanted) {
    $out = Join-Path $dest $f.Name
    $have = if (Test-Path $out) { (Get-Item $out).Length } else { 0 }
    if ($have -eq $f.Bytes) { Write-Output "VERIFIED $($f.Name): $have bytes" }
    else { $bad += "$($f.Name): got $have want $($f.Bytes)" }
}

if ($bad.Count -gt 0) {
    Write-Output "RESULT: INCOMPLETE - do not use these files"
    $bad | ForEach-Object { Write-Output "  $_" }
    exit 1
}
Write-Output "RESULT: ALL FILES VERIFIED against the Hugging Face byte counts"
exit 0