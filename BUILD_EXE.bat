@echo off
setlocal
cd /d "%~dp0"
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist Witcher2_Universal_Dual_Subtitles.spec del /q Witcher2_Universal_Dual_Subtitles.spec
where py.exe >nul 2>nul
if %errorlevel%==0 (set "PY=py -3") else (set "PY=python")
%PY% -m ensurepip --upgrade >nul 2>nul
%PY% -m pip install --upgrade pyinstaller
if errorlevel 1 (echo Failed to install PyInstaller.&pause&exit /b 1)
%PY% -m PyInstaller --noconfirm --clean --onedir --windowed --name "Witcher2_Universal_Dual_Subtitles" --add-data "Gibbed RED Tools;Gibbed RED Tools" Witcher2_Dual_Subtitles_Tool.py
if errorlevel 1 (echo BUILD FAILED.&pause&exit /b 1)
echo.
echo BUILD COMPLETE.
echo dist\Witcher2_Universal_Dual_Subtitles\Witcher2_Universal_Dual_Subtitles.exe
echo.
pause
