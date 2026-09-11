. (Join-Path $PSScriptRoot "common.ps1")

$failed = $false
Write-Host "Dola Gateway diagnostics" -ForegroundColor Cyan
if (Test-Path $script:VersionFile) { Write-Host "Version: $((Get-Content -LiteralPath $script:VersionFile -Raw).Trim())" }
Write-Host "Package: $(Format-SafePath $script:PackageRoot)"
Write-Host "Data:    $(Format-SafePath $script:StateDir)"

if (Test-Path $script:VenvPython) {
    $version = (& $script:VenvPython --version 2>&1) -join " "
    Write-Host "[OK] $version"
    Set-GatewayEnvironment
    Push-Location $script:AppDir
    try {
        & $script:VenvPython -c "import fastapi, aiohttp, pydantic, patchright, PIL, cv2; import config; print('[OK] Application dependencies and configuration load')"
        if ($LASTEXITCODE -ne 0) { $failed = $true }
        & $script:VenvPython (Join-Path $PSScriptRoot "browser_check.py")
        if ($LASTEXITCODE -ne 0) { $failed = $true }
    } finally { Pop-Location }
} else {
    Write-Host "[FAIL] Private Python environment is missing. Run setup.cmd." -ForegroundColor Red
    $failed = $true
}

if (Test-Path $script:EnvFile) {
    Write-Host "[OK] Private configuration exists"
} else {
    Write-Host "[FAIL] Private configuration is missing. Run setup.cmd." -ForegroundColor Red
    $failed = $true
}

if (Test-GatewayRunning) {
    $port = Get-EnvValue "DOLA_PORT" "8000"
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$port/health" -TimeoutSec 3
        if ($response.StatusCode -eq 200) { Write-Host "[OK] Gateway health endpoint is responding" }
    } catch {
        Write-Host "[FAIL] Gateway process exists but health check failed" -ForegroundColor Red
        $failed = $true
    }
} else {
    Write-Host "[INFO] Gateway is not running"
}

if ($failed) { exit 1 }
Write-Host "Diagnostics passed." -ForegroundColor Green
exit 0
