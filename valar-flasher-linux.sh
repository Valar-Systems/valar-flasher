#!/bin/bash
cd "$(dirname "$0")"
if [ ! -x ".venv/bin/python" ]; then
  echo "First-time setup: creating environment and installing the flasher..."
  python3 -m venv .venv || { echo "Install python3 + python3-venv + python3-tk first."; exit 1; }
  ".venv/bin/python" -m pip install --upgrade pip >/dev/null
  ".venv/bin/python" -m pip install esptool
fi
".venv/bin/python" valar_flasher.py
