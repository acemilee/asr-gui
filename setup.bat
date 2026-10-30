@echo off
setlocal EnableExtensions
rem ---------------------------------------------------------------------------
rem  asr-gui - Windows dependency bootstrap
rem
rem  Creates .venv in the repository root and installs requirements.txt into it.
rem  This file is deliberately PURE ASCII with CRLF line endings: cmd.exe parses
rem  .bat files using the OEM code page, so non-ASCII bytes or LF-only line
rem  endings break "if (...)" blocks and make double-clicking fail.
rem
rem  Usage:  setup.bat        create / refresh .venv and install deps
rem          setup.bat /y     same, but do not pause at the end
rem ---------------------------------------------------------------------------

cd /d "%~dp0"

echo.
echo ==========================================================
echo   asr-gui setup
echo ==========================================================
echo.

rem --- 1. Locate a Python 3.9+ interpreter --------------------------------
set "PY_CMD="

where py.exe >nul 2>nul
if errorlevel 1 goto :try_python
py -3 -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 9) else 1)" >nul 2>nul
if errorlevel 1 goto :try_python
set "PY_CMD=py -3"
goto :have_python

:try_python
where python.exe >nul 2>nul
if errorlevel 1 goto :no_python
python -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 9) else 1)" >nul 2>nul
if errorlevel 1 goto :no_python
set "PY_CMD=python"

:have_python
echo [1/4] Python:
%PY_CMD% -c "import sys; print('      ' + sys.executable)"
%PY_CMD% -c "import sys; print('      Python ' + sys.version.split()[0])"

rem --- 2. Create (or reuse) the virtual environment -----------------------
set "VENV=%CD%\.venv"
set "VENV_PY=%VENV%\Scripts\python.exe"

if exist "%VENV_PY%" goto :have_venv
echo [2/4] Creating virtual environment: .venv
%PY_CMD% -m venv "%VENV%"
if errorlevel 1 goto :venv_failed
if not exist "%VENV_PY%" goto :venv_failed
goto :venv_ready

:have_venv
echo [2/4] Reusing existing virtual environment: .venv

:venv_ready
rem --- 3. Install dependencies --------------------------------------------
echo [3/4] Upgrading pip ...
"%VENV_PY%" -m pip install --upgrade pip --disable-pip-version-check
if errorlevel 1 goto :pip_failed

echo [4/4] Installing requirements.txt ...
"%VENV_PY%" -m pip install -r "%CD%\requirements.txt"
if errorlevel 1 goto :pip_failed

echo.
echo [OK] Setup complete.
echo.
echo   Optional GPU support (NVIDIA, CUDA 12):
echo     "%VENV_PY%" -m pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
echo.
echo Run asr_gui\run.bat to start.
echo.
if /i "%~1"=="/y" goto :done
pause

:done
endlocal
exit /b 0

rem --- Error paths (ASCII only) -------------------------------------------
:no_python
echo [ERROR] Python 3.9 or newer was not found.
echo.
echo   Install it from https://www.python.org/downloads/ and tick
echo   "Add python.exe to PATH" in the installer, then run setup.bat again.
echo.
echo   Alternatively point the app at an existing interpreter:
echo       set "ASR_PYTHON=C:\path\to\python.exe"
echo.
pause
exit /b 1

:venv_failed
echo [ERROR] Could not create the virtual environment at:
echo         %VENV%
echo.
echo   Check that the current user can write to this folder and that the
echo   Python installation includes the "venv" module.
echo.
pause
exit /b 1

:pip_failed
echo [ERROR] Installing dependencies failed.
echo.
echo   Most often this is a network or proxy problem. Try again, or run:
echo       "%VENV_PY%" -m pip install -r "%CD%\requirements.txt"
echo   with an index mirror, for example:
echo       "%VENV_PY%" -m pip install -r "%CD%\requirements.txt" -i https://pypi.tuna.tsinghua.edu.cn/simple
echo.
pause
exit /b 1
