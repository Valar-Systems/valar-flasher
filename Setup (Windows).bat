@echo off
cd /d "%~dp0"
echo Installing the Ropener Flasher environment...
py -3 -m venv .venv 2>nul || python -m venv .venv
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install esptool
echo.
echo Done. You can now double-click "Flash Ropener (Windows).bat".
pause
