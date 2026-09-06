# One-line installer for the autordp Windows binary.
#
#   irm https://raw.githubusercontent.com/bewithsnehasish/autoRDP/main/install.ps1 | iex
#
# Downloads the latest release's combined binary, installs it as autordp.exe in a
# per-user folder, and adds that folder to your PATH. `autordp` runs the CLI;
# `autordp --gui` opens the desktop app.
$ErrorActionPreference = "Stop"

$repo  = "bewithsnehasish/autoRDP"
$asset = "autordp-windows.exe"
$url   = "https://github.com/$repo/releases/latest/download/$asset"

$dest = Join-Path $env:LOCALAPPDATA "Programs\autordp"
New-Item -ItemType Directory -Force -Path $dest | Out-Null
$exe = Join-Path $dest "autordp.exe"

Write-Host "Downloading autordp from the latest release..."
Invoke-WebRequest -Uri $url -OutFile $exe

# Add the folder to the user PATH if it isn't already there.
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$dest*") {
    [Environment]::SetEnvironmentVariable("Path", "$userPath;$dest", "User")
    $env:Path = "$env:Path;$dest"
    Write-Host "Added $dest to your PATH (restart the terminal to pick it up)."
}

Write-Host ""
Write-Host "Installed: $exe"
Write-Host "Try:  autordp --help      (CLI)"
Write-Host "      autordp --gui       (desktop app)"
