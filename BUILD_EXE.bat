@echo off
setlocal
cd /d "%~dp0"
if not defined W2_BUILD_PYTHON set "W2_BUILD_PYTHON=python"
"%W2_BUILD_PYTHON%" build_windows.py %*
exit /b %errorlevel%
