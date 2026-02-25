# Telegram Webhook（Copilot CLI）

这是一个轻量 FastAPI 服务：接收 Telegram 消息，并把 Copilot CLI 的结果回发到 Telegram。

## 使用前提

- Windows + PowerShell
- Python 3.11+
- 已安装 `uv`
- 可用的 GitHub Copilot CLI（或在 `.env` 里配置 `COPILOT_COMMAND`）
- 从 BotFather 获取的 Telegram Bot Token

## 配置步骤

1. 复制 `.env.example` 为 `.env`
2. 至少填写：
   - `BOT_TOKEN`
   - `ALLOWED_USER_ID`（或 `ALLOWED_USER_IDS`）
3. 安装依赖：
   - `uv sync`

## 启动

- 启动服务：
  - `./start.ps1`
- 将本机 `8000` 端口暴露到公网（例如 Cloudflare Tunnel）
- 设置 Telegram Webhook：
  - `https://api.telegram.org/bot<BOT_TOKEN>/setWebhook?url=https://<公网域名>/webhook/<BOT_TOKEN>`

## Telegram 命令

- `/help` 查看帮助
- `/new` 开始新会话
- `/sessions` 查看最近会话
- `/use <id>` 切换到历史会话

直接发送普通消息会自动续接当前会话。
