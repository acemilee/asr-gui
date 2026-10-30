#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# asr-gui - Linux / macOS dependency bootstrap
#
# Creates .venv in the repository root and installs requirements.txt into it.
# Requires Python 3.9+. No sudo is used: everything stays inside .venv.
#
# Usage:  ./setup.sh
# ---------------------------------------------------------------------------
set -eu

cd "$(dirname "$0")"

info() { printf '[asr-gui] %s\n' "$*"; }
die() { printf '[asr-gui] ERROR: %s\n' "$*" >&2; exit 1; }

echo "=========================================================="
echo "  asr-gui setup"
echo "=========================================================="
echo

# --- 1. Locate a Python 3.9+ interpreter -----------------------------------
PYTHON_BIN="${PYTHON_BIN:-}"
if [ -z "$PYTHON_BIN" ]; then
    for candidate in python3.13 python3.12 python3.11 python3.10 python3.9 python3 python; do
        if command -v "$candidate" >/dev/null 2>&1; then
            if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 9) else 1)' >/dev/null 2>&1; then
                PYTHON_BIN="$candidate"
                break
            fi
        fi
    done
fi
[ -n "$PYTHON_BIN" ] || die "Python 3.9+ not found. Install it, or set PYTHON_BIN=/path/to/python3."

info "[1/4] Python: $(command -v "$PYTHON_BIN")"
"$PYTHON_BIN" -c 'import sys; print("      Python " + sys.version.split()[0])'

# --- 2. Create (or reuse) the virtual environment --------------------------
VENV_DIR="$PWD/.venv"
VENV_PY="$VENV_DIR/bin/python"

if [ -x "$VENV_PY" ]; then
    info "[2/4] Reusing existing virtual environment: .venv"
else
    info "[2/4] Creating virtual environment: .venv"
    "$PYTHON_BIN" -m venv "$VENV_DIR"
fi
[ -x "$VENV_PY" ] || die "Virtual environment creation failed ($VENV_PY missing)."

# --- 3. Install dependencies ----------------------------------------------
info "[3/4] Upgrading pip ..."
"$VENV_PY" -m pip install --upgrade pip --disable-pip-version-check

info "[4/4] Installing requirements.txt ..."
"$VENV_PY" -m pip install -r requirements.txt

# tkinter is part of the Python distribution but is packaged separately on
# several Linux distributions; the GUI cannot start without it.
if ! "$VENV_PY" -c 'import tkinter' >/dev/null 2>&1; then
    info "WARNING: tkinter is missing - the GUI will not start."
    info "  Debian/Ubuntu: sudo apt install python3-tk"
    info "  Fedora:        sudo dnf install python3-tkinter"
    info "  macOS:         brew install python-tk"
fi

echo
echo "[OK] Setup complete."
echo
echo "  Run it with:"
echo "      .venv/bin/python asr_gui/app.py"
echo
echo "  Optional GPU support (NVIDIA, CUDA 12):"
echo "      .venv/bin/python -m pip install nvidia-cublas-cu12 nvidia-cudnn-cu12"
echo
