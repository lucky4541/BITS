@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================================
echo BITSTool - Production Build (onedir)
echo ============================================================

echo.
echo [1/4] Cleaning previous build\ and dist\ ...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo.
echo [2/4] Running PyInstaller (BITSTool.spec) ...
python -m PyInstaller BITSTool.spec --noconfirm
if errorlevel 1 (
    echo.
    echo ============================================================
    echo BUILD FAILED - PyInstaller reported an error above.
    echo ============================================================
    exit /b 1
)

echo.
echo [3/4] Verifying the packaged output ...
python verify_package.py
if errorlevel 1 (
    echo.
    echo ============================================================
    echo BUILD FAILED - verify_package.py found missing resources.
    echo Do NOT ship the contents of dist\BITSTool\ until this passes.
    echo ============================================================
    exit /b 1
)

echo.
echo [4/4] Done.
echo ============================================================
echo BUILD SUCCEEDED
echo.
echo   Executable:  %~dp0dist\BITSTool\BITSTool.exe
echo   Full folder: %~dp0dist\BITSTool\        ^(copy this WHOLE folder to distribute^)
echo ============================================================
exit /b 0
