@echo off
chcp 65001 >nul
title WorkBuddy 全自动方案 - 卸载
setlocal enabledelayedexpansion

set "PY="
if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe" (
  set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
)
if not defined PY (
  for /d %%D in ("%USERPROFILE%\.workbuddy\binaries\python\versions\*") do (
    if exist "%%~D\python.exe" if not defined PY set "PY=%%~D\python.exe"
  )
)
if not defined PY set "PY=python"

echo.
echo ============================================
echo   WorkBuddy 全自动方案 - 卸载
echo ============================================
echo.
echo   正在清理，请稍候...
echo.

"%PY%" "%~dp0guard.py" uninstall

echo.
echo ============================================
echo   清理完成。您的皮肤仍然保留，可继续使用。
echo   按任意键关闭。
echo ============================================
echo.
pause
