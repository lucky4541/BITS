@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================================
echo EPUBForge - OPTIONAL Single-File Build (onefile)
echo NOTE: the onedir build (build_exe.bat) is the primary,
echo       recommended production package - see EPUBForge_onefile.spec
echo       for why. Use this only if a single EXE is specifically needed.
echo ============================================================

echo.
echo [1/3] Cleaning previous build\ and dist\ ...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo.
echo [2/3] Running PyInstaller (EPUBForge_onefile.spec) ...
python -m PyInstaller EPUBForge_onefile.spec --noconfirm
if errorlevel 1 (
    echo.
    echo ============================================================
    echo BUILD FAILED - PyInstaller reported an error above.
    echo ============================================================
    exit /b 1
)

echo.
echo [3/3] Verifying the packaged output ...
python verify_package.py --onefile
if errorlevel 1 (
    echo.
    echo ============================================================
    echo BUILD FAILED - verify_package.py could not confirm the EXE.
    echo ============================================================
    exit /b 1
)

echo.
echo ============================================================
echo BUILD SUCCEEDED
echo.
echo   Executable: %~dp0dist\EPUBForge.exe
echo ============================================================
exit /b 0
