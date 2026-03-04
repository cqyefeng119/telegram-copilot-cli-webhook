# Telegram Webhook for Copilot CLI

---

## Overview

This project is:

* **Highly transparent** — all operations are visible
* **Highly controllable** — you decide what to authorize
* **Low complexity** — single local agent
* **Extensible** — scales without redesigning

The minimal trust foundation for AI to safely interact with your personal data. Provides a **minimal controllable architecture**:

```
Telegram (phone)
      ↓
Cloudflare Tunnel (fixed domain / HTTPS)
      ↓
localhost:8000 (Webhook)
      ↓
Single Agent (Copilot CLI, local execution)
```

Core principles:

* Agent always runs locally
* All operations are visible
* Permissions granted incrementally
* Human can take control at any time

This is not "wire AI into everything" — it establishes an **auditable trust path**.

![Telegram interaction demo](docs/images/telegram-demo.jpg)

---

## The Problem It Solves

Most AI agents are deployed in sandboxes and are not allowed to access personal data for one reason: **uncontrollable risk**.

Once connected to:

* Private calendar
* Local documents
* Chat applications
* Browser operations

Without visibility and permission boundaries, security hazards emerge.

This architecture provides:

* Local execution
* Telegram as a visible control console
* Tunnel as a secure entry point

Enabling "remote command, local execution."

---

## Real Use Cases

| Scenario | How It Works |
| -------- | ------------ |
| Restaurant booking | Agent operates browser → sends screenshot → you confirm |
| Document organization | Process local directories, nothing touches cloud |
| Draft a message | Generate draft → review → authorize send |
| Contract data entry | Agent handles formatting → you verify key fields |

Key principle:

> Grant read permission first, write permission only after verification.
> See results first, authorize execution second.

---

## Prerequisites

* Windows + PowerShell
* Python 3.11+
* `uv` installed
* Copilot CLI available (or custom command)
* Telegram Bot Token from BotFather

---

## Setup

### 1. Environment Preparation

```
Copy .env.example to .env
Fill in BOT_TOKEN
Run: uv sync
```

### 2. Choose Tunnel Mode

#### Option A: Permanent Public URL (recommended)

In `.env`:
```
PUBLIC_URL=https://your-domain.example.com
```

Then run:
```powershell
.\start.ps1
```

#### Option B: Cloudflare Quick Tunnel

Just run:
```powershell
.\start.ps1
```

Quick Tunnel URL changes on every restart. Use a fixed tunnel for production.

---

## Security Setup

### 1. User Whitelist

After starting the server, send any message to your bot, then check the log:

```powershell
Get-Content .\uvicorn.log -Tail 20
```

Find a line like:

```
[telegram] message from user_id=123456789 text='hello'
```

Add it to `.env` and restart:

```ini
ALLOWED_USER_IDS=123456789
```

```powershell
.\start.ps1
```

### 2. Permission Control Philosophy

* Default: read-only
* Key steps: manual confirmation required
* High-risk commands: reviewed one by one
* Process can be stopped at any time

This model supports "incremental permission granting."

---

## Telegram Commands

| Command | Description |
| ------- | ----------- |
| `/help` | Show help |
| `/new` | Start new Copilot session |
| `/sessions` | List recent sessions |
| `/use <id>` | Switch to session |

Normal messages automatically continue the current session.
