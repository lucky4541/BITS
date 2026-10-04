Set-Location -Path $PSScriptRoot
$log = Join-Path $PSScriptRoot 'build_onefile_log.txt'
"Started $(Get-Date)" | Out-File -FilePath $log -Encoding utf8
& cmd.exe /c "packaging\build_onefile.bat" 2>&1 | ForEach-Object { $_.ToString() } | Tee-Object -FilePath $log -Append
$rc = $LASTEXITCODE
"Finished $(Get-Date)" | Out-File -FilePath $log -Append -Encoding utf8
"BUILD_EXIT_CODE=$rc" | Out-File -FilePath $log -Append -Encoding utf8
exit $rc
