# run_dev.ps1 - Start the AML Builder service in development mode
# Usage: .\run_dev.ps1
# -----------------------------------------------------------------------------

Write-Host "Starting AML Builder Agent - Port 8005" -ForegroundColor Cyan

# Set Python path so 'web.*' imports resolve from the service root
$env:PYTHONPATH = Join-Path $PSScriptRoot "services\aml_builder"

# Ensure checkpoint directory exists
$checkpointDir = Join-Path $PSScriptRoot "artifacts"
if (-not (Test-Path $checkpointDir)) {
    New-Item -ItemType Directory -Path $checkpointDir | Out-Null
    Write-Host "  Created artifacts/ directory for SQLite checkpoints." -ForegroundColor Gray
}

# Verify .env exists
$envFile = Join-Path $PSScriptRoot ".env"
if (-not (Test-Path $envFile)) {
    Write-Host "  .env file not found. Copy .env.example to .env and fill in values." -ForegroundColor Yellow
    exit 1
}

# Export environment variables from .env to the PowerShell session
Get-Content $envFile | ForEach-Object {
    $line = $_.Trim()
    if ($line -and -not $line.StartsWith("#") -and $line -like "*=*") {
        $key, $value = $line.Split("=", 2)
        $key = $key.Trim()
        $value = $value.Trim().Trim('"').Trim("'")
        [System.Environment]::SetEnvironmentVariable($key, $value)
    }
}

Write-Host "  PYTHONPATH = $env:PYTHONPATH" -ForegroundColor Gray
Write-Host "  Endpoint  = http://127.0.0.1:8005/chat/stream" -ForegroundColor Gray
Write-Host "  Docs      = http://127.0.0.1:8005/docs" -ForegroundColor Gray
Write-Host ""

uvicorn web.api.main:app --reload --port 8005 --host 0.0.0.0
