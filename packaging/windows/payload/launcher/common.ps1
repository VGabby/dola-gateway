$ErrorActionPreference = "Stop"

$script:PackageRoot = Split-Path -Parent $PSScriptRoot
$script:AppDir = Join-Path $script:PackageRoot "app"
if (-not $env:LOCALAPPDATA) {
    throw "LOCALAPPDATA is not available. Run this package as a normal Windows user."
}
$script:StateDir = Join-Path $env:LOCALAPPDATA "DolaGateway"
$script:RuntimeDir = Join-Path $script:StateDir "runtime"
$script:VenvDir = Join-Path $script:RuntimeDir "venv"
$script:VenvPython = Join-Path $script:VenvDir "Scripts\python.exe"
$script:BrowserDir = Join-Path $script:RuntimeDir "browsers"
$script:EnvFile = Join-Path $script:StateDir ".env.local"
$script:PidFile = Join-Path $script:StateDir "gateway.pid"
$script:LogDir = Join-Path $script:StateDir "logs"
$script:VersionFile = Join-Path $script:PackageRoot "VERSION"

function Format-SafePath([string]$Path) {
    if ($env:DOLA_REDACT_PATHS -eq "1") {
        $homePath = [Environment]::GetFolderPath("UserProfile")
        if ($homePath -and $Path.StartsWith($homePath, [StringComparison]::OrdinalIgnoreCase)) {
            return "<HOME>" + $Path.Substring($homePath.Length)
        }
    }
    return $Path
}

function Set-GatewayEnvironment {
    $env:DOLA_STATE_DIR = $script:StateDir
    $env:DOLA_ENV_FILE = $script:EnvFile
    $env:PLAYWRIGHT_BROWSERS_PATH = $script:BrowserDir
    $env:PYTHONUTF8 = "1"
}

function Get-EnvValue([string]$Name, [string]$Default = "") {
    if (-not (Test-Path $script:EnvFile)) {
        return $Default
    }
    foreach ($line in Get-Content -LiteralPath $script:EnvFile) {
        if ($line -match "^\s*$([regex]::Escape($Name))\s*=(.*)$") {
            return $Matches[1].Trim().Trim('"').Trim("'")
        }
    }
    return $Default
}

function Get-GatewayPid {
    if (-not (Test-Path $script:PidFile)) {
        return $null
    }
    $value = (Get-Content -LiteralPath $script:PidFile -Raw).Trim()
    if ($value -notmatch "^\d+$") {
        return $null
    }
    return [int]$value
}

function Test-GatewayRunning {
    $gatewayPid = Get-GatewayPid
    if (-not $gatewayPid) {
        return $false
    }
    return $null -ne (Get-Process -Id $gatewayPid -ErrorAction SilentlyContinue)
}

function New-RandomSecret([int]$ByteCount = 24) {
    $bytes = New-Object byte[] $ByteCount
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($bytes)
    } finally {
        $generator.Dispose()
    }
    return -join ($bytes | ForEach-Object { $_.ToString("x2") })
}
