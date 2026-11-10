@echo off
cd /d "%~dp0"
if not exist ".venv-gemma\Scripts\litert-lm.exe" (
  echo Install the local Gemma runtime first. See README.md.
  pause
  exit /b 1
)
echo Starting the local Gemma model server at http://127.0.0.1:9379
echo Keep this window open while using the underwriting platform.
".venv-gemma\Scripts\litert-lm.exe" serve --host 127.0.0.1 --port 9379
pause
