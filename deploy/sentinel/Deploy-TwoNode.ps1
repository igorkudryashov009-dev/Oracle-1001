# Deploy Sentinel two-node topology from Windows workstation — FULL IMAGE BAKE.
# Node A (Korolev) 45.8.230.214 — Docker core + web :8765 (baked image, force-recreate)
# Node B (London)  185.39.19.75  — lean AIS relay (venv/systemd); sync same services SoT
#
# Usage:
#   .\deploy\sentinel\Deploy-TwoNode.ps1
#   .\deploy\sentinel\Deploy-TwoNode.ps1 -SkipLondon
#   .\deploy\sentinel\Deploy-TwoNode.ps1 -SkipBuild

[CmdletBinding()]
param(
  [string]$NodeA = "45.8.230.214",
  [string]$NodeB = "185.39.19.75",
  [string]$RemoteRoot = "/opt/oracle1001/sentinel",
  [switch]$SkipLondon,
  [switch]$SkipSync,
  [switch]$SkipBuild,
  [switch]$UseCache
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $Root

function Invoke-SSH([string]$HostName, [string]$RemoteCmd) {
  # Native ssh writes progress to stderr; with $ErrorActionPreference=Stop that
  # becomes a terminating NativeCommandError even when exit code is 0.
  $prev = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    & ssh -o BatchMode=yes -o ConnectTimeout=25 "root@$HostName" $RemoteCmd 2>&1 | ForEach-Object {
      if ($_ -is [System.Management.Automation.ErrorRecord]) {
        Write-Host $_.Exception.Message
      } else {
        Write-Host $_
      }
    }
    $code = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $prev
  }
  if ($code -ne 0) { throw "SSH failed ($HostName) exit=$code" }
}

