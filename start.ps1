# start.ps1 - Start Telegram webhook service (Windows)
# Usage: .\start.ps1

Set-Location $PSScriptRoot

if (-not (Test-Path ".env")) {
    Write-Host "[warn] .env not found — please copy .env.example to .env and fill in your values." -ForegroundColor Yellow
    exit 1
}

# 确保 uv 在 PATH 中
$uvPath = "$env:USERPROFILE\.local\bin"
if ($env:Path -notlike "*$uvPath*") {
    $env:Path = "$uvPath;$env:Path"
}

Write-Host "[info] Starting Telegram webhook server on http://0.0.0.0:8000" -ForegroundColor Cyan
uv run uvicorn server:app --host 0.0.0.0 --port 8000 --reload
