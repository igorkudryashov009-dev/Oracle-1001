# Pull gas weekly JSON from Rucloud private runner → local Mission Control inputs.
# Copies ONLY summary/results/errors/mission_control_module — never .env.
param(
    [string]$HostIp = "45.8.230.214",
    [string]$SshUser = "root",
    [string]$RemoteExport = "/opt/oracle1001/weekly_monitor/export/gas_carrier_weekly",
    [string]$LocalDir = ""
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
if (-not $LocalDir) {
    $LocalDir = Join-Path $Root "features\gas_carrier_weekly"
}
New-Item -ItemType Directory -Force -Path $LocalDir | Out-Null
$Remote = "${SshUser}@${HostIp}"

Write-Host "==> Ensuring remote export is fresh"
ssh -o BatchMode=yes $Remote "bash /opt/oracle1001/weekly_monitor/bin/export_gas_artifacts.sh"

Write-Host "==> Pulling JSON artifacts to $LocalDir"
$files = @("summary.json", "results.json", "errors.json", "mission_control_module.json")
foreach ($f in $files) {
    scp -o BatchMode=yes "${Remote}:${RemoteExport}/${f}" (Join-Path $LocalDir $f)
}

Write-Host "==> Refresh Mission Control + budget ledger"
& (Join-Path $Root "venv\Scripts\python.exe") (Join-Path $Root "budget_ledger.py")
& (Join-Path $Root "venv\Scripts\python.exe") (Join-Path $Root "build_mission_control.py")
Write-Host "Done. Open http://127.0.0.1:8765/output/mission_control.html"
