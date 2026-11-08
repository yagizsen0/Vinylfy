@echo off
setlocal
echo ============================================
echo   Vinylfy — Windows Build ^& Installer Script
echo ============================================

echo [1/3] Installing / verifying dependencies...
python -m pip install -r requirements.txt pyinstaller
if errorlevel 1 (
    echo Failed to install Python dependencies.
    exit /b 1
)

echo [2/3] Building Vinylfy executable with PyInstaller...
python -m PyInstaller --noconfirm --clean Vinylfy.spec
if errorlevel 1 (
    echo PyInstaller build failed.
    exit /b 1
)
echo PyInstaller build succeeded: dist\Vinylfy\Vinylfy.exe

echo [3/3] Checking for Inno Setup 6 Compiler (ISCC.exe)...
set "ISCC_PATH="
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC_PATH=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC_PATH=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC_PATH=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"

if defined ISCC_PATH (
    echo Compiling Windows Setup Wizard with Inno Setup...
    "%ISCC_PATH%" installer.iss
    echo Done! Installer created in: Output\Vinylfy_Setup_v1.0.0.exe
) else (
    echo Inno Setup 6 was not found in default paths.
    echo Install Inno Setup 6 from https://jrsoftware.org/isdl.php and compile installer.iss,
    echo OR distribute dist\Vinylfy\ directly.
)

endlocal
pause
