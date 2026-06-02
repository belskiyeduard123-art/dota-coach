@echo off
title Dota Coach - server
echo ============================================
echo   DOTA COACH - starting server
echo ============================================
echo.

if not exist ".env" (
  echo [WARNING] File .env not found!
  echo.
  echo Create a .env file next to this launcher:
  echo   1. Copy the file .env.example
  echo   2. Rename the copy to .env
  echo   3. Put your YandexGPT key inside
  echo.
  pause
  exit /b
)

echo Starting server... browser opens in 3 seconds.
echo To stop the server - close this window or press Ctrl+C.
echo.

start "" /b cmd /c "timeout /t 3 >nul & start http://127.0.0.1:5000"

python app.py

pause
