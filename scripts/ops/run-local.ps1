[CmdletBinding()]
param(
    [switch]$Build
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$secretFile = Join-Path $env:LOCALAPPDATA 'dynamic-leverage-2x\secrets\runtime.env'
$emptySecretFile = Join-Path $projectRoot 'config\empty-secrets.env'

if (-not (docker info 2>$null)) {
    throw 'Docker Desktop is not running. Start it, then run this script again.'
}

$env:DVL_SECRET_FILE = if (Test-Path -LiteralPath $secretFile) {
    $secretFile
} else {
    $emptySecretFile
}
$env:SHIOAJI_MODE = if (Test-Path -LiteralPath $secretFile) {
    'readonly'
} else {
    'disabled'
}

$env:SHIOAJI_ACCOUNT_ID = ''
if (Test-Path -LiteralPath $secretFile) {
    $bindingLine = Get-Content -LiteralPath $secretFile |
        Where-Object { $_ -match '^SHIOAJI_ACCOUNT_ID=' } |
        Select-Object -Last 1
    if ($bindingLine) {
        $env:SHIOAJI_ACCOUNT_ID = $bindingLine.Substring('SHIOAJI_ACCOUNT_ID='.Length)
    }
}

$composeArgs = @('compose', 'up', '-d')
if ($Build) {
    $composeArgs += '--build'
}

Push-Location $projectRoot
try {
    & docker @composeArgs
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose failed with exit code $LASTEXITCODE"
    }
} finally {
    Pop-Location
}

Write-Host 'DVL Control is available at http://localhost:5173' -ForegroundColor Green
Write-Host 'Production order submission remains disabled.' -ForegroundColor Yellow
