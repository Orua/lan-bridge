[CmdletBinding()]
param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $projectRoot
try {
    & $Python scripts/version.py --check
    if ($LASTEXITCODE -ne 0) { throw 'Version check failed.' }
    & $Python -m pip install -U pyinstaller
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller installation failed.' }
    & $Python -m PyInstaller --clean --noconfirm lan-bridge.spec --distpath dist-backend --workpath agent/temp/pyinstaller
    if ($LASTEXITCODE -ne 0) { throw 'Backend build failed.' }
    $backend = Join-Path $projectRoot "dist-backend/lan-bridge.exe"
    if (-not (Test-Path -LiteralPath $backend)) {
        throw "Backend build did not produce $backend"
    }
    $expectedVersion = (Get-Content VERSION -Raw).Trim()
    $actualVersion = & $backend --version
    if ($LASTEXITCODE -ne 0 -or "$actualVersion" -ne "lan-bridge, version $expectedVersion") {
        throw "Backend version mismatch: $actualVersion"
    }
    Get-FileHash -Algorithm SHA256 -LiteralPath $backend
}
finally {
    Pop-Location
}
