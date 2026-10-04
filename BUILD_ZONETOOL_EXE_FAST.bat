@echo off
title ZoneTool FAST EXE build - do not close this window
cd /d "%~dp0"
echo ============================================================
echo  FAST build of the single self-contained EPUBForge.exe
echo  (reuses the installed packages and PyInstaller cache)
echo  DO NOT CLOSE THIS WINDOW until it says Done.
echo ============================================================
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_exe_fast.ps1"
set RC=%errorlevel%
echo.
if "%RC%"=="0" (
  echo BUILD SUCCEEDED: %~dp0dist\EPUBForge.exe
) else (
  echo BUILD FAILED - see build_onefile_log.txt
)
echo.
pause
