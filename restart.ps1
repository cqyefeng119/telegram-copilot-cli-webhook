# restart.ps1 - Stable restart for Telegram webhook service (Windows)
# Usage: .\restart.ps1
#
# Improvements over start.ps1:
#   - Kills ALL existing uvicorn / cloudflared processes before starting
#   - Redirects uvicorn stdout/stderr to log files
#   - Watchdog loop: auto-restarts either process if it crashes
#   - Re-registers Telegram webhook automatically after tunnel restart

Set-Location $PSScriptRoot

# Single-instance guard: kill any previous restart.ps1 watchdog
$pidFile = Join-Path $PSScriptRoot "restart.pid"
if (Test-Path $pidFile) {
    $oldPid = [int](Get-Content $pidFile -ErrorAction SilentlyContinue)
    if ($oldPid -and (Get-Process -Id $oldPid -ErrorAction SilentlyContinue)) {
        Write-Host "[init]  Stopping previous watchdog (PID $oldPid)..." -ForegroundColor DarkCyan
        Stop-Process -Id $oldPid -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 1
    }
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
}
$PID | Out-File -FilePath $pidFile -Encoding ascii

if (-not (Test-Path ".env")) {
    Write-Host "[error] .env not found — copy .env.example to .env and fill in your values." -ForegroundColor Red
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

# Ensure uv is resolvable
$uvPath = Join-Path $env:USERPROFILE ".local\bin"
if ($env:Path -notlike "*$uvPath*") { $env:Path = "$uvPath;$env:Path" }
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "[error] uv not found. Install: https://docs.astral.sh/uv/getting-started/installation/" -ForegroundColor Red
    exit 1
}

# Ensure cloudflared is resolvable
$cfWinget = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Recurse -Filter "cloudflared.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
if ($cfWinget -and $env:Path -notlike "*$($cfWinget.DirectoryName)*") {
    $env:Path = "$($cfWinget.DirectoryName);$env:Path"
}
if (-not (Get-Command cloudflared -ErrorAction SilentlyContinue)) {
    Write-Host "[error] cloudflared not found." -ForegroundColor Red
    Write-Host "        Install: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/" -ForegroundColor DarkYellow
    exit 1
}

# ── Helper functions ───────────────────────────────────────────────────────────

