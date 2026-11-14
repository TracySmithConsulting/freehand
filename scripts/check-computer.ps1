<#
.SYNOPSIS
  Tell FreeHand what this computer can run (for models on this computer).

.DESCRIPTION
  FreeHand runs inside Docker, where it can't see this computer's graphics
  card or memory. This script looks, once, and saves what it found to
  vault\computer.json, which FreeHand reads to recommend an Ollama model
  that fits. It also says whether Ollama is installed.

  Nothing is sent anywhere. Run it again after changing hardware.

      powershell -ExecutionPolicy Bypass -File scripts\check-computer.ps1

  Add -InstallOllama to install Ollama too (with winget, if it isn't there).
#>
param([switch]$InstallOllama)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$out = Join-Path $root 'vault\computer.json'

$ramGb = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB, 1)

# Graphics cards. Windows' own figure stops at 4 GB, so ask NVIDIA's tool
# when there is one, then the driver's registry entry (exact, 64-bit).
$gpus = @()
$smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if ($smi) {
    foreach ($line in (& $smi.Source --query-gpu=name,memory.total --format=csv,noheader,nounits 2>$null)) {
        $parts = $line -split ',\s*'
        if ($parts.Count -ge 2) { $gpus += [pscustomobject]@{ name = $parts[0].Trim(); vram_gb = [math]::Round([double]$parts[1] / 1024, 1); vendor = 'nvidia' } }
    }
}
$classKey = 'HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}'
foreach ($k in (Get-ChildItem $classKey -ErrorAction SilentlyContinue | Where-Object { $_.PSChildName -match '^\d{4}$' })) {
    $p = Get-ItemProperty $k.PSPath -ErrorAction SilentlyContinue
    $name = $p.DriverDesc
    if (-not $name -or ($gpus | Where-Object { $_.name -eq $name })) { continue }
    $bytes = $p.'HardwareInformation.qwMemorySize'
    if (-not $bytes) { $bytes = $p.'HardwareInformation.MemorySize' }
    if ($bytes -is [byte[]]) { $bytes = [BitConverter]::ToUInt64(($bytes + (,[byte]0 * 8))[0..7], 0) }
    $vendor = if ($name -match 'NVIDIA') { 'nvidia' } elseif ($name -match 'AMD|Radeon') { 'amd' } elseif ($name -match 'Intel') { 'intel' } else { 'other' }
    $gpus += [pscustomobject]@{ name = $name; vram_gb = [math]::Round([double]$bytes / 1GB, 1); vendor = $vendor }
}

$ollamaExe = @("$env:LOCALAPPDATA\Programs\Ollama\ollama.exe", "$env:ProgramFiles\Ollama\ollama.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $ollamaExe -and $InstallOllama) {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host 'Installing Ollama (this can take a few minutes)...'
        winget install --id Ollama.Ollama --exact --accept-source-agreements --accept-package-agreements --silent
        $ollamaExe = @("$env:LOCALAPPDATA\Programs\Ollama\ollama.exe", "$env:ProgramFiles\Ollama\ollama.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
    } else {
        Write-Host 'winget is not available. Download Ollama from https://ollama.com/download instead.'
    }
}

# Where Ollama listens. Usually port 11434, but OLLAMA_HOST can move it
# (for example 127.0.0.1:11435), and FreeHand must knock on the right door.
$ollamaHost = @([Environment]::GetEnvironmentVariable('OLLAMA_HOST', 'Process'),
                [Environment]::GetEnvironmentVariable('OLLAMA_HOST', 'User'),
                [Environment]::GetEnvironmentVariable('OLLAMA_HOST', 'Machine')) | Where-Object { $_ } | Select-Object -First 1
$ollamaPort = 11434
if ($ollamaHost -and ($ollamaHost -match ':(\d+)\s*$')) { $ollamaPort = [int]$Matches[1] }

$info = [ordered]@{
    checked_at       = (Get-Date).ToUniversalTime().ToString('o')
    os               = 'windows'
    ram_gb           = $ramGb
    gpus             = @($gpus)
    ollama_installed = [bool]$ollamaExe
    ollama_port      = $ollamaPort
}
New-Item -ItemType Directory -Force -Path (Split-Path $out) | Out-Null
$info | ConvertTo-Json -Depth 4 | Set-Content -Path $out -Encoding UTF8

Write-Host "Done. FreeHand now knows this computer has $ramGb GB of memory" -NoNewline
$best = $gpus | Sort-Object vram_gb -Descending | Select-Object -First 1
if ($best) { Write-Host " and a $($best.name) graphics card with $($best.vram_gb) GB." } else { Write-Host '.' }
if ($ollamaExe) { Write-Host 'Ollama is installed.' } else { Write-Host 'Ollama is not installed yet. Run this again with -InstallOllama, or get it from https://ollama.com/download' }
Write-Host 'Go back to FreeHand and press "Check again" under Models on this computer.'
