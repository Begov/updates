@echo off
chcp 65001 >nul
rem BegoFrame: первичная установка зависимостей в локальное окружение .venv
cd /d "%~dp0"
where python >nul 2>nul || (
  echo [!] Python не найден. Установите Python 3.11+ с python.org и отметьте "Add to PATH".
  pause & exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  echo Создаю окружение .venv ...
  python -m venv .venv || (pause & exit /b 1)
)
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt || (pause & exit /b 1)
where ffmpeg >nul 2>nul || echo [!] FFmpeg не найден в PATH: установите его (winget install Gyan.FFmpeg).
echo.
echo Готово. Запуск: start.bat
pause
