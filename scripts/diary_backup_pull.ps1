# Pull the shooting-diary backups from the server to this Windows PC.
# Files are only added here, never deleted, so the PC keeps every weekly backup.
#
# One-time: SSH key login to the server (no password prompt), then register the weekly task:
#   powershell -ExecutionPolicy Bypass -File scripts\diary_backup_pull.ps1 -Register
param(
    [string]$Server = "root@10.30.2.2",
    [string]$RemoteDir = "/root/Flask_Telemetry/diary_backups",
    [string]$Dest = "$env:USERPROFILE\Documents\Ampumapaivakirja\backups",
    [switch]$Register
)
$ErrorActionPreference = "Stop"

if ($Register) {
    $script = $MyInvocation.MyCommand.Path
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`""
    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At 12:00
    # StartWhenAvailable: if the PC was off on Sunday, run at the next opportunity.
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries
    Register-ScheduledTask -TaskName "Ampumapaivakirja backup" -Action $action -Trigger $trigger `
        -Settings $settings -Description "Pull shooting diary backups from $Server" -Force | Out-Null
    Write-Host "Scheduled: every Sunday 12:00 (or at next start if missed)."
    exit 0
}

New-Item -ItemType Directory -Force -Path $Dest | Out-Null
$log = Join-Path $Dest "backup.log"
"$(Get-Date -Format s) pulling from ${Server}:$RemoteDir" | Add-Content $log
# BatchMode: fail instead of hanging on a password prompt if the SSH key is missing.
# scp writes to stderr; with "Stop" Windows PowerShell would treat that as a terminating error.
$ErrorActionPreference = "Continue"
& scp -q -o BatchMode=yes -o ConnectTimeout=15 "${Server}:$RemoteDir/diary-*" "$Dest" 2>&1 | ForEach-Object { "$_" } | Add-Content $log
if ($LASTEXITCODE -ne 0) {
    "$(Get-Date -Format s) FAILED (scp exit $LASTEXITCODE)" | Add-Content $log
    exit $LASTEXITCODE
}
$latest = Get-ChildItem $Dest -Filter "diary-*.json" | Sort-Object Name | Select-Object -Last 1
"$(Get-Date -Format s) ok, newest: $($latest.Name)" | Add-Content $log
Write-Host "Backups in $Dest (newest: $($latest.Name))"
