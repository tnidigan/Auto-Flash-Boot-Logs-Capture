<#
Wrapper for Task Scheduler: runs edl_flash.py unattended (--yes skips the
flash confirmation prompt, since nobody's at the keyboard at 10AM) and logs
all output to a timestamped file under .\Scheduled-Logs\, since the console
output would otherwise just vanish when Task Scheduler runs this headless.

Register once with (elevated PowerShell):
    schtasks /Create /TN "Nightly Boot Flash" /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"C:\path\to\run_daily_flash.ps1\"" /SC DAILY /ST 10:00 /RL HIGHEST

Or use Task Scheduler's GUI (taskschd.msc) with the same action/trigger.
#>

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

$LogDir = Join-Path $ScriptDir "Scheduled-Logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$Timestamp = Get-Date -Format "yyyy-MM-dd_HH-mm-ss"
$LogFile = Join-Path $LogDir "flash_$Timestamp.log"

python3 .\edl_flash.py --yes *> $LogFile
$ExitCode = $LASTEXITCODE

Write-Output "Exit code: $ExitCode (see $LogFile)"
exit $ExitCode
