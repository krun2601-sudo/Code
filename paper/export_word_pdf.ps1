param(
    [Parameter(Mandatory = $true)]
    [string]$InputDocx,

    [Parameter(Mandatory = $true)]
    [string]$OutputPdf
)

$inputPath = (Resolve-Path -LiteralPath $InputDocx).Path
$outputDirectory = Split-Path -Parent $OutputPdf
if (-not (Test-Path -LiteralPath $outputDirectory)) {
    New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
}
$outputPath = [System.IO.Path]::GetFullPath($OutputPdf)

$word = $null
$document = $null
try {
    $word = New-Object -ComObject Word.Application
    $word.Visible = $false
    $word.DisplayAlerts = 0
    $document = $word.Documents.Open($inputPath, $false, $true)
    # 17 is wdExportFormatPDF.
    $document.ExportAsFixedFormat($outputPath, 17)
    Write-Output $outputPath
}
finally {
    if ($null -ne $document) {
        $document.Close($false)
        [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($document)
    }
    if ($null -ne $word) {
        $word.Quit()
        [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($word)
    }
    [GC]::Collect()
    [GC]::WaitForPendingFinalizers()
}
