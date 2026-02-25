# Telegram Webhook for Copilot CLI

A small FastAPI webhook that receives Telegram messages and replies with GitHub Copilot CLI output.

## Prerequisites

- Windows with PowerShell
- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/getting-started/installation/) installed
- GitHub Copilot CLI available (or set `COPILOT_COMMAND` in `.env`)
- A Telegram bot token from [BotFather](https://t.me/BotFather)

## Setup

1. Copy `.env.example` to `.env`.
2. Fill in at least:
   - `BOT_TOKEN` — your bot token from BotFather
   - `ALLOWED_USER_IDS` — leave blank for now; see step below
3. Install dependencies:
   ```powershell
   uv sync
   ```

## First Run

> **Prerequisite:** install [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/) and make sure it is in your PATH.
> `start.ps1` automatically handles steps 2 and 3 below.

### 1. Start everything with one command

```powershell
.\start.ps1
```

The script will:
1. Start the uvicorn server on `http://0.0.0.0:8000`
2. Start a Cloudflare Tunnel and wait for the public URL
3. Automatically register that URL as your Telegram webhook

If `cloudflared` is not installed, the server starts on port 8000 and you will need to register the webhook manually (see below).

### 2. Find your Telegram User ID

After the server is running, send any message to your bot in Telegram, then check the log:

```powershell
Get-Content .\uvicorn.log -Tail 20
```

Look for a line like:

```
[telegram] message from user_id=123456789 text='hello'
```

### 3. Update `.env` and restart

```ini
ALLOWED_USER_IDS=123456789
```

```powershell
.\start.ps1
```

### Manual webhook registration (if cloudflared is not used)

```powershell
curl -X POST "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook" `
     -d "url=https://<public-domain>/webhook/<BOT_TOKEN>"
```

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/help` | Show command help |
| `/new` | Start a new Copilot session |
| `/sessions` | List recent sessions |
| `/use <id>` | Switch to an existing session |

Any other message automatically continues the current session.
