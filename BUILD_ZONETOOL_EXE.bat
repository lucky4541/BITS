@echo off
title Building ZoneTool single EXE - please wait (can take 15-40 minutes)
cd /d "%~dp0"
echo ============================================================
echo  Building the single self-contained EPUBForge.exe
echo  Everything (profiles, OCR models, Paddle, EPUBCheck, Java)
echo  is bundled inside. Full log: build_onefile_log.txt
echo ============================================================
echo Started %date% %time% > "%~dp0build_onefile_log.txt"
call "%~dp0packaging\build_onefile.bat" >> "%~dp0build_onefile_log.txt" 2>&1
set RC=%errorlevel%
echo Finished %date% %time% with exit code %RC% >> "%~dp0build_onefile_log.txt"
echo BUILD_EXIT_CODE=%RC% >> "%~dp0build_onefile_log.txt"
echo.
if "%RC%"=="0" (
  echo BUILD SUCCEEDED: %~dp0dist\EPUBForge.exe
) else (
  echo BUILD FAILED - see build_onefile_log.txt
)
echo.
pause
