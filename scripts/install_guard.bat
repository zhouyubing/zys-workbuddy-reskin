@echo off
chcp 65001 >nul
title WorkBuddy 全自动方案 - 安装
setlocal enabledelayedexpansion

set "PY="
set "PYW="
if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe" (
  set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
  set "PYW=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\pythonw.exe"
)
if not defined PY (
  for /d %%D in ("%USERPROFILE%\.workbuddy\binaries\python\versions\*") do (
    if exist "%%~D\python.exe" if not defined PY (
      set "PY=%%~D\python.exe"
      set "PYW=%%~D\pythonw.exe"
    )
  )
)
if not defined PY set "PY=python"
if not defined PYW set "PYW=pythonw"

echo.
echo ============================================
echo   WorkBuddy 全自动方案 - 安装
echo ============================================
echo.
echo   正在配置，请稍候...
echo.

"%PY%" "%~dp0guard.py" install

echo.
echo   正在启动后台守护...
start "" "%PYW%" "%~dp0guard.py" run

echo.
echo ============================================
echo   配置完成。
echo   以后请双击桌面「WorkBuddy-皮肤版」启动。
echo   按任意键关闭本窗口。
echo ============================================
echo.
pause
