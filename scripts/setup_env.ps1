param(
  [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Req = Join-Path $Root "requirements.txt"

Write-Host "[setup] root: $Root"
& $Python -m pip install --upgrade pip
& $Python -m pip install -r $Req

Write-Host "[setup] done"

