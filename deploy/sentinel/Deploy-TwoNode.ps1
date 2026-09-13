# Deploy Sentinel two-node topology from Windows workstation — FULL IMAGE BAKE.
# Node A (Korolev) 45.8.230.214 — Docker core + web :8765 (baked image, force-recreate)
# Node B (London)  185.39.19.75  — lean AIS relay (venv/systemd); sync same services SoT
#
# Prereq: SSH key BatchMode to both roots.
# Usage:
#   .\deploy\sentinel\Deploy-TwoNode.ps1
#   .\deploy\sentinel\Deploy-TwoNode.ps1 -SkipLondon
#   .\deploy\sentinel\Deploy-TwoNode.ps1 -SkipBuild   # NOT for bake — hotfix path only

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
  & ssh -o BatchMode=yes -o ConnectTimeout=25 "root@$HostName" $RemoteCmd
  if ($LASTEXITCODE -ne 0) { throw "SSH failed ($HostName) exit=$LASTEXITCODE" }
}

function Sync-Tree([string]$HostName, [string]$Dest = $RemoteRoot) {
  Write-Host "==> pack/scp sync -> root@${HostName}:${Dest}"
  Invoke-SSH $HostName "mkdir -p $Dest"
  $pack = Join-Path $env:TEMP "sentinel_deploy.tgz"
  Write-Host "    packing $pack ..."
  if (-not (Get-Command tar -ErrorAction SilentlyContinue)) {
    throw "tar not found — install Windows tar or run from Git Bash"
  }
  # NOTE: do NOT exclude *.glb — Digital Twin assets must ship (contract 1.4+)
  & tar -czf $pack `
    --exclude=venv --exclude=.git --exclude=logs --exclude=__pycache__ `
    --exclude=output/_qa_fidelity_v31 --exclude=output/.publish_snapshot `
    --exclude=assets/7000/videos --exclude=assets/1-10 `
    --exclude=*.mp4 --exclude=node_modules `
    docker-compose.yml docker-compose.prod.yml Dockerfile .dockerignore `
    docker services scripts web config.yaml requirements.txt `
    run_release.py build_sentinel_dashboard.py api_server.py `
    AGENTS.md CHANGELOG.md data deploy/sentinel `
    output/fleet_database.csv output/fleet_oil_tankers.csv output/fleet_database_full.csv `
    output/sentinel_dashboard.html output/js output/css output/assets `
    output/archive/api_status.json output/models `
    assets/7000 2>$null
  if (-not (Test-Path $pack)) { throw "pack failed" }
  & scp -o BatchMode=yes $pack "root@${HostName}:/tmp/sentinel_deploy.tgz"
  if ($LASTEXITCODE -ne 0) { throw "scp failed" }
  Invoke-SSH $HostName "mkdir -p $Dest && tar -xzf /tmp/sentinel_deploy.tgz -C $Dest && rm -f /tmp/sentinel_deploy.tgz"
  if (Test-Path .env) {
    & scp -o BatchMode=yes .env "root@${HostName}:${Dest}/.env"
  }
}

Write-Host "=== Sentinel two-node FULL BAKE deploy ==="
Write-Host "Root=$Root  A=$NodeA  B=$NodeB  UseCache=$UseCache SkipBuild=$SkipBuild"

Write-Host "`n--- Node A provision ---"
$prov = Get-Content -Raw (Join-Path $PSScriptRoot "provision_vps.sh") -Encoding UTF8
$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($prov))
Invoke-SSH $NodeA "echo $b64 | base64 -d > /tmp/provision_vps.sh && sed -i 's/\r$//' /tmp/provision_vps.sh && bash /tmp/provision_vps.sh --role korolev"

if (-not $SkipSync) {
  Sync-Tree $NodeA
}

if (-not $SkipBuild) {
  Write-Host "`n--- Node A FULL IMAGE BAKE + recreate ---"
  $nc = if ($UseCache) { "0" } else { "1" }
  $deploy = Get-Content -Raw (Join-Path $PSScriptRoot "deploy_korolev_sentinel.sh") -Encoding UTF8
  $b64d = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($deploy))
  Invoke-SSH $NodeA "echo $b64d | base64 -d > /tmp/deploy_korolev_sentinel.sh && sed -i 's/\r$//' /tmp/deploy_korolev_sentinel.sh && chmod +x /tmp/deploy_korolev_sentinel.sh && NO_CACHE=$nc FORCE_RECREATE=1 bash /tmp/deploy_korolev_sentinel.sh"
} else {
  Write-Warning "SkipBuild set — NOT a bake. Hotfix path only."
}

if (-not $SkipLondon) {
  Write-Host "`n--- Node B (London lean relay) sync same services SoT ---"
  try {
    Invoke-SSH $NodeB "echo $b64 | base64 -d > /tmp/provision_vps.sh && sed -i 's/\r$//' /tmp/provision_vps.sh && bash /tmp/provision_vps.sh --role london"
    # Stage full tree then rsync services into ais_ingest (B is venv/systemd, not Docker HUD)
    Sync-Tree $NodeB "/tmp/sentinel_london_stage"
    $londonInstall = Get-Content -Raw (Join-Path $PSScriptRoot "install_london_ais_relay.sh") -Encoding UTF8
    $b64l = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($londonInstall))
    Invoke-SSH $NodeB "echo $b64l | base64 -d > /tmp/install_london_ais_relay.sh && sed -i 's/\r$//' /tmp/install_london_ais_relay.sh && STAGE_DIR=/tmp/sentinel_london_stage bash /tmp/install_london_ais_relay.sh"
    # Prove dual_gate / archive honesty landed on B
    Invoke-SSH $NodeB "grep -q 'DISK_FREE_MIN_PCT' /opt/oracle1001/ais_ingest/services/dual_gate.py && grep -q 'OSINT REGISTRY' /opt/oracle1001/ais_ingest/services/archive_service.py && ! grep -q 'PREMIUM SATELLITE' /opt/oracle1001/ais_ingest/services/archive_service.py && echo NODE_B_SOT_OK"
  } catch {
    Write-Warning "Node B sync/install failed: $_"
  }
}

Write-Host "`n=== External health check (Node A) ==="
try {
  $h = Invoke-RestMethod -Uri "http://${NodeA}:8765/output/api/v1/health" -TimeoutSec 25
  Write-Host ("pipeline={0} fleet={1} coverage={2} disk_free={3} active={4}" -f `
    $h.pipeline_health_status, $h.fleet_sample_status, $h.top500_live_coverage, $h.disk_free_pct, $h.active_node)
} catch {
  Write-Warning "External health not reachable yet: $_"
}

Write-Host "Done. HUD: http://${NodeA}:8765/output/sentinel_dashboard.html?sheet=top10"
Write-Host "Bake contract: hotfixes must survive docker compose down && up --force-recreate"
