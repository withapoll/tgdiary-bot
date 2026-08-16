@echo off
chcp 65001 >nul
title tgdiary-bot
cd /d "%~dp0"

echo ============================================
echo   tgdiary-bot — агент-оппонент над каналом
echo ============================================
echo.

set "PATH=%PATH%;C:\hermes\node"

REM Проверка авторизации Claude (подписка, не API-ключ)
claude auth status --text 2>nul | findstr /C:"Login method" >nul
if errorlevel 1 (
    echo [!] Claude не авторизован.
    echo     Выполни:  claude auth login
    echo.
    pause
    exit /b 1
)

echo [+] Claude авторизован
echo [+] Запуск. Окно НЕ закрывать — бот работает, пока оно открыто.
echo     Остановить: Ctrl+C или просто закрыть окно.
echo.

:loop
python bot.py
echo.
echo [!] Бот остановился (код %errorlevel%). Перезапуск через 5 секунд...
echo     Если это не нужно — закрой окно сейчас.
timeout /t 5 >nul
goto loop
