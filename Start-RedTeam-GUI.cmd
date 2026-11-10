@echo off
cd /d "%~dp0"
if not exist ".venv-redteam\Scripts\python.exe" (
  echo Install the red-team environment first. See redteam\README.md.
  pause
  exit /b 1
)
".venv-redteam\Scripts\python.exe" -m redteam.gui %*
pause
