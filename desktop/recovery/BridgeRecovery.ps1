[CmdletBinding()]
param(
    [ValidateSet('Install', 'Watch', 'Library')]
    [string]$Action = 'Watch',
    [string]$StatePath = (Join-Path $env:LOCALAPPDATA 'lan-bridge\recovery\state.json')
)

$ErrorActionPreference = 'Stop'
$script:RecoveryStatePath = [IO.Path]::GetFullPath($StatePath)
$script:RecoveryLogPath = Join-Path (Split-Path $script:RecoveryStatePath) 'recovery.log'
$script:RecoveryExe = Join-Path (Split-Path (Split-Path $PSScriptRoot)) 'LAN BRIDGE.exe'
$script:NextLaunchUtc = [datetime]::MinValue

function Write-RecoveryLog([string]$Message) {
    try {
        $directory = Split-Path $script:RecoveryLogPath
        [IO.Directory]::CreateDirectory($directory) | Out-Null
        if ((Test-Path -LiteralPath $script:RecoveryLogPath) -and (Get-Item -LiteralPath $script:RecoveryLogPath).Length -ge 1MB) {
            [IO.File]::Copy($script:RecoveryLogPath, "$script:RecoveryLogPath.1", $true)
            [IO.File]::WriteAllText($script:RecoveryLogPath, '')
        }
        [IO.File]::AppendAllText($script:RecoveryLogPath, ([datetime]::UtcNow.ToString('o') + ' ' + $Message + [Environment]::NewLine))
    } catch { }
}

function Read-RecoveryState {
    if (-not (Test-Path -LiteralPath $script:RecoveryStatePath)) { return $null }
    return ([IO.File]::ReadAllText($script:RecoveryStatePath) | ConvertFrom-Json)
}

function Test-RecoveryRequested($State) {
    if ($null -eq $State -or $State.enabled -isnot [bool] -or $State.desiredRunning -isnot [bool]) { return $false }
    if (-not $State.enabled -or -not $State.desiredRunning) { return $false }
    if ([IO.Path]::GetFullPath([string]$State.desktopExecutable) -ne [IO.Path]::GetFullPath($script:RecoveryExe)) { return $false }
    return (Test-Path -LiteralPath $script:RecoveryExe -PathType Leaf)
}

function Test-RecoveryDesktopAlive($State) {
    $desktop = Get-Process -Id ([int]$State.desktopPid) -ErrorAction SilentlyContinue
    return ($null -ne $desktop -and $desktop.Path -eq $script:RecoveryExe)
}

function Start-RecoveryDesktop {
    Start-Process -FilePath $script:RecoveryExe -ArgumentList '--bridge-recovery' -WorkingDirectory (Split-Path $script:RecoveryExe) -WindowStyle Hidden | Out-Null
}

function Invoke-RecoveryCheck {
    $state = Read-RecoveryState
    if (-not (Test-RecoveryRequested $state)) {
        $script:NextLaunchUtc = [datetime]::MinValue
        return 'paused'
    }
    if (Test-RecoveryDesktopAlive $state) {
        $script:NextLaunchUtc = [datetime]::MinValue
        return 'running'
    }
    if ([datetime]::UtcNow -lt $script:NextLaunchUtc) { return 'backoff' }
    # Re-read immediately before launching so a manual stop/quit wins.
    $latest = Read-RecoveryState
    if (-not (Test-RecoveryRequested $latest) -or $latest.desktopPid -ne $state.desktopPid) { return 'changed' }
    # Give Electron time to acquire its singleton and publish its new PID.
    $script:NextLaunchUtc = [datetime]::UtcNow.AddSeconds(30)
    Start-RecoveryDesktop
    Write-RecoveryLog "Recovered missing desktop pid=$($state.desktopPid) executable=$script:RecoveryExe"
    return 'launched'
}

if ($Action -eq 'Library') { return }

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$taskName = 'LAN BRIDGE Recovery-' + $identity.User.Value

if ($Action -eq 'Install') {
    # Task Scheduler starts the watcher outside Electron/Codex's process tree.
    # No logon trigger: the app's existing launch-at-login preference remains authoritative.
    $arguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $PSCommandPath + '" -Action Watch -StatePath "' + $script:RecoveryStatePath + '"'
    $powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($null -eq $existing -or $existing.Actions.Execute -ne $powershell -or $existing.Actions.Arguments -ne $arguments) {
        $taskAction = New-ScheduledTaskAction -Execute $powershell -Argument $arguments
        $principal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType Interactive -RunLevel Limited
        $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([timespan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        $task = New-ScheduledTask -Action $taskAction -Principal $principal -Settings $settings -Description 'Recover LAN BRIDGE after an unexpected desktop exit; manual stop and quit are respected.'
        Register-ScheduledTask -TaskName $taskName -InputObject $task -Force | Out-Null
    }
    Start-ScheduledTask -TaskName $taskName
    Write-RecoveryLog "Recovery task installed/started: $taskName"
    exit 0
}

# A named mutex also protects against duplicate on-demand watcher starts.
$mutexName = 'Local\LAN-BRIDGE-Recovery-' + $identity.User.Value
$mutex = New-Object Threading.Mutex($false, $mutexName)
$acquired = $false
try {
    try { $acquired = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $acquired = $true }
    if (-not $acquired) { exit 0 }
    Write-RecoveryLog "Independent watcher started pid=$PID"
    while ($true) {
        try { Invoke-RecoveryCheck | Out-Null }
        catch { Write-RecoveryLog ("Recovery check failed: " + $_.Exception.GetType().Name) }
        Start-Sleep -Seconds 5
    }
} finally {
    if ($acquired) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
