@echo off
setlocal
where py >nul 2>nul
if %errorlevel% equ 0 (
    py -3 "%~dp0deps_installer.py" %*
    exit /b %errorlevel%
)
python "%~dp0deps_installer.py" %*
exit /b %errorlevel%
