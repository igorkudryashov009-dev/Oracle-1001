# Deploy weekly gas monitor to Rucloud (private runner) via OpenSSH + tar.
param(
    [string]$HostIp = "45.8.230.214",
    [string]$SshUser = "root",
    [string]$SshAllowCidrs = "",
    [string]$ProviderApiKey = "",
    [string]$ProviderCostPerCallUsd = "0.05",
    [ValidateSet("0", "1")]
    [string]$AllowDryRun = "1"
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Stage = Join-Path $env:TEMP "oracle1001_gas_monitor_stage"
$Tarball = Join-Path $env:TEMP "oracle1001_gas_monitor.tar.gz"
$Remote = "${SshUser}@${HostIp}"

Write-Host "==> Staging from $Root"
if (Test-Path $Stage) { Remove-Item -Recurse -Force $Stage }
New-Item -ItemType Directory -Path $Stage | Out-Null

@(
    "requirements-monitor.txt",
    "env.template",
    "run_gas_monitor.sh",
    "setup_ufw.sh",
    "install.sh",
    "gas-monitor.service",
    "gas-monitor.timer",
    "README_VPS_GAS_MONITOR.md"
) | ForEach-Object {
    Copy-Item (Join-Path $PSScriptRoot $_) (Join-Path $Stage $_) -Force
}
Copy-Item (Join-Path $Root "weekly_gas_carrier_monitor.py") (Join-Path $Stage "weekly_gas_carrier_monitor.py") -Force
Copy-Item (Join-Path $Root "output\fleet_database.csv") (Join-Path $Stage "fleet_database.csv") -Force

Get-ChildItem $Stage -File | Where-Object { $_.Extension -in ".sh", ".service", ".timer" -or $_.Name -eq "install.sh" } | ForEach-Object {
    $c = [System.IO.File]::ReadAllText($_.FullName) -replace "`r`n", "`n" -replace "`r", "`n"
    $utf8NoBom = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($_.FullName, $c, $utf8NoBom)
}

Write-Host "==> Creating tarball"
if (Test-Path $Tarball) { Remove-Item -Force $Tarball }
Push-Location $Stage
try { & tar -czf $Tarball * } finally { Pop-Location }

Write-Host "==> Uploading to $Remote"
ssh -o BatchMode=yes $Remote "rm -rf /tmp/oracle1001_gas_monitor /tmp/oracle1001_gas_monitor.tar.gz; mkdir -p /tmp/oracle1001_gas_monitor"
scp -o BatchMode=yes $Tarball "${Remote}:/tmp/oracle1001_gas_monitor.tar.gz"

$remoteInstall = @'
set -euo pipefail
tar -xzf /tmp/oracle1001_gas_monitor.tar.gz -C /tmp/oracle1001_gas_monitor
chmod +x /tmp/oracle1001_gas_monitor/*.sh
for s in /tmp/oracle1001_gas_monitor/*.sh; do
  echo "bash -n $s"
  bash -n "$s"
done
export SSH_ALLOW_CIDRS="__SSH_ALLOW_CIDRS__"
bash /tmp/oracle1001_gas_monitor/install.sh
'@
$remoteInstall = $remoteInstall.Replace("__SSH_ALLOW_CIDRS__", $SshAllowCidrs)
$remoteInstallPath = Join-Path $env:TEMP "oracle1001_remote_install.sh"
[System.IO.File]::WriteAllText($remoteInstallPath, ($remoteInstall -replace "`r`n", "`n"), (New-Object System.Text.UTF8Encoding $false))
scp -o BatchMode=yes $remoteInstallPath "${Remote}:/tmp/oracle1001_remote_install.sh"
ssh -o BatchMode=yes $Remote "bash /tmp/oracle1001_remote_install.sh"

if ($ProviderApiKey -and ($ProviderApiKey -notlike "YOUR_*")) {
    Write-Host "==> Upserting PROVIDER_API_KEY on remote .env"
    $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($ProviderApiKey))
    $envUpsert = @"
set -euo pipefail
KEY=`$(printf '%s' '$b64' | base64 -d)
ENV=/opt/oracle1001/weekly_monitor/.env
touch "`$ENV"; chmod 600 "`$ENV"
upsert() { local k=`$1 v=`$2; if grep -q "^`${k}=" "`$ENV"; then sed -i "s|^`${k}=.*|`${k}=`${v}|" "`$ENV"; else printf '%s=%s\n' "`$k" "`$v" >> "`$ENV"; fi; }
upsert PROVIDER_API_KEY "`$KEY"
upsert PROVIDER_COST_PER_CALL_USD '$ProviderCostPerCallUsd'
upsert PROVIDER_MONTHLY_BUDGET_USD '50'
upsert PROVIDER_NAME 'vesselfinder'
upsert PROVIDER_BASE_URL 'https://api.vesselfinder.com/vessels'
upsert FLEET_CSV '/opt/oracle1001/weekly_monitor/data/fleet_database.csv'
upsert GAS_MONITOR_OUT_DIR '/opt/oracle1001/weekly_monitor/features/gas_carrier_weekly'
upsert GAS_MONITOR_ALLOW_DRY_RUN '$AllowDryRun'
chmod 600 "`$ENV"
"@
    $envUpsertPath = Join-Path $env:TEMP "oracle1001_env_upsert.sh"
    [System.IO.File]::WriteAllText($envUpsertPath, ($envUpsert -replace "`r`n", "`n"), (New-Object System.Text.UTF8Encoding $false))
    scp -o BatchMode=yes $envUpsertPath "${Remote}:/tmp/oracle1001_env_upsert.sh"
    ssh -o BatchMode=yes $Remote "bash /tmp/oracle1001_env_upsert.sh"
}

Write-Host "==> Manual oneshot verification"
$verify = @'
set -euo pipefail
systemctl start gas-monitor.service
systemctl status gas-monitor.timer --no-pager -l || true
systemctl list-timers gas-monitor.timer --no-pager
systemctl show gas-monitor.service -p Result -p ExecMainStatus --no-pager
test -f /opt/oracle1001/weekly_monitor/features/gas_carrier_weekly/summary.json
python3 - <<'PY'
import json
p="/opt/oracle1001/weekly_monitor/features/gas_carrier_weekly/summary.json"
s=json.load(open(p, encoding="utf-8"))
print("summary_ok", s.get("status"), "coverage", s.get("coverage_of_top_n_target_pct"), "dry_run", s.get("dry_run"))
PY
echo "ALERTS:"; tail -n 5 /opt/oracle1001/weekly_monitor/logs/alerts.log 2>/dev/null || echo "(none yet)"
ufw status | head -n 25
'@
$verifyPath = Join-Path $env:TEMP "oracle1001_verify.sh"
[System.IO.File]::WriteAllText($verifyPath, ($verify -replace "`r`n", "`n"), (New-Object System.Text.UTF8Encoding $false))
scp -o BatchMode=yes $verifyPath "${Remote}:/tmp/oracle1001_verify.sh"
ssh -o BatchMode=yes $Remote "bash /tmp/oracle1001_verify.sh"

Write-Host "==> Deploy finished."
