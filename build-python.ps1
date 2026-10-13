param([switch]$InstallDependencies)
$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    $projectPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $projectPython)) {
        & python -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python virtual environment.' }
    }
    if ($InstallDependencies) {
        & $projectPython -m pip install -r requirements-build.txt
        if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
    }
    & $projectPython -m PyInstaller --noconfirm --clean --onefile --windowed --name WiFiAPSelector --manifest resources/app.manifest main.py
    if ($LASTEXITCODE -ne 0) { throw 'Python executable build failed. Run with -InstallDependencies to install build tools.' }
    Write-Host 'Built dist\WiFiAPSelector.exe successfully.'
} finally {
    Pop-Location
}
