# Fast-START build: dist\EPUBForge\EPUBForge.exe (+ _internal folder).
# Opens in seconds instead of unpacking ~1 GB on every double-click.
Set-Location -Path $PSScriptRoot
$log = Join-Path $PSScriptRoot 'build_fast_start_log.txt'
$sw = [Diagnostics.Stopwatch]::StartNew()
function Say($m) { $line = "[{0:mm\:ss}] {1}" -f $sw.Elapsed, $m; Write-Host $line -ForegroundColor Cyan; $line | Out-File $log -Append -Encoding utf8 }
"Started $(Get-Date)" | Out-File $log -Encoding utf8

$py = Join-Path $PSScriptRoot 'build_venv311\Scripts\python.exe'
if (-not (Test-Path $py)) { Say 'build_venv311 not found - run BUILD_ZONETOOL_EXE.bat once first (it creates it).'; "BUILD_EXIT_CODE=1" | Out-File $log -Append -Encoding utf8; exit 1 }

Say '[1/4] Checking the build environment...'
& $py -c "import PyInstaller, paddle, paddleocr, fitz, lxml, spellchecker, cv2, PIL, numpy, fontTools" 2>$null
if ($LASTEXITCODE -ne 0) {
    Say '      Installing missing requirements (one time only)...'
    & $py -m pip install -r requirements.txt -r packaging\requirements-build.txt 2>&1 | ForEach-Object { $_.ToString() } | Tee-Object -FilePath $log -Append
    if ($LASTEXITCODE -ne 0) { Say 'BUILD FAILED - dependency installation failed.'; "BUILD_EXIT_CODE=1" | Out-File $log -Append -Encoding utf8; exit 1 }
} else { Say '      OK - reusing installed packages.' }

Say '[2/4] Removing the previous dist\EPUBForge folder...'
if (Test-Path 'dist\EPUBForge') { Remove-Item 'dist\EPUBForge' -Recurse -Force }

Say '[3/4] PyInstaller: building the EPUBForge folder (a few minutes)...'
& $py -m PyInstaller 'packaging\EPUBForge_fast_start.spec' --distpath dist --workpath build_fast_start --noconfirm 2>&1 |
    ForEach-Object { $_.ToString() } | Tee-Object -FilePath $log -Append |
    Where-Object { $_ -match 'Building COLLECT|Building EXE|completed successfully|ERROR|Error|Build complete' }
if ($LASTEXITCODE -ne 0 -or -not (Test-Path 'dist\EPUBForge\EPUBForge.exe')) {
    Say 'BUILD FAILED - see build_fast_start_log.txt'; "BUILD_EXIT_CODE=1" | Out-File $log -Append -Encoding utf8; exit 1
}

Say '[4/4] Verifying every bundled resource...'
& $py 'packaging\check_package.py' 2>&1 | ForEach-Object { $_.ToString() } | Tee-Object -FilePath $log -Append
$rc = $LASTEXITCODE
Say ("Done in {0:mm\:ss}" -f $sw.Elapsed)
"BUILD_EXIT_CODE=$rc" | Out-File $log -Append -Encoding utf8
exit $rc
