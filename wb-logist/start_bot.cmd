@echo off
cd /d "C:\Users\User\.cline\data\workspaces\chat\wb-logist"
".venv\Scripts\python.exe" "bot_worker.py" --verbose
echo.
echo Bot stopped. Press any key to close.
pause >nul
