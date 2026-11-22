@echo off
setlocal
where py >nul 2>nul
if not errorlevel 1 goto run_py
where python3 >nul 2>nul
if not errorlevel 1 goto run_py3
python "%~dp0host_lock.py" %*
exit /b %errorlevel%
:run_py
py -3 "%~dp0host_lock.py" %*
exit /b %errorlevel%
:run_py3
python3 "%~dp0host_lock.py" %*
exit /b %errorlevel%
