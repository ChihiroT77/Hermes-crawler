@echo off
setlocal

echo.
echo   Hermes-crawler 爬虫插件 - 一键安装
echo   ==========================
echo.

where powershell >nul 2>nul
if errorlevel 1 (
    echo   [不行] 这个系统里找不到 PowerShell。
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set RC=%ERRORLEVEL%

if "%MC_NO_PAUSE%"=="1" exit /b %RC%
if not "%RC%"=="0" (
    echo.
    echo   安装没成功，退出码 %RC%。上面有具体原因。
)
exit /b %RC%
