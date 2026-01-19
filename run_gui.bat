@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\activate.bat" (
  call ".venv\Scripts\activate.bat"
)
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m src.app.main
) else (
  python -m src.app.main
)
pause
