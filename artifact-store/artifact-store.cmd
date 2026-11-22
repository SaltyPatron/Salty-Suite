@echo off
setlocal
where py >nul 2>nul
if %errorlevel% equ 0 (
    py -3 "%~dp0artifact_store.py" %*
    exit /b %errorlevel%
)
python "%~dp0artifact_store.py" %*
exit /b %errorlevel%
