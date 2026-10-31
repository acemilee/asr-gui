@echo off
rem Build the single-file Windows exe (dist\ASR-GUI.exe) with PyInstaller.
rem Requires: a Python 3.9+ environment with requirements.txt + pyinstaller installed.
rem Usage: double-click this file, or run packaging\build.bat from any directory.

setlocal enableextensions
set "HERE=%~dp0"
set "REPO=%HERE%.."

set "PY="
if defined ASR_PYTHON set "PY=%ASR_PYTHON%"
if not defined PY if exist "%REPO%\.venv\Scripts\python.exe" set "PY=%REPO%\.venv\Scripts\python.exe"
if not defined PY for %%I in (python.exe) do if not defined PY set "PY=%%~$PATH:I"
if not defined PY goto :nopython

if not exist "%HERE%icon.ico" (
    echo [1/3] Generating packaging\icon.ico ...
    "%PY%" "%HERE%make_icon.py" || goto :failed
) else (
    echo [1/3] Using existing packaging\icon.ico
)

echo [2/3] Building with PyInstaller ...
cd /d "%REPO%"
"%PY%" -m PyInstaller --clean --noconfirm packaging\asr-gui.spec || goto :failed

echo [3/3] Done: %REPO%\dist\ASR-GUI.exe
"%PY%" -c "import os,sys; p=os.path.join(sys.argv[1],'ASR-GUI.exe'); print('size:', round(os.path.getsize(p)/1048576.0, 1), 'MB')" "%REPO%\dist" || goto :failed
echo.
echo Smoke test (optional): dist\ASR-GUI.exe --selftest "%TEMP%\asr-gui-selftest.txt"
goto :eof

:nopython
echo ERROR: python.exe not found. Set ASR_PYTHON to a python executable, or create .venv.
pause
exit /b 1

:failed
echo ERROR: build failed. See the messages above.
pause
exit /b 1
