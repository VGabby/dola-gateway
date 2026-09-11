. (Join-Path $PSScriptRoot "common.ps1")

try {
    $gatewayPid = Get-GatewayPid
    if (-not $gatewayPid) {
        Write-Host "Dola Gateway is not running."
        Remove-Item $script:PidFile -Force -ErrorAction SilentlyContinue
        exit 0
    }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $gatewayPid" -ErrorAction SilentlyContinue
    if (-not $process) {
        Write-Host "Dola Gateway is not running; removed a stale PID file."
        Remove-Item $script:PidFile -Force -ErrorAction SilentlyContinue
        exit 0
    }
    if ($process.CommandLine -notmatch "uvicorn" -or $process.CommandLine -notmatch "server:app") {
        throw "PID $gatewayPid does not look like Dola Gateway; refusing to stop it. Remove $script:PidFile manually if it is stale."
    }
    Stop-Process -Id $gatewayPid
    try { Wait-Process -Id $gatewayPid -Timeout 10 -ErrorAction Stop } catch {
        Stop-Process -Id $gatewayPid -Force -ErrorAction SilentlyContinue
    }
    Remove-Item $script:PidFile -Force -ErrorAction SilentlyContinue
    Write-Host "Dola Gateway stopped." -ForegroundColor Green
    exit 0
} catch {
    Write-Error $_
    exit 1
}
