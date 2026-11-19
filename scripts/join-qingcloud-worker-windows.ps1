$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw "Install Python 3.11+ first: https://www.python.org/downloads/windows/"
}
$venv = Join-Path $Root ".venv"
if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
    py -3 -m venv $venv
}
$python = Join-Path $venv "Scripts\python.exe"
& $python -m pip install -r (Join-Path $Root "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }

$url = Read-Host "QingCloud HTTPS address (example: https://your-host.example)"
if (-not $url.StartsWith("https://")) {
    throw "Remote workers must use HTTPS. Do not send enrollment tokens over public HTTP."
}
$token = Read-Host "One-time provider invitation token (recommended) or enrollment token"
$mode = Read-Host "Is this a one-time invitation token? Enter Y or N"
if ($mode -match '^[Yy]$') { $env:QCLOUD_INVITATION_TOKEN = $token }
else { $env:QCLOUD_ENROLLMENT_TOKEN = $token }
$env:QCLOUD_URL = $url

Write-Host "Enrolling this computer and checking its hardware..." -ForegroundColor Cyan
& $python -m worker_agent.agent --once
if ($LASTEXITCODE -ne 0) { throw "Enrollment/first heartbeat failed. Check the URL and token." }
Remove-Item Env:\QCLOUD_INVITATION_TOKEN -ErrorAction SilentlyContinue
Remove-Item Env:\QCLOUD_ENROLLMENT_TOKEN -ErrorAction SilentlyContinue

$runJobs = Read-Host "Allow this worker to execute QingCloud's bounded built-in jobs? Enter Y to opt in"
if ($runJobs -match '^[Yy]$') {
    Write-Host "Worker online. Keep this window open. Ctrl+C stops it." -ForegroundColor Green
    & $python -m worker_agent.agent --enable-jobs --interval 30 --max-jobs-per-cycle 2
} else {
    Write-Host "Worker will report hardware and heartbeat only. Ctrl+C stops it." -ForegroundColor Green
    & $python -m worker_agent.agent --interval 30
}
