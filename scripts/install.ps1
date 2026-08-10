# FreeHand Installer for Windows
# Usage: iwr https://freehand.tracysmith.co.za/install.ps1 -useb | iex
#Requires -Version 5.1

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = "Stop"

$INSTALL_DIR = "$env:USERPROFILE\.freehand"
$SCRIPT_NAME = "FreeHand Installer v0.1.0"

Write-Host ""
Write-Host ("━" * 58)
Write-Host "  FreeHand Installer v0.1.0" -ForegroundColor Cyan
Write-Host "  Local AI agent with OAuth integrations" -ForegroundColor Cyan
Write-Host ("━" * 58)
Write-Host ""

# ── Check Python 3.11+ ───────────────────────────────────
$pythonExe = $null
foreach ($v in @("python3.12", "python3.11", "python")) {
    if (Get-Command $v -ErrorAction SilentlyContinue) {
        $pyInfo = $v + " --version"
        $verOut = & $v --version 2>&1
        if ($verOut -match "(\d+)\.(\d+)") {
            $major = [int]$matches[1]
            $minor = [int]$matches[2]
            if ($major -ge 3 -and $minor -ge 11) {
                $pythonExe = $v
                Write-Host "  Python $major.$minor found ($pythonExe)" -ForegroundColor Green
                break
            }
        }
    }
}

if (-not $pythonExe) {
    Write-Host ""
    Write-Host "  Python 3.11+ not found. Installing via winget..." -ForegroundColor Yellow
    winget install Python.Python.3.12 --accept-source-agreements --accept-package-agreements
    $pythonExe = "python"
    Write-Host "  Python 3.12 installed." -ForegroundColor Green
}

# ── Check Node.js 18+ ─────────────────────────────────────
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    Write-Host ""
    Write-Host "  Node.js not found. Installing via winget..." -ForegroundColor Yellow
    winget install OpenJS.NodeJS.LTS --accept-source-agreements --accept-package-agreements
    Write-Host "  Node.js installed." -ForegroundColor Green
} else {
    $nodeVer = node -v
    Write-Host "  Node.js $nodeVer found" -ForegroundColor Green
}

# ── Create install directory ──────────────────────────────
Write-Host ""
Write-Host "  Installing to $INSTALL_DIR ..."
New-Item -ItemType Directory -Force -Path $INSTALL_DIR | Out-Null
Set-Location $INSTALL_DIR

# ── Create virtual environment ───────────────────────────
Write-Host "  Creating Python virtual environment..."
& $pythonExe -m venv venv

# ── Activate venv ─────────────────────────────────────────
$venvPython = "$INSTALL_DIR\venv\Scripts\python.exe"
$venvPip = "$INSTALL_DIR\venv\Scripts\pip.exe"

# ── Install FreeHand ──────────────────────────────────────
Write-Host "  Installing freehand-agent..."
& $venvPip install --upgrade pip
& $venvPip install "freehand-agent[all]"

# ── Install WhatsApp bridge ───────────────────────────────
Write-Host "  Installing WhatsApp bridge..."
$bridgeDir = "$INSTALL_DIR\bridge"
if (Test-Path $bridgeDir) {
    Set-Location $bridgeDir
    node install --no-audit --no-fund
    Set-Location $INSTALL_DIR
} else {
    Write-Host "  Bridge directory not found — downloading..."
    # Bridge will be included in the tarball from releases
}

# ── Initialize FreeHand ───────────────────────────────────
Write-Host ""
Write-Host "  Initializing FreeHand..."
& $venvPython "$INSTALL_DIR\venv\Scripts\freehand.exe" init

Write-Host ""
Write-Host ("━" * 58)
Write-Host "  FreeHand installed successfully!" -ForegroundColor Green
Write-Host ""
Write-Host "  Quick start:" -ForegroundColor Cyan
Write-Host "    cd $INSTALL_DIR"
Write-Host "    venv\Scripts\activate"
Write-Host "    freehand serve"
Write-Host ""
Write-Host "  Then open: http://localhost:8000" -ForegroundColor Yellow
Write-Host ""
Write-Host "  For OAuth setup, see: docs/OAUTH_SETUP.md"
Write-Host "━" * 58
Write-Host ""
