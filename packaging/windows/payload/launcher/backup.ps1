param([switch]$IncludeProfiles)

. (Join-Path $PSScriptRoot "common.ps1")

try {
    if (Test-GatewayRunning) {
        throw "Stop the gateway with stop.cmd before creating a backup."
    }
    if (-not (Test-Path $script:StateDir)) {
        throw "No Dola Gateway data directory exists yet."
    }

    $documents = [Environment]::GetFolderPath("MyDocuments")
    $backupDir = Join-Path $documents "DolaGateway Backups"
    New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $archive = Join-Path $backupDir "DolaGateway-$stamp.zip"
    $staging = Join-Path ([IO.Path]::GetTempPath()) ("DolaGatewayBackup-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force -Path $staging | Out-Null
    try {
        foreach ($name in @(".env.local", "accounts.local.json", "tasks.db", "pool_usage.db", "downloads")) {
            $source = Join-Path $script:StateDir $name
            if (Test-Path $source) { Copy-Item -LiteralPath $source -Destination $staging -Recurse -Force }
        }
        if ($IncludeProfiles) {
            $profiles = Join-Path $script:StateDir "accounts"
            if (Test-Path $profiles) { Copy-Item -LiteralPath $profiles -Destination $staging -Recurse -Force }
        }
        Compress-Archive -Path (Join-Path $staging "*") -DestinationPath $archive -CompressionLevel Optimal
    } finally {
        Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
    }

    Write-Host "Backup created: $archive" -ForegroundColor Green
    Write-Host "This ZIP contains sensitive configuration, history, and media. Store it securely and never send it to anyone." -ForegroundColor Yellow
    if ($IncludeProfiles) {
        Write-Host "It also contains reusable browser login sessions." -ForegroundColor Yellow
    } else {
        Write-Host "Browser login profiles were not included. Re-login is required after a full restore."
    }
    exit 0
} catch {
    Write-Error $_
    exit 1
}
