# Register Windows Task Scheduler job for daily AIS snapshot (00:05 UTC)
# Run PowerShell AS ADMINISTRATOR:
#   Set-ExecutionPolicy -Scope Process Bypass
#   .\scripts\register_daily_task.ps1

$Root = Split-Path -Parent $PSScriptRoot
$Bat = Join-Path $Root "scripts\run_daily_snapshot.bat"
$PyExe = Join-Path $Root "venv\Scripts\python.exe"
$TaskName = "Oracle1001_AIS_DailySnapshot"

if (-not (Test-Path $Bat)) {
    Write-Error "run_daily_snapshot.bat not found at $Bat — aborting."
    exit 1
}
if (-not (Test-Path $PyExe)) {
    Write-Warning "venv python.exe not found at $PyExe — the task WILL fail at 00:05 UTC. Run 'python -m venv venv; venv\Scripts\pip install -r requirements.txt' first."
}

# schtasks has no native UTC trigger; convert 00:05 UTC to the machine's local
# time automatically so this script is correct regardless of the PC's timezone
# (avoid hardcoding one offset, which silently breaks if TZ ever changes).
$utcTarget = [DateTime]::UtcNow.Date.AddHours(0).AddMinutes(5)
$localTarget = $utcTarget.ToLocalTime()
$LocalTime = $localTarget.ToString("HH:mm")
$TzId = (Get-TimeZone).Id

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Error "This script must run in an ELEVATED (Administrator) PowerShell. 'schtasks /RL HIGHEST' silently fails with access-denied otherwise, and this script would then falsely report success. Right-click PowerShell -> 'Run as administrator', then re-run: .\scripts\register_daily_task.ps1"
    exit 1
}

schtasks /Create /F /TN $TaskName /TR "`"$Bat`"" /SC DAILY /ST $LocalTime /RL HIGHEST
if ($LASTEXITCODE -ne 0) {
    Write-Error "schtasks /Create failed with exit code $LASTEXITCODE -- task was NOT registered. See the error above."
    exit 1
}

Write-Host ""
Write-Host "Created task '$TaskName' daily at $LocalTime local (system TZ: $TzId) = 00:05 UTC."
Write-Host "NOTE: task runs in the current-user context, 'run only when user is logged on'"
Write-Host "      (default schtasks behavior, no stored password needed). If the PC is off"
Write-Host "      or the user is logged out at trigger time, that day's snapshot is SKIPPED"
Write-Host "      -- this is a documented gap (see README 'operational risks'), not a bug."
Write-Host ""

Write-Host "--- Verification ---"
$queryOut = schtasks /Query /TN $TaskName /V /FO LIST
if (-not $queryOut) {
    Write-Error "schtasks /Query returned nothing -- task registration cannot be verified. Aborting."
    exit 1
}
$queryOut | Select-String -Pattern "TaskName|Task To Run|Next Run Time|Scheduled Task State|Run As User|Repeat"
Write-Host ""

$taskRunLine = $queryOut | Select-String "Task To Run"
if (-not $taskRunLine) {
    Write-Warning "[CHECK] Could not find 'Task To Run' in schtasks output -- inspect manually."
} elseif ($taskRunLine.ToString() -match [regex]::Escape($Bat)) {
    Write-Host "[OK] Task action points at run_daily_snapshot.bat, which calls venv python.exe by full path (not system python)."
} else {
    Write-Warning "[CHECK] Task action does not match expected bat path -- inspect manually: $($taskRunLine.ToString())"
}

Write-Host ""
Write-Host "Full details:   schtasks /Query /TN $TaskName /V /FO LIST"
Write-Host "Run right now:  schtasks /Run /TN $TaskName"
Write-Host "Remove task:    schtasks /Delete /TN $TaskName /F"
