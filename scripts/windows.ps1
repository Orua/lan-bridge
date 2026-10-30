[CmdletBinding()]
param(
    [ValidateSet('Build', 'Start', 'Dev')]
    [string]$Action = 'Start',
    [string]$Python = 'python'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $root '.venv/Scripts/python.exe'
$version = (Get-Content (Join-Path $root 'VERSION') -Raw).Trim()
$release = Join-Path $root "agent/output/windows/$version"

function Invoke-Checked {
    param([string]$Command, [string[]]$Arguments)
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed ($LASTEXITCODE): $Command $($Arguments -join ' ')" }
}

function Get-ReleaseCommit {
    $commit = & git -C $root rev-parse HEAD
    if ($LASTEXITCODE -ne 0) { throw 'Release builds require a Git checkout.' }
    $dirty = & git -C $root status --porcelain --untracked-files=all -- . ':!agent/**'
    if ($LASTEXITCODE -ne 0) { throw 'Cannot check release source status.' }
    if ($dirty) { throw "Commit all product source changes before packaging (agent records excluded):`n$($dirty -join "`n")" }
    return "$commit".Trim()
}

Push-Location $root
try {
    if ($env:OS -ne 'Windows_NT') { throw 'The supported desktop build requires Windows 10/11 x64.' }
    if ($Action -eq 'Start') {
        $exe = Join-Path $release 'win-unpacked/LAN BRIDGE.exe'
        if (-not (Test-Path -LiteralPath $exe)) {
            throw 'Desktop is not built. Run: powershell -ExecutionPolicy Bypass -File scripts/windows.ps1 -Action Build'
        }
        Invoke-Checked 'node' @('desktop/scripts/verify-release.cjs', $release)
        Start-Process -FilePath $exe -WorkingDirectory (Split-Path $exe)
        return
    }

    if ($Action -eq 'Build') { $sourceCommit = Get-ReleaseCommit }
    $nodeVersion = & node -p 'process.versions.node'
    if ($LASTEXITCODE -ne 0 -or [version]$nodeVersion -lt [version]'22.12.0') {
        throw 'Install Node.js 22.12+ (including npm), then open a new PowerShell window.'
    }
    if (-not (Test-Path -LiteralPath $venvPython)) {
        Invoke-Checked $Python @('-m', 'venv', '.venv')
    }
    Invoke-Checked $venvPython @('-c', 'import sys; assert sys.version_info >= (3, 10), "Python 3.10+ required"')
    Invoke-Checked $venvPython @('scripts/version.py', '--check')
    Invoke-Checked $venvPython @('-m', 'pip', 'install', '-e', '.', 'pytest', 'pyinstaller')
    Push-Location (Join-Path $root 'desktop')
    try { Invoke-Checked 'npm.cmd' @('ci') } finally { Pop-Location }

    if ($Action -eq 'Dev') {
        # Electron's Python child must use this repository's environment.
        $env:PATH = "$(Split-Path $venvPython);$env:PATH"
        Push-Location (Join-Path $root 'desktop')
        try { Invoke-Checked 'npm.cmd' @('run', 'electron:dev') } finally { Pop-Location }
        return
    }

    Invoke-Checked $venvPython @('-m', 'pytest', '-q', '-o', 'cache_dir=agent/temp/pytest-cache')
    Push-Location (Join-Path $root 'desktop')
    try {
        Invoke-Checked 'npm.cmd' @('run', 'typecheck')
        Invoke-Checked 'npm.cmd' @('run', 'test:electron')
    } finally { Pop-Location }
    & (Join-Path $PSScriptRoot 'build-backend.ps1') -Python $venvPython
    $env:PATH = "$(Split-Path $venvPython);$env:PATH"
    Push-Location (Join-Path $root 'desktop')
    try { Invoke-Checked 'npm.cmd' @('run', 'electron:build', '--', '--win', '--x64', '--publish', 'never', "--config.directories.output=$release") } finally { Pop-Location }
    if ((Get-ReleaseCommit) -ne $sourceCommit) { throw 'Source commit changed during the build. Do not publish these artifacts.' }
    $manifest = @{ version = $version; sourceCommit = $sourceCommit; sourceClean = $true; platform = 'win32-x64'; builtAt = [DateTime]::UtcNow.ToString('o') } | ConvertTo-Json
    [System.IO.File]::WriteAllText((Join-Path $release 'RELEASE.json'), $manifest, [System.Text.UTF8Encoding]::new($false))
    Invoke-Checked 'node' @('desktop/scripts/verify-release.cjs', $release, '--write-checksums')
    Write-Host "Build verified. Installer and portable desktop: $release"
    Write-Host 'Start desktop: powershell -ExecutionPolicy Bypass -File scripts/windows.ps1 -Action Start'
} finally {
    Pop-Location
}
