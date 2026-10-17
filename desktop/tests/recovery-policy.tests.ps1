$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\recovery\BridgeRecovery.ps1') -Action Library
$testRoot = Join-Path ([IO.Path]::GetTempPath()) ('lan-bridge-recovery-' + [guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($testRoot) | Out-Null
$script:RecoveryStatePath = Join-Path $testRoot 'state.json'
$script:RecoveryLogPath = Join-Path $testRoot 'recovery.log'
$script:RecoveryExe = Join-Path $testRoot 'LAN BRIDGE.exe'
[IO.File]::WriteAllText($script:RecoveryExe, 'test executable placeholder')
$script:starts = 0
$script:alive = $false
function Test-RecoveryDesktopAlive($State) { return $script:alive }
function Start-RecoveryDesktop { $script:starts++ }
function Save-State($Enabled, $Desired, $Exe = $script:RecoveryExe) {
    @{ enabled=$Enabled; desiredRunning=$Desired; desktopPid=42; desktopExecutable=$Exe } | ConvertTo-Json | Set-Content -LiteralPath $script:RecoveryStatePath -Encoding UTF8
}
function Expect($Actual, $Expected) { if ($Actual -ne $Expected) { throw "Expected '$Expected', got '$Actual'" } }
try {
    Expect (Invoke-RecoveryCheck) 'paused'
    Save-State $false $true
    Expect (Invoke-RecoveryCheck) 'paused'
    Save-State $true $false
    Expect (Invoke-RecoveryCheck) 'paused'
    Save-State 'true' $true
    Expect (Invoke-RecoveryCheck) 'paused'
    Save-State $true $true (Join-Path $testRoot 'unrelated.exe')
    Expect (Invoke-RecoveryCheck) 'paused'
    Save-State $true $true
    $script:alive = $true
    Expect (Invoke-RecoveryCheck) 'running'
    $script:alive = $false
    Expect (Invoke-RecoveryCheck) 'launched'
    Expect (Invoke-RecoveryCheck) 'backoff'
    Expect $script:starts 1
    Save-State $false $false
    Expect (Invoke-RecoveryCheck) 'paused'
    $script:NextLaunchUtc = [datetime]::MinValue
    Save-State $true $true
    # Simulate a quit arriving between the initial check and the final read.
    function Test-RecoveryDesktopAlive($State) { Save-State $false $false; return $false }
    Expect (Invoke-RecoveryCheck) 'changed'
    Expect $script:starts 1
    Write-Output 'Recovery policy: passed'
} finally {
    [IO.Directory]::Delete($testRoot, $true)
}
