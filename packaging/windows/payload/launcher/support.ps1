. (Join-Path $PSScriptRoot "common.ps1")

try {
    $documents = [Environment]::GetFolderPath("MyDocuments")
    $supportDir = Join-Path $documents "DolaGateway Support"
    New-Item -ItemType Directory -Force -Path $supportDir | Out-Null
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $archive = Join-Path $supportDir "DolaGateway-Support-$stamp.zip"
    $staging = Join-Path ([IO.Path]::GetTempPath()) ("DolaGatewaySupport-" + [guid]::NewGuid().ToString("N"))
    $bundle = Join-Path $staging "DolaGateway-Support-$stamp"
    New-Item -ItemType Directory -Force -Path $bundle | Out-Null
    try {
        $doctor = Join-Path $PSScriptRoot "doctor.ps1"
        $oldRedact = $env:DOLA_REDACT_PATHS
        $env:DOLA_REDACT_PATHS = "1"
        try {
            $homePath = [Environment]::GetFolderPath("UserProfile")
            $diagnostics = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $doctor 2>&1 |
                ForEach-Object { $_.ToString().Replace($homePath, "<HOME>") }
            @("Shareable Dola Gateway diagnostic report", "Created: $([DateTime]::UtcNow.ToString('s'))Z", "") + $diagnostics |
                Out-File -LiteralPath (Join-Path $bundle "diagnostics.txt") -Encoding UTF8
        } finally {
            $env:DOLA_REDACT_PATHS = $oldRedact
        }

        $inventory = @("Log inventory only; log contents are intentionally excluded.")
        if (Test-Path $script:LogDir) {
            foreach ($log in Get-ChildItem -LiteralPath $script:LogDir -Filter "*.log" -File) {
                $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $log.FullName).Hash.ToLowerInvariant()
                $inventory += "$($log.Name)  bytes=$($log.Length)  sha256=$hash"
            }
        }
        $inventory | Out-File -LiteralPath (Join-Path $bundle "log-inventory.txt") -Encoding UTF8

        if (Test-Path $script:EnvFile) {
            Get-Content -LiteralPath $script:EnvFile |
                Where-Object { $_ -match "^\s*DOLA_[A-Z0-9_]+\s*=" } |
                ForEach-Object { (($_ -split "=", 2)[0]).Trim() + "=<redacted>" } |
                Out-File -LiteralPath (Join-Path $bundle "configured-settings.txt") -Encoding UTF8
        }
        Compress-Archive -LiteralPath $bundle -DestinationPath $archive -CompressionLevel Optimal
    } finally {
        Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
    }
    Write-Host "Shareable support bundle created: $archive" -ForegroundColor Green
    Write-Host "It excludes keys, profiles, databases, generated media, and log contents."
    exit 0
} catch {
    Write-Error $_
    exit 1
}
