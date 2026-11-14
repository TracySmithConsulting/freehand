<#
.SYNOPSIS
  Start FreeHand's WhatsApp bridge automatically when you sign in to Windows.

.DESCRIPTION
  Creates a Task Scheduler task, "FreeHand WhatsApp bridge", for your own
  Windows account (no administrator rights needed). It starts the bridge
  with no window each time you sign in, and starts it again if it stops.
  The bridge's messages go to %LOCALAPPDATA%\FreeHand\whatsapp-bridge.log.

  Run it once, from the FreeHand folder:
      powershell -ExecutionPolicy Bypass -File scripts\windows-bridge-autostart.ps1

  To stop starting it automatically:
      powershell -ExecutionPolicy Bypass -File scripts\windows-bridge-autostart.ps1 -Remove
#>
param([switch]$Remove)

$ErrorActionPreference = 'Stop'
$taskName = 'FreeHand WhatsApp bridge'

if ($Remove) {
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        Write-Host "Done. The WhatsApp bridge won't start by itself any more."
    } else {
        Write-Host "There was nothing to remove: the bridge wasn't set to start by itself."
    }
    return
}

$bridgeDir = Join-Path (Split-Path -Parent $PSScriptRoot) 'freehand\bridge'
$launcher = Join-Path $bridgeDir 'start-hidden.vbs'
if (-not (Test-Path $launcher)) { throw "Can't find $launcher. Run this from inside the FreeHand folder." }
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    throw "Node.js isn't installed (or isn't on the PATH). Install it from nodejs.org, then run this again."
}
if (-not (Test-Path (Join-Path $bridgeDir '.env'))) {
    Write-Warning "The bridge has no .env file yet, so it will stop straight away. Set the bridge up first (freehand\bridge\README.md)."
}

$action = New-ScheduledTaskAction -Execute "$env:WINDIR\System32\wscript.exe" `
    -Argument "`"$launcher`"" -WorkingDirectory $bridgeDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
# Restart up to 999 times, a minute apart; never stop it for running long;
# run on battery too.
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description 'Connects FreeHand to WhatsApp. Created by scripts\windows-bridge-autostart.ps1.' -Force | Out-Null
Write-Host "Done. The WhatsApp bridge will now start by itself whenever you sign in to Windows."
Write-Host "Its messages go to $env:LOCALAPPDATA\FreeHand\whatsapp-bridge.log"
