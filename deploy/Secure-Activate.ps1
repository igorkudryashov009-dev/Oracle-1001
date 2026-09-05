#Requires -Version 5.1
<#
.SYNOPSIS
  Secure combat activation for Oracle-1001 (secrets never echo / never chat).
.DESCRIPTION
  Prompts with SecureString / masked input. Pushes gas secrets straight to Rucloud
  via SSH stdin (no long-lived local .env with provider key). Optionally enables
  AIS collector, UFW allowlist, and copies prices.csv if a real path is given.
  Writes deploy/.last_activate_status.json WITHOUT secret values.
#>
param(
    [string]$RucloudHost = "45.8.230.214",
    [string]$SshUser = "root"
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$StatusPath = Join-Path $Root "deploy\.last_activate_status.json"
$Remote = "${SshUser}@${RucloudHost}"

function Read-Secret([string]$Prompt) {
    $sec = Read-Host -Prompt $Prompt -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

function Read-Optional([string]$Prompt) {
    $v = Read-Host -Prompt "$Prompt (Enter = skip)"
    if ([string]::IsNullOrWhiteSpace($v)) { return $null }
    return $v.Trim()
}

Write-Host ""
Write-Host "=== Oracle-1001 secure activation ===" -ForegroundColor Cyan
Write-Host "Secrets are NOT printed. Empty = skip that contour (honest degradation)."
Write-Host ""

$providerKey = Read-Secret "PROVIDER_API_KEY (VesselFinder userkey)"
$providerCost = Read-Optional "PROVIDER_COST_PER_CALL_USD (real tariff, e.g. 0.033)"
$aisKey = Read-Secret "AISSTREAM_API_KEY"
$sshIp = Read-Optional "Your public IP for UFW allowlist (/32)"
$pricesPath = Read-Optional "Full path to real TTF/Brent prices file (csv/xlsx/json)"

$status = [ordered]@{
    generated_at_utc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    gas_combat        = "skipped"
    gas_dry_run       = $null
    gas_success_count = $null
    ais_collector     = "skipped"
    ais_archive_start_utc = $null
    ufw_allowlist     = "skipped"
    causal            = "skipped"
    missing           = @()
    errors            = @()
}

# --- Gas combat on Rucloud ---
$hasProvider = -not [string]::IsNullOrWhiteSpace($providerKey)
$hasCost = -not [string]::IsNullOrWhiteSpace($providerCost)
if ($hasProvider -and -not $hasCost) {
    Write-Host "PROVIDER_COST_PER_CALL_USD required with API key (budget gate). Skipping gas combat." -ForegroundColor Yellow
    $status.missing += "PROVIDER_COST_PER_CALL_USD"
    $status.gas_combat = "blocked_missing_cost"
    $hasProvider = $false
}
if (-not $hasProvider) {
    $status.missing += "PROVIDER_API_KEY"
    if (-not $hasCost) { $status.missing += "PROVIDER_COST_PER_CALL_USD" }
} else {
    Write-Host "Pushing gas .env to Rucloud (stdin, no local linger)..." -ForegroundColor Green
    $keyB64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($providerKey))
    $costB64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($providerCost))
    $remoteGas = @'
set -euo pipefail
KEY=$(printf '%s' '__KEY_B64__' | base64 -d)
COST=$(printf '%s' '__COST_B64__' | base64 -d)
ENV=/opt/oracle1001/weekly_monitor/.env
touch "$ENV"
chmod 600 "$ENV"
upsert() { local k="$1" v="$2"; if grep -q "^${k}=" "$ENV" 2>/dev/null; then sed -i "s|^${k}=.*|${k}=${v}|" "$ENV"; else printf '%s=%s\n' "$k" "$v" >> "$ENV"; fi; }
upsert PROVIDER_NAME vesselfinder
upsert PROVIDER_BASE_URL 'https://api.vesselfinder.com/vessels'
upsert PROVIDER_API_KEY "$KEY"
upsert PROVIDER_COST_PER_CALL_USD "$COST"
upsert PROVIDER_MONTHLY_BUDGET_USD 50
upsert FLEET_CSV /opt/oracle1001/weekly_monitor/data/fleet_database.csv
upsert GAS_MONITOR_OUT_DIR /opt/oracle1001/weekly_monitor/features/gas_carrier_weekly
upsert GAS_MONITOR_ALLOW_DRY_RUN 0
chmod 600 "$ENV"
sed -i 's/\r$//' "$ENV"
# Clear any leftover dry-run limit for production
grep -q '^GAS_MONITOR_DRY_RUN_LIMIT=' "$ENV" && sed -i '/^GAS_MONITOR_DRY_RUN_LIMIT=/d' "$ENV" || true
systemctl start gas-monitor.service
systemctl start gas-monitor-export.service || true
python3 - <<'PY'
import json
p="/opt/oracle1001/weekly_monitor/features/gas_carrier_weekly/summary.json"
s=json.load(open(p, encoding="utf-8"))
assert "dry_run" in s, "dry_run field missing"
print("DRY_RUN="+str(s.get("dry_run")).lower())
print("SUCCESS="+str(s.get("success_count")))
print("ERRORS="+str(s.get("error_count")))
if s.get("dry_run") is not False:
    raise SystemExit(11)
if int(s.get("success_count") or 0) <= 0:
    raise SystemExit(12)
PY
'@
    $remoteGas = $remoteGas.Replace("__KEY_B64__", $keyB64).Replace("__COST_B64__", $costB64)
    $tmp = Join-Path $env:TEMP ("oracle1001_gas_" + [guid]::NewGuid().ToString("N") + ".sh")
    try {
        [IO.File]::WriteAllText($tmp, ($remoteGas -replace "`r`n", "`n"), (New-Object Text.UTF8Encoding $false))
        scp -o BatchMode=yes $tmp "${Remote}:/tmp/oracle1001_gas_activate.sh" | Out-Null
        $out = ssh -o BatchMode=yes $Remote "bash /tmp/oracle1001_gas_activate.sh; rm -f /tmp/oracle1001_gas_activate.sh"
        Write-Host $out
        if ($LASTEXITCODE -eq 0) {
            $status.gas_combat = "live"
            $status.gas_dry_run = $false
            if ($out -match "SUCCESS=(\d+)") { $status.gas_success_count = [int]$Matches[1] }
        } elseif ($LASTEXITCODE -eq 11) {
            $status.gas_combat = "failed_dry_run_still_true"
            $status.errors += "gas summary dry_run is not false"
        } elseif ($LASTEXITCODE -eq 12) {
            $status.gas_combat = "failed_success_count_zero"
            $status.errors += "gas success_count==0 with dry_run false (auth/quota?)"
        } else {
            $status.gas_combat = "failed"
            $status.errors += "gas activate exit=$LASTEXITCODE"
        }
    } finally {
        if (Test-Path $tmp) { Remove-Item -Force $tmp -ErrorAction SilentlyContinue }
        $providerKey = $null
        $keyB64 = $null
    }
}

