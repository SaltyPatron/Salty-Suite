$script = Join-Path $PSScriptRoot 'machine_env.py'
if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 $script @args
} elseif (Get-Command python3 -ErrorAction SilentlyContinue) {
    & python3 $script @args
} else {
    & python $script @args
}
exit $LASTEXITCODE
