$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function New-Secret {
    $bytes = New-Object byte[] 48
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $rng.GetBytes($bytes)
    $value = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+','-').Replace('/','_')
    $rng.Dispose()
    return $value
}

if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    Write-Host "Python 3 is required. Install it free from https://www.python.org/downloads/windows/ and tick 'Add Python to PATH'." -ForegroundColor Yellow
    exit 1
}
$pythonVersion = (& py -3 --version 2>&1)
if ($LASTEXITCODE -ne 0) { throw "Python 3 was not found. Install Python 3.11 or newer." }

$venv = Join-Path $Root ".venv"
if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
    py -3 -m venv $venv
}
$python = Join-Path $venv "Scripts\python.exe"
& $python -m pip install --upgrade pip
& $python -m pip install -r (Join-Path $Root "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }

$secretFile = Join-Path $Root ".qingcloud-local-secrets.json"
if (Test-Path $secretFile) {
    $secrets = Get-Content $secretFile -Raw | ConvertFrom-Json
} else {
    $secrets = [PSCustomObject]@{ admin = (New-Secret); enrollment = (New-Secret) }
    $secrets | ConvertTo-Json | Set-Content -Path $secretFile -Encoding UTF8
}
$env:QCLOUD_ADMIN_TOKEN = $secrets.admin
$env:QCLOUD_ENROLLMENT_TOKEN = $secrets.enrollment
$env:QCLOUD_DB_PATH = Join-Path $Root "qingcloud.sqlite3"

Write-Host ""
Write-Host "QINGCLOUD COMPUTE — LOCAL CONTROL CENTRE" -ForegroundColor Cyan
Write-Host "Dashboard: http://127.0.0.1:8000/dashboard" -ForegroundColor Green
Write-Host "API docs:  http://127.0.0.1:8000/docs"
Write-Host ""
Write-Host "ADMIN TOKEN (save this privately):" -ForegroundColor Yellow
Write-Host $secrets.admin
Write-Host ""
Write-Host "ENROLLMENT TOKEN (share only with trusted worker owners):" -ForegroundColor Yellow
Write-Host $secrets.enrollment
Write-Host ""
Write-Host "Keep this window open. Press Ctrl+C to stop. This is local-only; remote providers need a secure HTTPS endpoint."
& $python -m uvicorn qingcloud.main:app --host 127.0.0.1 --port 8000
