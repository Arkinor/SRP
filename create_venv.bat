@echo off
cd /d "%~dp0"
title Project Launcher
color 0A

set "PYTHON_VERSION=3.11"
set "PYTHON_INSTALLER_URL=https://www.python.org/ftp/python/3.11.0/python-3.11.0-amd64.exe"
set "PYTHON_DOWNLOAD_PAGE=https://www.python.org/downloads/release/python-3110/"

echo ================================================================
echo                    Universal Python Launcher
echo ================================================================
echo.

:: Find global Python 3.11
set PYTHON_EXE=
echo [1/5] Looking for Python %PYTHON_VERSION%...

:: Common install paths
for %%p in (
    "%ProgramFiles%\Python311\python.exe"
    "%LocalAppData%\Programs\Python\Python311\python.exe"
) do (
    if exist %%p set PYTHON_EXE=%%p & goto :found_python
)

:: Search Program Files and LocalAppData
for /d %%d in ("%ProgramFiles%\Python*") do (
    if exist "%%d\python.exe" (
        for /f "tokens=2" %%v in ('"%%d\python.exe" --version 2^>^&1') do (
            echo %%v | findstr "^%PYTHON_VERSION%" >nul && set PYTHON_EXE="%%d\python.exe" && goto :found_python
        )
    )
)
for /d %%d in ("%LocalAppData%\Programs\Python\Python*") do (
    if exist "%%d\python.exe" (
        for /f "tokens=2" %%v in ('"%%d\python.exe" --version 2^>^&1') do (
            echo %%v | findstr "^%PYTHON_VERSION%" >nul && set PYTHON_EXE="%%d\python.exe" && goto :found_python
        )
    )
)

:: Check PATH
where python >nul 2>&1
if not errorlevel 1 (
    for /f "tokens=2" %%v in ('python --version 2^>^&1') do (
        echo %%v | findstr "^%PYTHON_VERSION%" >nul && set PYTHON_EXE=python && goto :found_python
    )
)

:: Not found -> offer installation
echo Python %PYTHON_VERSION% not found.
echo.
echo ================================================================
echo Python Installation Required
echo ================================================================
echo.
echo Please download and install Python %PYTHON_VERSION% (64-bit)
echo.
echo Opening download page...
start %PYTHON_DOWNLOAD_PAGE%
echo.
echo Direct download link:
echo %PYTHON_INSTALLER_URL%
echo.
echo After installation, run this script again.
echo.
pause
exit /b 1

:found_python
echo Found: %PYTHON_EXE%
%PYTHON_EXE% --version
echo.

:: Create virtual environment if missing
echo [2/5] Setting up virtual environment...
if not exist ".venv" (
    %PYTHON_EXE% -m venv .venv
    if errorlevel 1 (
        echo ERROR: Failed to create virtual environment.
        pause
        exit /b 1
    )
    echo Virtual environment created.
) else (
    echo Virtual environment already exists.
)

:: Activate venv
echo [3/5] Activating virtual environment...
call .venv\Scripts\activate.bat
if errorlevel 1 (
    echo ERROR: Failed to activate virtual environment.
    pause
    exit /b 1
)
echo Virtual environment activated.
echo.
echo Python version in venv:
python --version
echo.

:: Done - stop here
echo [4/5] Installing project dependencies...
python -m pip install --upgrade pip
if errorlevel 1 (
    echo ERROR: Failed to upgrade pip.
    pause
    exit /b 1
)
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: Failed to install requirements.
    pause
    exit /b 1
)
echo Dependencies installed.
echo.

echo [5/5] Setup complete!
echo ================================================================
echo Virtual environment is ready and activated
echo ================================================================
echo.
echo Run the application:
echo   python app.py
echo.
echo Build a single EXE with an icon:
echo   build_exe.bat
echo.
echo To deactivate the virtual environment, type:
echo   deactivate
echo.
pause