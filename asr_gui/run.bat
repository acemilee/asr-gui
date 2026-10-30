@echo off
setlocal EnableExtensions
rem ---------------------------------------------------------------------------
rem  Launcher for the local ASR GUI (pure ASCII on purpose: cmd.exe reads .bat
rem  files in the OEM code page, so any non-ASCII text here garbles on some
rem  locales). The real logic lives in run.bat.
rem ---------------------------------------------------------------------------
set "HERE=%~dp0"
cd /d "%HERE%"

rem 1) Optional local override (git-ignored, machine specific)
if not defined ASR_PYTHON if exist "%HERE%config.local.bat" call "%HERE%config.local.bat"

set "PY=%ASR_PYTHON%"
if defined PY if exist "%PY%" goto have_py

rem 2) Repo-local virtualenv
if exist "%HERE%..\.venv\Scripts\python.exe" (
    set "PY=%HERE%..\.venv\Scripts\python.exe"
    goto have_py
)

rem 3) python.exe on PATH
where python.exe >nul 2>nul
if not errorlevel 1 (
    set "PY=python.exe"
    goto have_py
)

rem 4) Portable bundle layout used by some releases
if exist "%HERE%..\VideoToNo-1.4.6-portable\venv\Scripts\python.exe" (
    set "PY=%HERE%..\VideoToNo-1.4.6-portable\venv\Scripts\python.exe"
    goto have_py
)

echo [ERROR] No Python interpreter found.
echo.
echo Set the environment variable ASR_PYTHON to a python.exe that has
echo sherpa-onnx, faster-whisper, av and numpy installed, or create
echo asr_gui\config.local.bat containing:
echo     set "ASR_PYTHON=C:\path\to\venv\Scripts\python.exe"
pause
exit /b 1

:have_py
"%PY%" -c "import sherpa_onnx, av, numpy" >nul 2>nul
if errorlevel 1 (
    echo [ERROR] This interpreter cannot import the required packages:
    echo     %PY%
    echo.
    echo Install them with:
    echo     "%PY%" -m pip install -r "%~dp0..\requirements.txt"
    pause
    exit /b 1
)

rem Prefer pythonw.exe (no console window) when it sits next to python.exe
for %%I in ("%PY%") do set "PYDIR=%%~dpI"
if exist "%PYDIR%pythonw.exe" set "PY=%PYDIR%pythonw.exe"

start "" "%PY%" "%HERE%app.py"
endlocal
