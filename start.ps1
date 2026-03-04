# start.ps1 - Start Telegram webhook service (Windows)
# Usage: .\start.ps1
#
# Automates 3 steps:
#   1. Start uvicorn (FastAPI server)
#   2. Start Cloudflare Tunnel and capture public URL  (skipped if cloudflared not found)
#   3. Register Telegram webhook with the public URL   (skipped if cloudflared not found)

Set-Location $PSScriptRoot

if (-not (Test-Path ".env")) {
    Write-Host "[warn] .env not found — please copy .env.example to .env and fill in your values." -ForegroundColor Yellow
    exit 1
}

# Load .env into current process environment
Get-Content ".env" | ForEach-Object {
    if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
        [System.Environment]::SetEnvironmentVariable($Matches[1], $Matches[2].Trim())
    }
}
$BOT_TOKEN  = [System.Environment]::GetEnvironmentVariable("BOT_TOKEN")
$PUBLIC_URL = [System.Environment]::GetEnvironmentVariable("PUBLIC_URL")

# Ensure uv command is resolvable in this shell session
$uvPath = Join-Path $env:USERPROFILE ".local\bin"
if ($env:Path -notlike "*$uvPath*") { $env:Path = "$uvPath;$env:Path" }
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "[error] uv not found in PATH. Install: https://docs.astral.sh/uv/getting-started/installation/" -ForegroundColor Red
    exit 1
}

# ── Step 1: Start uvicorn ──────────────────────────────────────────────────────
# Kill any process already holding port 8000
$portInUse = Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue | Select-Object -First 1
if ($portInUse) {
    Write-Host "[info]  Port 8000 in use (PID $($portInUse.OwningProcess)) — stopping it first ..." -ForegroundColor DarkCyan
    Stop-Process -Id $portInUse.OwningProcess -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
}

Write-Host "[1/3] Starting uvicorn on http://0.0.0.0:8000 ..." -ForegroundColor Cyan
$uvProcess = Start-Process -NoNewWindow -FilePath "uv" `
    -ArgumentList "run","python","-m","uvicorn","server:app","--host","0.0.0.0","--port","8000" `
    -PassThru
Start-Sleep -Seconds 2

# ── Step 2: Cloudflare Tunnel / PUBLIC_URL ────────────────────────────────────
if ($PUBLIC_URL) {
    Write-Host "[2/3] Using permanent PUBLIC_URL: $PUBLIC_URL" -ForegroundColor Cyan
    $publicUrl = $PUBLIC_URL.TrimEnd("/")
} else {
    # cloudflared installed via winget lands in a non-standard path; add it to PATH if needed
    $cfWinget = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Recurse -Filter "cloudflared.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cfWinget -and $env:Path -notlike "*$($cfWinget.DirectoryName)*") {
        $env:Path = "$($cfWinget.DirectoryName);$env:Path"
    }

    if (-not (Get-Command cloudflared -ErrorAction SilentlyContinue)) {
        Write-Host "[warn] cloudflared not found — skipping tunnel and webhook registration." -ForegroundColor Yellow
        Write-Host "       Install: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/" -ForegroundColor DarkYellow
        Write-Host "[ready] Server running at http://localhost:8000  (register webhook manually)" -ForegroundColor Green
        Wait-Process -Id $uvProcess.Id
        exit 0
    }

    $cfLog = Join-Path $PSScriptRoot "cf-tunnel.log"
    $cfLogOut = Join-Path $PSScriptRoot "cf-tunnel-out.log"
    Remove-Item $cfLog,$cfLogOut -ErrorAction SilentlyContinue
    Write-Host "[2/3] Starting Cloudflare Tunnel ..." -ForegroundColor Cyan
    $cfProcess = Start-Process -NoNewWindow -FilePath "cloudflared" `
        -ArgumentList "tunnel","--url","http://localhost:8000" `
        -RedirectStandardOutput $cfLogOut -RedirectStandardError $cfLog -PassThru

    # Wait up to 30 s for the tunnel URL to appear in the log (cloudflared writes URL to stderr)
    $publicUrl = $null
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 1
        foreach ($logFile in @($cfLog, $cfLogOut)) {
            if (Test-Path $logFile) {
                $content = Get-Content $logFile -Raw -ErrorAction SilentlyContinue
                if ($content -match 'https://[a-z0-9\-]+\.trycloudflare\.com') {
                    $publicUrl = $Matches[0]; break
                }
            }
        }
        if ($publicUrl) { break }
    }
    if (-not $publicUrl) {
        Write-Host "[error] Could not detect Cloudflare Tunnel URL after 30 s. Check cf-tunnel.log." -ForegroundColor Red
        exit 1
    }
    Write-Host "[info]  Tunnel URL: $publicUrl" -ForegroundColor Green
    Write-Host "[info]  Waiting 5 s for DNS propagation ..." -ForegroundColor DarkCyan
    Start-Sleep -Seconds 5
}

# ── Step 3: Register Telegram webhook ─────────────────────────────────────────
Write-Host "[3/3] Registering Telegram webhook ..." -ForegroundColor Cyan
$webhookUrl = "$publicUrl/webhook/$BOT_TOKEN"
$registered = $false
for ($attempt = 1; $attempt -le 5; $attempt++) {
    try {
        $resp = Invoke-RestMethod -Method Post `
            -Uri "https://api.telegram.org/bot$BOT_TOKEN/setWebhook" `
            -Body @{ url = $webhookUrl }
        if ($resp.ok) {
            Write-Host "[ok]   Webhook set: $webhookUrl" -ForegroundColor Green
            $registered = $true; break
        } else {
            Write-Host "[warn] Attempt $attempt — Telegram: $($resp.description)" -ForegroundColor Yellow
        }
    } catch {
        Write-Host "[warn] Attempt $attempt — $_" -ForegroundColor Yellow
    }
    if ($attempt -lt 5) { Start-Sleep -Seconds 5 }
}
if (-not $registered) {
    Write-Host "[warn] Webhook not registered automatically. Set it manually:" -ForegroundColor Yellow
    Write-Host "       curl -X POST `"https://api.telegram.org/bot$BOT_TOKEN/setWebhook`" -d `"url=$webhookUrl`"" -ForegroundColor DarkYellow
}

Write-Host ""
Write-Host "[ready] All services running. Press Ctrl+C to stop." -ForegroundColor Green
# Keep script alive; wait for uvicorn (always running), or cloudflared if using quick-tunnel
if ($PUBLIC_URL) {
    Wait-Process -Id $uvProcess.Id
} else {
    Wait-Process -Id $cfProcess.Id
}
