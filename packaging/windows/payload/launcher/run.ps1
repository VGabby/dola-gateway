. (Join-Path $PSScriptRoot "common.ps1")

try {
    if (-not (Test-Path $script:VenvPython) -or -not (Test-Path $script:EnvFile)) {
        throw "Setup is incomplete. Run setup.cmd first."
    }
    if (Test-GatewayRunning) {
        $existingPort = Get-EnvValue "DOLA_PORT" "8000"
        $existingUrl = "http://127.0.0.1:$existingPort/"
        Write-Host "Dola Gateway is already running at $existingUrl" -ForegroundColor Green
        Start-Process $existingUrl
        exit 0
    }

    $adminKey = Get-EnvValue "DOLA_ADMIN_KEY"
    $clientKey = Get-EnvValue "DOLA_API_KEYS"
    if (-not $adminKey -or -not $clientKey -or $adminKey -like "replace-*" -or $clientKey -like "replace-*") {
        throw "Private API/admin keys are missing. Run setup.cmd or repair $script:EnvFile."
    }
    $portText = Get-EnvValue "DOLA_PORT" "8000"
    $port = 0
    if (-not [int]::TryParse($portText, [ref]$port) -or $port -lt 1 -or $port -gt 65535) {
        throw "Invalid DOLA_PORT in $script:EnvFile"
    }

    New-Item -ItemType Directory -Force -Path $script:LogDir | Out-Null
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $outLog = Join-Path $script:LogDir "server.out.log"
    $errLog = Join-Path $script:LogDir "server.err.log"
    if (Test-Path $outLog) { Move-Item $outLog (Join-Path $script:LogDir "server-$stamp.out.log") -Force }
    if (Test-Path $errLog) { Move-Item $errLog (Join-Path $script:LogDir "server-$stamp.err.log") -Force }

    Set-GatewayEnvironment
    $arguments = @("-m", "uvicorn", "server:app", "--host", "127.0.0.1", "--port", $port.ToString())
    $process = Start-Process -FilePath $script:VenvPython -ArgumentList $arguments -WorkingDirectory $script:AppDir -RedirectStandardOutput $outLog -RedirectStandardError $errLog -WindowStyle Hidden -PassThru
    $process.Id | Set-Content -LiteralPath $script:PidFile -Encoding ASCII

    $healthUrl = "http://127.0.0.1:$port/health"
    $ready = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Seconds 1
        if ($process.HasExited) { break }
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $healthUrl -TimeoutSec 2
            if ($response.StatusCode -eq 200) { $ready = $true; break }
        } catch {}
    }
    if (-not $ready) {
        if (-not $process.HasExited) { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue }
        Remove-Item $script:PidFile -Force -ErrorAction SilentlyContinue
        $tail = if (Test-Path $errLog) { (Get-Content $errLog -Tail 20) -join [Environment]::NewLine } else { "No error log was written." }
        throw "The gateway did not become healthy. Error log:`n$tail"
    }

    $url = "http://127.0.0.1:$port/"
    Write-Host "Dola Gateway is running at $url" -ForegroundColor Green
    Write-Host "Use stop.cmd before shutting down, backing up, or updating."
    Start-Process $url
    exit 0
} catch {
    Write-Error $_
    exit 1
}
