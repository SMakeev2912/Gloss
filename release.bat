@echo off
chcp 65001 >nul
rem Выпуск новой версии Gloss: release.bat [minor ^| major ^| 1.2.3]. Подробности в tools\release.py.
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
.venv\Scripts\python.exe tools\release.py %*
set CODE=%ERRORLEVEL%
exit /b %CODE%
