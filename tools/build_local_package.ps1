[CmdletBinding()]
param(
  [Parameter()]
  [string]$Output = "$PSScriptRoot\..\_dist\app.zip",

  [Parameter()]
  [ValidateSet('3.11','3.10','3.9')]
  [string]$PythonVersion = '3.11'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path "$PSScriptRoot\..").Path
$distDir = Join-Path $repoRoot '_dist'
$staging = Join-Path $distDir 'staging'

if (Test-Path $distDir) { Remove-Item $distDir -Recurse -Force }
New-Item -ItemType Directory -Path $distDir | Out-Null
New-Item -ItemType Directory -Path $staging | Out-Null

$python = (Get-Command python -ErrorAction SilentlyContinue)
if (-not $python) {
  throw "Python not found on PATH. Install Python $PythonVersion and retry."
}

Write-Host "Staging app into: $staging"
Copy-Item -Path (Join-Path $repoRoot 'app.py') -Destination $staging -Force
Copy-Item -Path (Join-Path $repoRoot 'requirements.txt') -Destination $staging -Force

# Install dependencies into .python_packages so App Service can run without remote build.
$packagesDir = Join-Path $staging '.python_packages\lib\site-packages'
New-Item -ItemType Directory -Path $packagesDir -Force | Out-Null

Write-Host "Installing dependencies into .python_packages (this runs locally)..."
& $python.Source -m pip install -r (Join-Path $staging 'requirements.txt') -t $packagesDir | Write-Host

# Zip it up
$outputPath = $Output
if (-not [System.IO.Path]::IsPathRooted($outputPath)) {
  $outputPath = Join-Path $repoRoot $outputPath
}

$outputDir = Split-Path $outputPath -Parent
if ([string]::IsNullOrWhiteSpace($outputDir)) {
  $outputDir = $distDir
}
New-Item -ItemType Directory -Path $outputDir -Force | Out-Null

$Output = Join-Path $outputDir (Split-Path $outputPath -Leaf)

Write-Host "Creating zip package: $Output"
Compress-Archive -Path (Join-Path $staging '*') -DestinationPath $Output -Force

Write-Host "Done. Deploy this zip with ZipDeploy (run-from-package recommended)."