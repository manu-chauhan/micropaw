# MicroPaw installer for Windows PowerShell 5.1 and PowerShell 7. No admin rights needed.
#   .\install.ps1 [-Extras telegram,discord] [-Version 0.4.0] [-NoSetup]
param(
    [string]$Extras = "",
    [string]$Version = "",
    [switch]$NoSetup
)

$ErrorActionPreference = "Stop"

# The extras go into a package spec, so only plain names are allowed.
if ($Extras -and $Extras -notmatch '^[a-z0-9,]+$') {
    throw "Invalid -Extras: use lowercase names separated by commas (telegram, discord, slack, whatsapp, browser, pdf, embeddings, all)."
}
if ($Version -and $Version -notmatch '^[A-Za-z0-9.+!-]+$') {
    throw "Invalid -Version."
}

$spec = "micropaw"
if ($Extras) { $spec = "micropaw[$Extras]" }
if ($Version) { $spec = "$spec==$Version" }

function Test-Command($name) {
    return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

if (-not (Test-Command "uv")) {
    Write-Host "Installing uv (official installer, user directory)..."
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $uvBin = Join-Path $env:USERPROFILE ".local\bin"
    if (($env:Path -split ';') -notcontains $uvBin) { $env:Path = "$uvBin;$env:Path" }
    if (-not (Test-Command "uv")) {
        throw "uv was installed but is not on PATH. Open a new terminal and run this again."
    }
}

Write-Host "Installing $spec ..."
uv tool install --compile-bytecode --upgrade $spec
if ($LASTEXITCODE -ne 0) { throw "uv tool install failed." }

$toolBin = (uv tool dir --bin 2>$null)
if (-not $toolBin) { $toolBin = Join-Path $env:USERPROFILE ".local\bin" }
if (($env:Path -split ';') -notcontains $toolBin) { $env:Path = "$toolBin;$env:Path" }

if (-not $NoSetup) {
    paw setup
} else {
    Write-Host "Done. Run 'paw setup' to configure MicroPaw."
}
Write-Host ""
Write-Host "Open a new terminal so the 'paw' command is on your PATH everywhere."
