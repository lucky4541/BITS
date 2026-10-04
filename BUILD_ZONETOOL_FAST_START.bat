@echo off
title ZoneTool FAST-START build - do not close this window
cd /d "%~dp0"
echo ============================================================
echo  FAST-START build: dist\EPUBForge\EPUBForge.exe
echo  (a folder build - opens in seconds, everything included)
echo  DO NOT CLOSE THIS WINDOW until it says Done.
echo ============================================================
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_exe_fast_start.ps1"
set RC=%errorlevel%
echo.
if "%RC%"=="0" (
  echo BUILD SUCCEEDED
  echo   Run:        %~dp0dist\EPUBForge\EPUBForge.exe
  echo   To share:   copy the WHOLE dist\EPUBForge folder ^(exe + _internal^)
) else (
  echo BUILD FAILED - see build_fast_start_log.txt
)
echo.
pause
