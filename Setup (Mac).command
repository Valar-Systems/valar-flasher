#!/bin/bash
cd "$(dirname "$0")"
echo "Installing the Ropener Flasher environment..."
python3 -m venv .venv || { echo "Python 3 not found. Install it from python.org."; read -n1; exit 1; }
".venv/bin/python" -m pip install --upgrade pip
".venv/bin/python" -m pip install esptool
echo
echo "Done. You can now double-click 'Flash Ropener (Mac).command'."
read -n1 -p "Press any key to close."
