@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title SR Pro - build single EXE
color 0B

set "VENV_PY=%~dp0.venv\Scripts\python.exe"
set "APP_NAME=SRPro"
set "ICON=%~dp0assets\srpro.ico"

echo ================================================================
echo                  SR Pro - single EXE builder
echo ================================================================
echo.

rem ----------------------------------------------------------------
rem 1. Virtual environment
rem ----------------------------------------------------------------
echo [1/4] Checking Python virtual environment...
if not exist "%VENV_PY%" (
    echo      .venv not found, creating it with Python 3.11...
    py -3.11 -m venv ".venv" 2>nul
    if errorlevel 1 (
        echo      py launcher unavailable, trying python...
        python -m venv ".venv"
    )
)
if not exist "%VENV_PY%" (
    echo.
    echo ERROR: could not create .venv. Install Python 3.11+ and try again.
    goto :error
)
"%VENV_PY%" --version
if errorlevel 1 goto :error

rem ----------------------------------------------------------------
rem 2. Dependencies
rem ----------------------------------------------------------------
echo.
echo [2/4] Installing dependencies (this may take a few minutes)...
"%VENV_PY%" -m pip install --upgrade pip
if errorlevel 1 goto :error
"%VENV_PY%" -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto :error

rem ----------------------------------------------------------------
rem 3. Icon
rem ----------------------------------------------------------------
echo.
echo [3/4] Generating the application icon...
"%VENV_PY%" tools\make_icon.py
if errorlevel 1 goto :error
if not exist "%ICON%" (
    echo ERROR: icon was not generated: %ICON%
    goto :error
)

rem ----------------------------------------------------------------
rem 4. Build
rem ----------------------------------------------------------------
echo.
echo [4/4] Building %APP_NAME%.exe (single file)...
tasklist /FI "IMAGENAME eq %APP_NAME%.exe" 2>nul | find /I "%APP_NAME%.exe" >nul
if not errorlevel 1 (
    echo ERROR: %APP_NAME%.exe is running. Close it and run the build again.
    goto :error
)
if exist "build" rmdir /s /q "build" 2>nul
if not exist "dist" mkdir "dist"

"%VENV_PY%" -m PyInstaller ^
    --noconfirm --clean --onefile --windowed ^
    --name "%APP_NAME%" ^
    --icon "%ICON%" ^
    --distpath "dist" ^
    --workpath "build\work" ^
    --specpath "build" ^
    --hidden-import "keyboard._winkeyboard" ^
    --hidden-import "pythoncom" ^
    --hidden-import "pywintypes" ^
    --collect-all "soundcard" ^
    --collect-all "imageio_ffmpeg" ^
    app.py
if errorlevel 1 goto :error

if not exist "dist\%APP_NAME%.exe" (
    echo ERROR: PyInstaller finished but dist\%APP_NAME%.exe is missing.
    goto :error
)

echo.
echo ================================================================
echo Build complete!
echo   EXE: "%~dp0dist\%APP_NAME%.exe"
for %%A in ("dist\%APP_NAME%.exe") do echo   Size: %%~zA bytes
echo ================================================================
echo.
start "" explorer "%~dp0dist"
pause
exit /b 0

:error
echo.
echo ================================================================
echo BUILD FAILED - see the messages above.
echo ================================================================
pause
exit /b 1
