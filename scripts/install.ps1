# FreeHand for Windows, in one line (PowerShell):
#
#   irm https://github.com/TracySmithConsulting/freehand/releases/latest/download/install.ps1 | iex
#
# It downloads the FreeHand app (FreeHand.exe) and opens it. The app does
# the rest, in screens a screen reader reads: it checks the computer,
# switches on what Windows needs, installs FreeHand and opens it. Running
# this again gets the newest app.
#
# Settings (optional):
#   $env:FREEHAND_APP_DIR    where the app goes (default: your Programs folder)
#   $env:FREEHAND_NO_START   "1" downloads the app without opening it
#
# docs/design/granny-installer.md, step 4.
#Requires -Version 5.1

[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

# In a function, so a problem ends it with `return`: `irm | iex` runs in the
# person's own PowerShell window, and `exit` would close it before they
# could read why.
function Install-FreeHand {
    # Inside here only, so the person's own window is left as it was.
    $ErrorActionPreference = "Stop"
    $ProgressPreference = "SilentlyContinue"  # Windows PowerShell's progress bar makes downloads very slow

    $url = "https://github.com/TracySmithConsulting/freehand/releases/download/helper-latest/FreeHand.exe"
    $dir = if ($env:FREEHAND_APP_DIR) { $env:FREEHAND_APP_DIR } else { Join-Path $env:LOCALAPPDATA "Programs\FreeHand" }
    $exe = Join-Path $dir "FreeHand.exe"
    $download = "$exe.download"

    function Stop-Install($why) {
        Write-Host ""
        Write-Host "FreeHand wasn't installed: $why"
    }

    Write-Host ""
    Write-Host "Downloading the FreeHand app..."
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    try { Invoke-WebRequest -Uri $url -OutFile $download -UseBasicParsing }
    catch { Remove-Item $download -ErrorAction SilentlyContinue; Stop-Install "the app couldn't be downloaded. Check the internet connection, then try again. ($($_.Exception.Message))"; return }

    # A Windows program starts with "MZ"; anything else (an error page, say) isn't the app.
    $file = [IO.File]::OpenRead($download)  # works in Windows PowerShell and PowerShell 7
    try { $b0 = $file.ReadByte(); $b1 = $file.ReadByte() } finally { $file.Close() }
    if ((Get-Item $download).Length -lt 50KB -or $b0 -ne 0x4D -or $b1 -ne 0x5A) {
        Remove-Item $download -ErrorAction SilentlyContinue
        Stop-Install "what was downloaded isn't the FreeHand app, so it wasn't used."; return
    }

    $signature = Get-AuthenticodeSignature -FilePath $download
    if ($signature.Status -eq "Valid") {
        Write-Host "It's signed by $($signature.SignerCertificate.GetNameInfo('SimpleName', $false))."
    } else {
        Write-Host "It isn't signed yet (that's coming), so Windows may ask whether to run it."
    }

    try { Move-Item -Path $download -Destination $exe -Force }
    catch {
        Remove-Item $download -ErrorAction SilentlyContinue
        Stop-Install "the FreeHand app is already open, so it can't be replaced. Quit it (right-click its icon by the clock, then Quit), then try again."; return
    }
    Write-Host "The FreeHand app is in $dir."

    if ($env:FREEHAND_NO_START -eq "1") { return }
    Write-Host "Opening it. Its screens take you through the rest."
    Start-Process -FilePath $exe
}

Install-FreeHand
