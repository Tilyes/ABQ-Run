@echo off
rem Stand-in for abaqus.bat used by the test suite.
rem Uses the bundled runtime when present, otherwise python on PATH.
setlocal
set "PY=%~dp0..\runtime\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%~dp0fake_solver.py" %*
