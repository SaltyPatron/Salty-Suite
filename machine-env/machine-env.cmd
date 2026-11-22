@echo off
setlocal
where py >nul 2>nul
if not errorlevel 1 goto run_py
where python3 >nul 2>nul
if not errorlevel 1 goto run_py3
python "%~dp0machine_env.py" %*
exit /b %errorlevel%
:run_py
py -3 "%~dp0machine_env.py" %*
exit /b %errorlevel%
:run_py3
python3 "%~dp0machine_env.py" %*
exit /b %errorlevel%
