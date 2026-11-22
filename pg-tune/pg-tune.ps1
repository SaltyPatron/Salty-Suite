#!/usr/bin/env pwsh
$script = Join-Path $PSScriptRoot 'pg_tune.py'
if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 $script @args
    exit $LASTEXITCODE
}
if (Get-Command python3 -ErrorAction SilentlyContinue) {
    & python3 $script @args
    exit $LASTEXITCODE
}
if (Get-Command python -ErrorAction SilentlyContinue) {
    & python $script @args
    exit $LASTEXITCODE
}
[Console]::Error.WriteLine('pg-tune: Python 3 not found in PATH')
exit 127
