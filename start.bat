@echo off
rem BegoFrame: запуск интерфейса без окна консоли
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" bego.py
) else (
  start "" pythonw bego.py
)
