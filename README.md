# Telegram Webhook for Copilot CLI

A small FastAPI webhook that receives Telegram messages and replies with Copilot CLI output.

## Prerequisites

- Windows with PowerShell
- Python 3.11+
- `uv` installed
- GitHub Copilot CLI available (or set `COPILOT_COMMAND` in `.env`)
- A Telegram bot token from BotFather

## Setup

1. Copy `.env.example` to `.env`.
2. Fill at least:
   - `BOT_TOKEN`
   - `ALLOWED_USER_ID` (or `ALLOWED_USER_IDS`)
3. Install dependencies:
   - `uv sync`

## Run

- Start API server:
  - `./start.ps1`
- Expose local port 8000 (for example, Cloudflare Tunnel).
- Set Telegram webhook URL:
  - `https://api.telegram.org/bot<BOT_TOKEN>/setWebhook?url=https://<public-domain>/webhook/<BOT_TOKEN>`

## Telegram Commands

- `/help` Show command help
- `/new` Start a new session
- `/sessions` List recent sessions
- `/use <id>` Switch to an old session

Normal messages continue the current session automatically.
