# Sentinel zero-touch local boot (Contract 1.8.0-ops-gis-sot)
# Always runs from repo root — safe to call from any cwd (incl. $HOME).
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
$Py = Join-Path $Root "venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { $Py = "python" }
& $Py (Join-Path $Root "run_dev.py") @args
exit $LASTEXITCODE
