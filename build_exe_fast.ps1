# Fast one-file build of BITSTool.exe (see packaging\BITSTool_onefile_fast.spec).
Set-Location -Path $PSScriptRoot
$ErrorActionPreference = 'Continue'
$log = Join-Path $PSScriptRoot 'build_onefile_log.txt'
$sw = [Diagnostics.Stopwatch]::StartNew()
function Say($m) { $line = "[{0:mm\:ss}] {1}" -f $sw.Elapsed, $m; Write-Host $line -ForegroundColor Cyan; $line | Out-File $log -Append -Encoding utf8 }
"Started $(Get-Date)" | Out-File $log -Encoding utf8

$py = Join-Path $PSScriptRoot 'build_venv311\Scripts\python.exe'
if (-not (Test-Path $py)) {
    Say 'build_venv311 not found - running the full first-time build instead (creates it).'
    & cmd.exe /c "packaging\build_onefile.bat" 2>&1 | ForEach-Object { $_.ToString() } | Tee-Object -FilePath $log -Append
    $rc = $LASTEXITCODE; "BUILD_EXIT_CODE=$rc" | Out-File $log -Append -Encoding utf8; exit $rc
}

Say '[1/4] Checking the build environment (no re-install unless something is missing)...'
& $py -c "import PyInstaller, paddle, paddleocr, fitz, lxml, spellchecker, cv2, PIL, numpy, fontTools" 2>$null
if ($LASTEXITCODE -ne 0) {
    Say '      Something is missing - installing requirements (one time only)...'
    & $py -m pip install -r requirements.txt -r packaging\requirements-build.txt 2>&1 | ForEach-Object { $_.ToString() } | Tee-Object -FilePath $log -Append
    if ($LASTEXITCODE -ne 0) { Say 'BUILD FAILED - dependency installation failed.'; "BUILD_EXIT_CODE=1" | Out-File $log -Append -Encoding utf8; exit 1 }
} else { Say '      OK - reusing installed packages.' }

Say '[2/4] Removing the previous dist\BITSTool.exe (build cache is kept for speed)...'
if (Test-Path 'dist\BITSTool.exe') { Remove-Item 'dist\BITSTool.exe' -Force }

Say '[3/4] PyInstaller: analysing + packing everything into ONE exe (the "Building PKG" step is silent for a few minutes)...'
& $py -m PyInstaller 'packaging\BITSTool_onefile_fast.spec' --distpath dist --workpath build_onefile_fast --noconfirm 2>&1 |
    ForEach-Object { $_.ToString() } | Tee-Object -FilePath $log -Append |
    Where-Object { $_ -match 'Building PKG|Building EXE|completed successfully|ERROR|Error|FAST BUILD|Build complete' }
if ($LASTEXITCODE -ne 0 -or -not (Test-Path 'dist\BITSTool.exe')) {
    Say 'BUILD FAILED - see build_onefile_log.txt'; "BUILD_EXIT_CODE=1" | Out-File $log -Append -Encoding utf8; exit 1
}

Say '[4/4] Verifying every bundled resource inside the new exe...'
& $py 'packaging\check_package.py' --onefile 2>&1 | ForEach-Object { $_.ToString() } | Tee-Object -FilePath $log -Append
$rc = $LASTEXITCODE
Say ("Done in {0:mm\:ss}" -f $sw.Elapsed)
"BUILD_EXIT_CODE=$rc" | Out-File $log -Append -Encoding utf8
exit $rc
