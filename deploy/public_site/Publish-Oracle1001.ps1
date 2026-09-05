param(
    [string]$Ld8Host = "185.39.19.75",
    [string]$SshUser = "root",
    [switch]$RequestCert
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Remote = "$SshUser@$Ld8Host"
$Stage = Join-Path $env:TEMP "oracle1001_public_site"
$Tar = Join-Path $env:TEMP "oracle1001_public_site.tar.gz"

Write-Host "==> Rebuild Mission Control locally"
& (Join-Path $Root "venv\Scripts\python.exe") (Join-Path $Root "budget_ledger.py")
& (Join-Path $Root "venv\Scripts\python.exe") (Join-Path $Root "build_mission_control.py")

Write-Host "==> Stage static site (output only)"
if (Test-Path $Stage) { Remove-Item -Recurse -Force $Stage }
New-Item -ItemType Directory -Path (Join-Path $Stage "site\output") | Out-Null
$copy = @(
    "mission_control.html", "dashboard.html", "history_dashboard.html",
    "forecast_dashboard.html", "design_system.css", "dwt_filter_sort.js"
)
foreach ($f in $copy) {
    $src = Join-Path $Root ("output\" + $f)
    if (Test-Path $src) {
        Copy-Item $src (Join-Path $Stage ("site\output\" + $f)) -Force
    }
}
Copy-Item (Join-Path $PSScriptRoot "install_nginx_site.sh") (Join-Path $Stage "install_nginx_site.sh") -Force
$sh = Join-Path $Stage "install_nginx_site.sh"
$txt = [IO.File]::ReadAllText($sh)
$txt = $txt.Replace("`r`n", "`n").Replace("`r", "`n")
[IO.File]::WriteAllText($sh, $txt, (New-Object Text.UTF8Encoding $false))

if (Test-Path $Tar) { Remove-Item $Tar -Force }
Push-Location $Stage
try { tar -czf $Tar * } finally { Pop-Location }

Write-Host ("==> Upload to LD8 " + $Ld8Host)
scp -o BatchMode=yes $Tar ($Remote + ":/tmp/oracle1001_public_site.tar.gz")

$remotePath = Join-Path $env:TEMP "oracle1001_pub_install.sh"
$remoteBody = @(
    "set -euo pipefail",
    "rm -rf /tmp/oracle1001_public_site",
    "mkdir -p /tmp/oracle1001_public_site",
    "tar -xzf /tmp/oracle1001_public_site.tar.gz -C /tmp/oracle1001_public_site",
    "chmod +x /tmp/oracle1001_public_site/install_nginx_site.sh",
    "bash /tmp/oracle1001_public_site/install_nginx_site.sh",
    "test ! -f /var/www/oracle1001/.env",
    "echo PUBLISH_OK",
    "curl -sI -H 'Host: oracle1001.xyz' http://127.0.0.1/ | head -n 8"
) -join "`n"
[IO.File]::WriteAllText($remotePath, ($remoteBody + "`n"), (New-Object Text.UTF8Encoding $false))
scp -o BatchMode=yes $remotePath ($Remote + ":/tmp/oracle1001_pub_install.sh")
ssh -o BatchMode=yes $Remote "bash /tmp/oracle1001_pub_install.sh"

Write-Host ""
Write-Host "DNS required before TLS (RuVDS panel for zone oracle1001.xyz):" -ForegroundColor Yellow
Write-Host "  A    apex  185.39.19.75"
Write-Host "  A    apex  185.39.19.231   (optional 2nd)"
Write-Host "  A    www   185.39.19.75"
Write-Host ""

if ($RequestCert) {
    Write-Host "==> Checking DNS before certbot"
    $resolved = ssh -o BatchMode=yes $Remote "dig +short oracle1001.xyz A"
    Write-Host ("dig A: " + $resolved)
    if ($resolved -notmatch "185\.39\.19\.(75|231)") {
        Write-Host "DNS not pointing to LD8 yet - skip certbot (honest)." -ForegroundColor Yellow
    } else {
        $certPath = Join-Path $env:TEMP "oracle1001_certbot.sh"
        $certBody = @(
            "set -euo pipefail",
            "certbot --nginx -d oracle1001.xyz -d www.oracle1001.xyz --non-interactive --agree-tos --register-unsafely-without-email --redirect"
        ) -join "`n"
        [IO.File]::WriteAllText($certPath, ($certBody + "`n"), (New-Object Text.UTF8Encoding $false))
        scp -o BatchMode=yes $certPath ($Remote + ":/tmp/oracle1001_certbot.sh")
        ssh -o BatchMode=yes $Remote "bash /tmp/oracle1001_certbot.sh"
    }
}

Write-Host "Done. Test HTTP via IP with Host header oracle1001.xyz"
