$ErrorActionPreference = "Stop"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  Write-Host "Installing the uv Python runtime manager..."
  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
  $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}

$package = "toda-ai"
if ($env:TODA_VERSION) { $package = "toda-ai==$env:TODA_VERSION" }
Write-Host "Installing $package from PyPI..."
uv tool install --python 3.12 --force $package

$toda = Get-Command toda -ErrorAction SilentlyContinue
if (-not $toda) {
  $candidate = Join-Path $env:USERPROFILE ".local\bin\toda.exe"
  if (Test-Path $candidate) { $toda = $candidate } else { throw "Toda installed, but toda.exe is not on PATH." }
}

Write-Host "Creating Toda's per-user service..."
& $toda install
Write-Host "`nToda installed successfully."
Write-Host "Status: toda status"
Write-Host "Local console: http://127.0.0.1:8080"