# --- AIS collector on Rucloud (separate tree, no PROVIDER_* leakage) ---
$hasAis = -not [string]::IsNullOrWhiteSpace($aisKey)
if (-not $hasAis) {
    $status.missing += "AISSTREAM_API_KEY"
} else {
    Write-Host "Deploying/enabling AIS collector on Rucloud..." -ForegroundColor Green
    # Stage lean payload
    $stage = Join-Path $env:TEMP ("oracle1001_ais_" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $stage | Out-Null
    try {
        $aisDeploy = Join-Path $Root "deploy\ais_collector"
        Copy-Item (Join-Path $aisDeploy "*") $stage -Force
        Copy-Item (Join-Path $Root "collector.py") $stage -Force
        Copy-Item (Join-Path $Root "daily_snapshot.py") $stage -Force
        Copy-Item (Join-Path $Root "forecast.py") $stage -Force
        Copy-Item (Join-Path $Root "distance_calc.py") $stage -Force
        Copy-Item (Join-Path $Root "prepare_targets.py") $stage -Force
        Copy-Item (Join-Path $Root "config.yaml") $stage -Force
        Copy-Item (Join-Path $Root "targets.json") $stage -Force
        Copy-Item (Join-Path $Root "targets_batches.json") $stage -Force
        Copy-Item (Join-Path $Root "output\fleet_database.csv") (Join-Path $stage "fleet_database.csv") -Force
        Get-ChildItem $stage -Filter *.sh | ForEach-Object {
            $c = [IO.File]::ReadAllText($_.FullName) -replace "`r`n", "`n" -replace "`r", "`n"
            [IO.File]::WriteAllText($_.FullName, $c, (New-Object Text.UTF8Encoding $false))
        }
        $tar = Join-Path $env:TEMP "oracle1001_ais.tar.gz"
        if (Test-Path $tar) { Remove-Item $tar -Force }
        Push-Location $stage
        try { tar -czf $tar * } finally { Pop-Location }
        scp -o BatchMode=yes $tar "${Remote}:/tmp/oracle1001_ais.tar.gz" | Out-Null
        $aisB64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($aisKey))
        $aisRemote = @'
set -euo pipefail
rm -rf /tmp/oracle1001_ais && mkdir -p /tmp/oracle1001_ais
tar -xzf /tmp/oracle1001_ais.tar.gz -C /tmp/oracle1001_ais
export AISSTREAM_API_KEY=$(printf '%s' '__AIS_B64__' | base64 -d)
bash /tmp/oracle1001_ais/install_ais_collector.sh
START_UTC=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "ARCHIVE_START_UTC=$START_UTC" | tee /opt/oracle1001/ais_archive/ARCHIVE_START.txt
systemctl is-active ais-collector.service
systemctl is-enabled ais-collector.service
'@
        $aisRemote = $aisRemote.Replace("__AIS_B64__", $aisB64)
        $tmp2 = Join-Path $env:TEMP ("oracle1001_ais_act_" + [guid]::NewGuid().ToString("N") + ".sh")
        [IO.File]::WriteAllText($tmp2, ($aisRemote -replace "`r`n", "`n"), (New-Object Text.UTF8Encoding $false))
        scp -o BatchMode=yes $tmp2 "${Remote}:/tmp/oracle1001_ais_act.sh" | Out-Null
        $out2 = ssh -o BatchMode=yes $Remote "bash /tmp/oracle1001_ais_act.sh; rm -f /tmp/oracle1001_ais_act.sh /tmp/oracle1001_ais.tar.gz"
        Write-Host $out2
        if ($LASTEXITCODE -eq 0) {
            $status.ais_collector = "active"
            if ($out2 -match "ARCHIVE_START_UTC=(\S+)") { $status.ais_archive_start_utc = $Matches[1] }
        } else {
            $status.ais_collector = "failed"
            $status.errors += "ais install exit=$LASTEXITCODE"
        }
    } finally {
        if (Test-Path $stage) { Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue }
        if (Test-Path $tar) { Remove-Item -Force $tar -ErrorAction SilentlyContinue }
        if (Test-Path $tmp2) { Remove-Item -Force $tmp2 -ErrorAction SilentlyContinue }
        $aisKey = $null
        $aisB64 = $null
    }
}

