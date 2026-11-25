param(
    [string]$StageRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
)

$ErrorActionPreference = 'Stop'
$stage = (Resolve-Path -LiteralPath $StageRoot).Path
$delivery = Join-Path $stage 'delivery'
New-Item -ItemType Directory -Path $delivery -Force | Out-Null
$temporary = Join-Path $stage '_package_stage'
$zip = Join-Path $delivery 'ral_hil_stage1_phase1.zip'
$manifest = Join-Path $delivery 'package_manifest.sha256.csv'
$zipHash = Join-Path $delivery 'ral_hil_stage1_phase1.zip.sha256'

if (Test-Path -LiteralPath $temporary) {
    $resolved = (Resolve-Path -LiteralPath $temporary).Path
    if (-not $resolved.StartsWith($stage, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove staging path outside stage root: $resolved"
    }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}
New-Item -ItemType Directory -Path $temporary | Out-Null

$files = Get-ChildItem -LiteralPath $stage -Recurse -File | Where-Object {
    $relative = $_.FullName.Substring($stage.Length).TrimStart('\','/')
    $segments = $relative -split '[\\/]'
    $relative -notlike 'delivery\ral_hil_stage1_phase1.zip*' -and
    $relative -ne 'delivery\package_manifest.sha256.csv' -and
    $segments -notcontains 'private' -and
    $segments -notcontains '__pycache__' -and
    $segments -notcontains 'rendered' -and
    $segments -notcontains '_selftest' -and
    $segments -notcontains '_selftest_final' -and
    $segments -notcontains '.pytest_cache' -and
    $segments -notcontains '.pytest_tmp' -and
    $segments -notcontains '.pytest_tmp_all' -and
    $relative -notmatch '^tests[\\/]algorithm_policy[\\/](checker_report|patched_checker_report|final_checker_report)[\\/]' -and
    $_.Extension -ne '.pyc'
}

$records = foreach ($file in $files) {
    $relative = $file.FullName.Substring($stage.Length).TrimStart('\','/')
    $target = Join-Path $temporary $relative
    New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
    Copy-Item -LiteralPath $file.FullName -Destination $target
    [PSCustomObject]@{
        path = $relative.Replace('\','/')
        bytes = $file.Length
        sha256 = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    }
}

$records | Sort-Object path | Export-Csv -LiteralPath $manifest -NoTypeInformation -Encoding UTF8
Copy-Item -LiteralPath $manifest -Destination (Join-Path $temporary 'delivery\package_manifest.sha256.csv')
if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force }
Compress-Archive -Path (Join-Path $temporary '*') -DestinationPath $zip -CompressionLevel Optimal
$digest = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -LiteralPath $zipHash -Value "$digest  ral_hil_stage1_phase1.zip" -Encoding ASCII

$resolvedTemporary = (Resolve-Path -LiteralPath $temporary).Path
if (-not $resolvedTemporary.StartsWith($stage, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to remove staging path outside stage root: $resolvedTemporary"
}
Remove-Item -LiteralPath $resolvedTemporary -Recurse -Force

[PSCustomObject]@{
    zip = $zip
    sha256 = $digest
    files = $records.Count
    manifest = $manifest
} | ConvertTo-Json
