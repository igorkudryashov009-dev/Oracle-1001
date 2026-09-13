#Requires -Version 5.1
<#
.SYNOPSIS
  Sentinel SRE one-click controller (Windows → dual-node SSH).
.DESCRIPTION
  Canonical paths only:
    App:     /opt/oracle1001/sentinel
    Health:  http://HOST:8765/output/api/v1/health
    Volume:  sentinel_data_sqlite (named — NOT host bind)
    Heal:    /opt/oracle1001/deploy/sentinel/heal_node.sh

  DO NOT use /opt/sentinel or /api/v1/health (stale prompt paths → 404).

.EXAMPLE
  .\deploy\sentinel\Sentinel-SreController.ps1 -Action Health
  .\deploy\sentinel\Sentinel-SreController.ps1 -Action HealA
  .\deploy\sentinel\Sentinel-SreController.ps1 -Action Status
#>
[CmdletBinding()]
param(
  [ValidateSet('Health', 'HealA', 'HealB', 'Status', 'ExportDb', 'RecoverDb')]
  [string]$Action = 'Status',
  [string]$NodeA = '45.8.230.214',
  [string]$NodeB = '185.39.19.75',
  [string]$SshOpts = '-o BatchMode=yes -o ConnectTimeout=25 -o StrictHostKeyChecking=accept-new'
)

$ErrorActionPreference = 'Continue'
$HealthUrl = "http://${NodeA}:8765/output/api/v1/health"
$DashUrl   = "http://${NodeA}:8765/output/sentinel_dashboard.html?sheet=top10"
$HealA     = '/opt/oracle1001/deploy/sentinel/heal_node.sh --role korolev'
$HealB     = '/opt/oracle1001/deploy/sentinel/heal_node.sh --role london'
$Export    = '/opt/oracle1001/deploy/sentinel/export_ais_db_to_host.sh'
$Recover   = '/opt/oracle1001/deploy/sentinel/hard_recover_ais_db.sh'

function Invoke-Ssh([string]$HostIp, [string]$Remote) {
  $cmd = "export DEBIAN_FRONTEND=noninteractive; export NEEDRESTART_MODE=a; $Remote"
  & ssh $SshOpts.Split(' ') "root@$HostIp" $cmd
}

function Get-ExtHealth {
  try {
    $h = Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 20
    [pscustomobject]@{
      pipeline = $h.pipeline_health_status
      mode     = $h.source_mode
      replica  = $h.replica.status
      fleet    = $h.fleet_sample_status
      N        = $h.top500_live_coverage
      note     = 'fleet INSUFFICIENT/N low = G3 informational (does not block)'
    }
  } catch {
    [pscustomobject]@{ error = $_.Exception.Message }
  }
}

Write-Host "=== Sentinel SRE | Action=$Action | A=$NodeA B=$NodeB ==="
Write-Host "HUD: $DashUrl"
Write-Host "NOTE: use /output/api/v1/health (NOT /api/v1/health); app=/opt/oracle1001/sentinel"

switch ($Action) {
  'Health' {
    Get-ExtHealth | Format-List
    Invoke-Ssh $NodeA "curl -fsS http://127.0.0.1:8765/output/api/v1/health | head -c 500; echo"
  }
  'HealA' {
    Invoke-Ssh $NodeA "bash $HealA"
    Start-Sleep -Seconds 3
    Get-ExtHealth | Format-List
  }
  'HealB' {
    Invoke-Ssh $NodeB "bash $HealB"
  }
  'ExportDb' {
    Invoke-Ssh $NodeA "bash $Export"
  }
  'RecoverDb' {
    Write-Host 'WARNING: stops writers briefly for VACUUM/hard recover' -ForegroundColor Yellow
    Invoke-Ssh $NodeA "bash $Recover"
    Start-Sleep -Seconds 5
    Get-ExtHealth | Format-List
  }
  'Status' {
    Get-ExtHealth | Format-List
    Write-Host '--- Node A ---'
    Invoke-Ssh $NodeA 'docker ps --format "table {{.Names}}\t{{.Status}}" 2>/dev/null; docker exec sentinel-core sqlite3 /app/история1/sentinel_ais.db "PRAGMA integrity_check;" 2>/dev/null | head -n 1; crontab -l 2>/dev/null | grep -E "heal|export" || echo no_cron; free -m | head -n 2'
    Write-Host '--- Node B ---'
    Invoke-Ssh $NodeB "systemctl is-active aisstream-connector.service 2>/dev/null || echo inactive; swapon --show | head -n 3; ssh -o BatchMode=yes -o ConnectTimeout=10 root@$NodeA echo hop_ok"
  }
}

Write-Host 'DONE' -ForegroundColor Green