# --- UFW ---
if ([string]::IsNullOrWhiteSpace($sshIp)) {
    $status.missing += "UFW_SSH_ALLOW_IP"
} else {
    Write-Host "Applying UFW allowlist $sshIp ..." -ForegroundColor Green
    $ufwCmd = "SSH_ALLOW_CIDRS='$sshIp' SSH_PORT=22 bash /opt/oracle1001/weekly_monitor/bin/setup_ufw.sh"
    ssh -o BatchMode=yes $Remote $ufwCmd
    if ($LASTEXITCODE -eq 0) { $status.ufw_allowlist = "restricted:$sshIp" }
    else { $status.ufw_allowlist = "failed"; $status.errors += "ufw failed" }
}

# --- Causal prices (local only, never fabricate) ---
if ([string]::IsNullOrWhiteSpace($pricesPath)) {
    $status.missing += "TTF_BRENT_PRICES_FILE"
    $status.causal = "awaiting"
} elseif (-not (Test-Path -LiteralPath $pricesPath)) {
    $status.causal = "blocked_path_not_found"
    $status.errors += "prices path not found"
    $status.missing += "TTF_BRENT_PRICES_FILE"
} else {
    $dest = Join-Path $Root "input\prices.csv"
    Copy-Item -LiteralPath $pricesPath -Destination $dest -Force
    Write-Host "Copied prices → input\prices.csv; running causal_analysis.py" -ForegroundColor Green
    & (Join-Path $Root "venv\Scripts\python.exe") (Join-Path $Root "causal_analysis.py")
    if ($LASTEXITCODE -eq 0) { $status.causal = "ran" } else { $status.causal = "ran_with_errors" }
}

# Pull gas artifacts + rebuild MC + ledger
try {
    & (Join-Path $Root "deploy\ruvds_gas_monitor\Sync-GasArtifacts.ps1")
} catch {
    $status.errors += "sync: $($_.Exception.Message)"
}

$status | ConvertTo-Json -Depth 5 | Set-Content -Path $StatusPath -Encoding UTF8
Write-Host ""
Write-Host "Status written to deploy\.last_activate_status.json (no secrets)." -ForegroundColor Cyan
Write-Host "Press Enter to close..."
[void](Read-Host)
