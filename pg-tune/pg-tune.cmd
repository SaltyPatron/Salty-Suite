@echo off
setlocal
set "HERE=%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 "%HERE%pg_tune.py" %*
    exit /b %errorlevel%
)
where python >nul 2>nul
if %errorlevel%==0 (
    python "%HERE%pg_tune.py" %*
    exit /b %errorlevel%
)
1>&2 echo pg-tune: Python 3 not found in PATH
exit /b 127