function Sync-Tree([string]$HostName, [string]$Dest = $RemoteRoot, [switch]$LeanServicesOnly) {
  Write-Host "==> pack/scp sync -> root@${HostName}:${Dest} lean=$LeanServicesOnly"
  Invoke-SSH $HostName "mkdir -p $Dest"
  $pack = Join-Path $env:TEMP $(if ($LeanServicesOnly) { "sentinel_london_sot.tgz" } else { "sentinel_deploy.tgz" })
  Write-Host "    packing $pack ..."
  if (-not (Get-Command tar -ErrorAction SilentlyContinue)) {
    throw "tar not found - install Windows tar or use Git Bash"
  }
  if ($LeanServicesOnly) {
    # London AIS relay: identical services SoT only (no HUD GLB/mp4 volume).
    & tar -czf $pack `
      --exclude=__pycache__ --exclude=*.pyc `
      services scripts api_server.py config.yaml requirements.txt `
      AGENTS.md CHANGELOG.md deploy/sentinel 2>$null
  } else {
    # NOTE: do NOT exclude *.glb - Digital Twin assets must ship (contract 1.4+)
    # NOTE: do NOT blanket-exclude *.mp4 - ARCTIC serve videos live under assets/arctic
    # Heavy Q-Flex / ortho source trees stay excluded; *_source.mp4 are provenance-only.
    & tar -czf $pack `
      --exclude=venv --exclude=.git --exclude=logs --exclude=__pycache__ `
      --exclude=output/_qa_fidelity_v31 --exclude=output/.publish_snapshot `
      --exclude=assets/7000/videos --exclude=assets/1-10 `
      --exclude=*_source.mp4 --exclude=output/assets/arctic/_probe `
      --exclude=output/assets/arctic/screenshots --exclude=node_modules `
      --exclude=nasa-mission-control `
      docker-compose.yml docker-compose.prod.yml Dockerfile .dockerignore `
      docker services scripts web config.yaml requirements.txt `
      run_release.py build_sentinel_dashboard.py api_server.py `
      AGENTS.md CHANGELOG.md data deploy/sentinel `
      output/fleet_database.csv output/fleet_oil_tankers.csv output/fleet_database_full.csv `
      output/sentinel_dashboard.html output/js output/css output/assets `
      output/archive/api_status.json output/models output/qflex_fleet_cargo.json `
      assets/7000 assets/arctic 2>$null
  }
  if (-not (Test-Path $pack)) { throw "pack failed" }
  $prev = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    & scp -o BatchMode=yes -o ConnectTimeout=60 $pack "root@${HostName}:/tmp/$(Split-Path $pack -Leaf)" 2>&1 | ForEach-Object { Write-Host $_ }
    $scpCode = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $prev
  }
  if ($scpCode -ne 0) { throw "scp failed exit=$scpCode" }
  $remoteTar = "/tmp/$(Split-Path $pack -Leaf)"
  Invoke-SSH $HostName "mkdir -p $Dest; tar -xzf $remoteTar -C $Dest; rm -f $remoteTar"
  if ((Test-Path .env) -and -not $LeanServicesOnly) {
    & scp -o BatchMode=yes .env "root@${HostName}:${Dest}/.env"
  }
}

Write-Host "=== Sentinel two-node FULL BAKE deploy ==="
Write-Host "Root=$Root  A=$NodeA  B=$NodeB  UseCache=$UseCache SkipBuild=$SkipBuild"

Write-Host ""
Write-Host "--- Node A provision ---"
$provPath = Join-Path $PSScriptRoot "provision_vps.sh"
$provBytes = [IO.File]::ReadAllBytes($provPath)
$b64 = [Convert]::ToBase64String($provBytes)
Invoke-SSH $NodeA "echo $b64 | base64 -d > /tmp/provision_vps.sh; sed -i 's/\r`$//' /tmp/provision_vps.sh; bash /tmp/provision_vps.sh --role korolev"

if (-not $SkipSync) {
  Sync-Tree $NodeA
}

if (-not $SkipBuild) {
  Write-Host ""
  Write-Host "--- Node A FULL IMAGE BAKE + recreate ---"
  $nc = if ($UseCache) { "0" } else { "1" }
  $deployPath = Join-Path $PSScriptRoot "deploy_korolev_sentinel.sh"
  $deployBytes = [IO.File]::ReadAllBytes($deployPath)
  $b64d = [Convert]::ToBase64String($deployBytes)
  Invoke-SSH $NodeA "echo $b64d | base64 -d > /tmp/deploy_korolev_sentinel.sh; sed -i 's/\r`$//' /tmp/deploy_korolev_sentinel.sh; chmod +x /tmp/deploy_korolev_sentinel.sh; NO_CACHE=$nc FORCE_RECREATE=1 bash /tmp/deploy_korolev_sentinel.sh"
} else {
  Write-Warning "SkipBuild set - NOT a bake. Hotfix path only."
}

if (-not $SkipLondon) {
  Write-Host ""
  Write-Host "--- Node B (London lean relay) sync same services SoT ---"
  try {
    Invoke-SSH $NodeB "echo $b64 | base64 -d > /tmp/provision_vps.sh; sed -i 's/\r`$//' /tmp/provision_vps.sh; bash /tmp/provision_vps.sh --role london"
    Sync-Tree $NodeB "/tmp/sentinel_london_stage" -LeanServicesOnly
    $londonPath = Join-Path $PSScriptRoot "install_london_ais_relay.sh"
    $londonBytes = [IO.File]::ReadAllBytes($londonPath)
    $b64l = [Convert]::ToBase64String($londonBytes)
    Invoke-SSH $NodeB "echo $b64l | base64 -d > /tmp/install_london_ais_relay.sh; sed -i 's/\r`$//' /tmp/install_london_ais_relay.sh; STAGE_DIR=/tmp/sentinel_london_stage bash /tmp/install_london_ais_relay.sh"
    Invoke-SSH $NodeB "grep -q DISK_FREE_MIN_PCT /opt/oracle1001/ais_ingest/services/dual_gate.py; grep -q _resolve_live_dual_gate /opt/oracle1001/ais_ingest/services/quant_risk_service.py; grep -q 155159 /opt/oracle1001/ais_ingest/services/top10_vessels.py; grep PREMIUM /opt/oracle1001/ais_ingest/services/archive_service.py >/dev/null; if [ `$? -eq 0 ]; then echo NODE_B_HAS_PREMIUM_BAD; exit 1; fi; echo NODE_B_SOT_OK"
  } catch {
    Write-Warning "Node B sync/install failed: $_"
  }
}

Write-Host ""
Write-Host "=== External health check (Node A) ==="
try {
  $h = Invoke-RestMethod -Uri "http://${NodeA}:8765/output/api/v1/health" -TimeoutSec 25
  Write-Host ("pipeline={0} fleet={1} coverage={2} disk_free={3} active={4}" -f `
    $h.pipeline_health_status, $h.fleet_sample_status, $h.top500_live_coverage, $h.disk_free_pct, $h.active_node)
} catch {
  Write-Warning "External health not reachable yet: $_"
}

Write-Host ""
Write-Host "=== Deploy manifest verification (git tree vs Node A sha256) ==="
$py = $null
if (Test-Path (Join-Path $Root "venv\Scripts\python.exe")) {
  $py = Join-Path $Root "venv\Scripts\python.exe"
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
  $py = "python"
}
if (-not $py) {
  throw "Deploy manifest verify requires Python (venv\Scripts\python.exe or python on PATH)"
}
$env:SENTINEL_NODE_A = $NodeA
$env:SENTINEL_VERIFY_BASE_URL = "http://${NodeA}:8765"
$env:SENTINEL_VERIFY_LIVE = "1"
$env:SENTINEL_VERIFY_STRICT = "1"
& $py (Join-Path $Root "scripts\verify_deploy_manifest.py") --base-url "http://${NodeA}:8765" --strict
if ($LASTEXITCODE -ne 0) {
  throw "Deploy manifest FAIL - Node A is serving stale HUD assets vs this working tree (exit=$LASTEXITCODE)"
}
Write-Host "Deploy manifest PASS - Node A bytes match working tree"

Write-Host "Done. HUD: http://${NodeA}:8765/output/sentinel_dashboard.html?sheet=top10"
Write-Host "Bake contract: hotfixes must survive compose down + up --force-recreate"
