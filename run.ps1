$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    $projectPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $projectPython)) { $projectPython = 'python' }
    & $projectPython main.py @args
    if ($LASTEXITCODE -ne 0) { throw "Wi-Fi AP Selector exited with code $LASTEXITCODE." }
} finally {
    Pop-Location
}
