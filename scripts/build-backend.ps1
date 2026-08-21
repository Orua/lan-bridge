[CmdletBinding()]
param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $projectRoot
try {
    & $Python -m pip install -U pyinstaller
    & $Python -m PyInstaller --clean --noconfirm lan-bridge.spec --distpath dist-backend --workpath build/pyinstaller
    $backend = Join-Path $projectRoot "dist-backend/lan-bridge.exe"
    if (-not (Test-Path -LiteralPath $backend)) {
        throw "Backend build did not produce $backend"
    }
    Get-FileHash -Algorithm SHA256 -LiteralPath $backend
}
finally {
    Pop-Location
}