function Stop-Port8000 {
    # Check port 8000 via .NET (avoids Get-NetTCPConnection / netstat / CIM hangs)
    $isListening = ([System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties()).GetActiveTcpListeners() |
        Where-Object { $_.Port -eq 8000 } | Select-Object -First 1
    if (-not $isListening) { return }

    $ids = @(Get-Process -Name "python*" -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id)
    foreach ($id in $ids) {
        Write-Host "[info]  Stopping Python process (PID $id) ..." -ForegroundColor DarkCyan
        Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
    }
    if ($ids.Count -gt 0) { Start-Sleep -Seconds 2 }
}

function Stop-AllCloudflared {
    Get-Process -Name "cloudflared" -ErrorAction SilentlyContinue | ForEach-Object {
        Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Milliseconds 500
}

function Start-Uvicorn {
    Stop-Port8000
    # Also kill any lingering uv/python launcher processes that may hold log-file handles
    $stale = @(Get-Process -Name "uv","python*" -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id)
    foreach ($id in $stale) { Stop-Process -Id $id -Force -ErrorAction SilentlyContinue }
    if ($stale.Count -gt 0) { Start-Sleep -Milliseconds 500 }

    $logOut = Join-Path $PSScriptRoot "uvicorn.log"
    $logErr = Join-Path $PSScriptRoot "uvicorn-err.log"
    Remove-Item $logOut, $logErr -Force -ErrorAction SilentlyContinue
    $proc = Start-Process -NoNewWindow -FilePath "uv" `
        -ArgumentList "run","python","-m","uvicorn","server:app","--host","0.0.0.0","--port","8000" `
        -RedirectStandardOutput $logOut -RedirectStandardError $logErr -PassThru
    return $proc
}

function Start-CfTunnel {
    Stop-AllCloudflared
    $cfLog    = Join-Path $PSScriptRoot "cf-tunnel.log"
    $cfLogOut = Join-Path $PSScriptRoot "cf-tunnel-out.log"
    Remove-Item $cfLog,$cfLogOut -ErrorAction SilentlyContinue
    $proc = Start-Process -NoNewWindow -FilePath "cloudflared" `
        -ArgumentList "tunnel","--url","http://localhost:8000" `
        -RedirectStandardOutput $cfLogOut -RedirectStandardError $cfLog -PassThru
    return $proc
}

function Get-TunnelUrl {
    param([int]$TimeoutSeconds = 30)
    $cfLog    = Join-Path $PSScriptRoot "cf-tunnel.log"
    $cfLogOut = Join-Path $PSScriptRoot "cf-tunnel-out.log"
    for ($i = 0; $i -lt $TimeoutSeconds; $i++) {
        Start-Sleep -Seconds 1
        foreach ($logFile in @($cfLog, $cfLogOut)) {
            if (Test-Path $logFile) {
                $content = Get-Content $logFile -Raw -ErrorAction SilentlyContinue
                if ($content -match 'https://[a-z0-9\-]+\.trycloudflare\.com') {
                    return $Matches[0]
                }
            }
        }
    }
    return $null
}

function Register-Webhook {
    param([string]$PublicUrl, [int]$MaxAttempts = 5)
    $webhookUrl = "$PublicUrl/webhook/$BOT_TOKEN"
    for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
        try {
            $resp = Invoke-RestMethod -Method Post `
                -Uri "https://api.telegram.org/bot$BOT_TOKEN/setWebhook" `
                -Body @{ url = $webhookUrl }
            if ($resp.ok) {
                Write-Host "[ok]   Webhook set: $webhookUrl" -ForegroundColor Green
                return $true
            }
            Write-Host "[warn] Attempt $attempt — $($resp.description)" -ForegroundColor Yellow
        } catch {
            Write-Host "[warn] Attempt $attempt — $_" -ForegroundColor Yellow
        }
        if ($attempt -lt $MaxAttempts) { Start-Sleep -Seconds 5 }
    }
    Write-Host "[warn] Webhook not registered. Set manually:" -ForegroundColor Yellow
    Write-Host "       https://api.telegram.org/bot$BOT_TOKEN/setWebhook?url=$webhookUrl" -ForegroundColor DarkYellow
    return $false
}

# ── Initial startup ────────────────────────────────────────────────────────────

# Kill any leftover Python/uvicorn processes from previous runs
$staleIds = @(Get-Process -Name "python*","uv" -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id)
if ($staleIds.Count -gt 0) {
    Write-Host "[init]  Cleaning up $($staleIds.Count) stale Python/uv process(es) ..." -ForegroundColor DarkCyan
    foreach ($id in $staleIds) { Stop-Process -Id $id -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
}

Write-Host "[1/3] Starting uvicorn on http://0.0.0.0:8000 ..." -ForegroundColor Cyan
$uvProc = Start-Uvicorn
Start-Sleep -Seconds 2

# ── Step 2: Tunnel / PUBLIC_URL ───────────────────────────────────────────────
$cfProc = $null
if ($PUBLIC_URL) {
    Write-Host "[2/3] Using permanent PUBLIC_URL: $PUBLIC_URL" -ForegroundColor Cyan
    $publicUrl = $PUBLIC_URL.TrimEnd("/")
} else {
    Write-Host "[2/3] Starting Cloudflare Tunnel ..." -ForegroundColor Cyan
    $cfProc = Start-CfTunnel
    $publicUrl = Get-TunnelUrl -TimeoutSeconds 30
    if (-not $publicUrl) {
        Write-Host "[error] Could not detect Cloudflare Tunnel URL after 30 s. Check cf-tunnel.log." -ForegroundColor Red
        exit 1
    }
    Write-Host "[info]  Tunnel URL: $publicUrl" -ForegroundColor Green
    Write-Host "[info]  Waiting 5 s for DNS propagation ..." -ForegroundColor DarkCyan
    Start-Sleep -Seconds 5
}

Write-Host "[3/3] Registering Telegram webhook ..." -ForegroundColor Cyan
Register-Webhook -PublicUrl $publicUrl | Out-Null

Write-Host ""
Write-Host "[ready] All services running. Watchdog active. Press Ctrl+C to stop." -ForegroundColor Green

# ── Watchdog loop ──────────────────────────────────────────────────────────────
try {
while ($true) {
    Start-Sleep -Seconds 10

    # Restart uvicorn if it crashed: check by port rather than process handle,
    # because the $uvProc launcher may exit before the child Python process does.
    $uvListening = ([System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties()).GetActiveTcpListeners() |
        Where-Object { $_.Port -eq 8000 } | Select-Object -First 1
    if (-not $uvListening) {
        Write-Host "[watchdog $(Get-Date -Format 'HH:mm:ss')] uvicorn not listening on port 8000 — restarting ..." -ForegroundColor Yellow
        $uvProc = Start-Uvicorn
        Start-Sleep -Seconds 20  # wait for uvicorn to fully start before next check
    }

    # Restart cloudflared if it crashed (only in quick-tunnel mode)
    if ($cfProc -and $cfProc.HasExited) {
        Write-Host "[watchdog $(Get-Date -Format 'HH:mm:ss')] cloudflared crashed (exit $($cfProc.ExitCode)) — restarting tunnel ..." -ForegroundColor Yellow
        $cfProc = Start-CfTunnel
        $newUrl = Get-TunnelUrl -TimeoutSeconds 30
        if ($newUrl) {
            Write-Host "[watchdog] New tunnel URL: $newUrl" -ForegroundColor Green
            Start-Sleep -Seconds 5
            Register-Webhook -PublicUrl $newUrl | Out-Null
            $publicUrl = $newUrl
        } else {
            Write-Host "[watchdog] Could not get new tunnel URL — will retry next cycle." -ForegroundColor Red
        }
    }
}
} finally {
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
}
