# Telegram Webhook（Copilot CLI）

这是一个轻量 FastAPI 服务：接收 Telegram 消息，并把 GitHub Copilot CLI 的结果回发到 Telegram。

## 使用前提

- Windows + PowerShell
- Python 3.11+
- 已安装 [`uv`](https://docs.astral.sh/uv/getting-started/installation/)
- 可用的 GitHub Copilot CLI（或在 `.env` 里配置 `COPILOT_COMMAND`）
- 从 [BotFather](https://t.me/BotFather) 获取的 Telegram Bot Token

## 配置步骤

1. 复制 `.env.example` 为 `.env`
2. 至少填写：
   - `BOT_TOKEN` — BotFather 给出的 Bot Token
   - `ALLOWED_USER_IDS` — 暂时留空，见下方步骤
3. 安装依赖：
   ```powershell
   uv sync
   ```

## 首次运行

> **前提：** 安装 [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/) 并确保它在 PATH 中。
> `start.ps1` 会自动完成下方第 2、3 步。

### 1. 一键启动

```powershell
.\start.ps1
```

脚本会依次自动执行：
1. 在 `http://0.0.0.0:8000` 启动 uvicorn
2. 启动 Cloudflare Tunnel 并等待公网 URL
3. 自动将该 URL 注册为 Telegram Webhook

若 `cloudflared` 未安装，服务仍会在 8000 端口启动，需手动注册 Webhook（见下方）。

### 2. 获取你的 Telegram User ID

服务启动后，向机器人发送任意消息，然后查看日志：

```powershell
Get-Content .\uvicorn.log -Tail 20
```

找到类似这样的一行：

```
[telegram] message from user_id=123456789 text='hello'
```

如果需要完整原始 payload，可临时在 `server.py` 中 `data = await request.json()` 的下一行加上 `print(data)`，重启服务后发消息，在日志里查找：

```json
"from": {
  "id": 123456789,
  ...
}
```

### 3. 更新 `.env` 并重启

```ini
ALLOWED_USER_IDS=123456789
```

```powershell
.\start.ps1
```

### 手动注册 Webhook（不使用 cloudflared 时）

```powershell
curl -X POST "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook" `
     -d "url=https://<public-domain>/webhook/<BOT_TOKEN>"
```

## Telegram 命令

| 命令 | 说明 |
|------|------|
| `/help` | 查看帮助 |
| `/new` | 开始新的 Copilot 会话 |
| `/sessions` | 查看最近会话列表 |
| `/use <id>` | 切换到历史会话 |

直接发送普通消息会自动续接当前会话。
