@echo off
rem ABQ-Run launcher.
rem IMPORTANT: keep this file pure ASCII. Mixing "chcp 65001" with
rem non-ASCII text makes cmd.exe mis-read the following lines.
chcp 65001 >nul
title ABQ-Run - Abaqus Job Queue
setlocal

rem ---------------------------------------------------------------------
rem 1) bundled runtime: runtime\python.exe (official embeddable Python)
rem    This is the normal path - nothing to install on a new machine.
rem ---------------------------------------------------------------------
set "PY="
if exist "%~dp0runtime\python.exe" (
  "%~dp0runtime\python.exe" -c "import sys" >nul 2>&1 && set "PY=%~dp0runtime\python.exe"
)

rem ---------------------------------------------------------------------
rem 2) fall back to a Python 3 already installed on this machine
rem ---------------------------------------------------------------------
if not defined PY (
  for %%P in (
    "D:\miniconda3\python.exe"
    "C:\miniconda3\python.exe"
    "%USERPROFILE%\miniconda3\python.exe"
    "%USERPROFILE%\anaconda3\python.exe"
    "%ProgramData%\miniconda3\python.exe"
    "%ProgramData%\anaconda3\python.exe"
  ) do (
    if not defined PY if exist "%%~fP" set "PY=%%~fP"
  )
)
if not defined PY (
  for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
    if not defined PY if exist "%%~fD\python.exe" set "PY=%%~fD\python.exe"
  )
)
if not defined PY (
  for /d %%D in ("C:\Python3*" "D:\Python3*") do (
    if not defined PY if exist "%%~fD\python.exe" set "PY=%%~fD\python.exe"
  )
)
rem official py launcher
if not defined PY (
  py -3 --version >nul 2>&1 && set "PY=py"
)
rem python on PATH
if not defined PY (
  python --version >nul 2>&1 && set "PY=python"
)

if not defined PY (
  echo.
  echo   [X] No Python 3 found - ABQ-Run cannot start.
  echo.
  echo   The folder "runtime" next to this file is missing or incomplete.
  echo   Copy the whole ABQ-Run folder again ^(including runtime^), or
  echo   install Python 3.8 or newer from https://www.python.org/downloads/
  echo   ^(tick "Add python.exe to PATH"^) and run this file again.
  echo.
  echo   If Python lives somewhere else, set the PY variable near the top
  echo   of this file to the full path of your python.exe.
  echo.
  pause
  exit /b 1
)

"%PY%" "%~dp0abqrun.py" %*
if errorlevel 1 pause
