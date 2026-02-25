# Telegram Webhook（Copilot CLI）

この FastAPI サービスは Telegram メッセージを受信し、Copilot CLI の結果を Telegram に返信します。

## 前提条件

- Windows + PowerShell
- Python 3.11+
- `uv` のインストール
- GitHub Copilot CLI が利用可能（または `.env` で `COPILOT_COMMAND` を指定）
- BotFather で発行した Telegram Bot Token

## セットアップ

1. `.env.example` を `.env` にコピー
2. 最低限以下を設定
   - `BOT_TOKEN`
   - `ALLOWED_USER_ID`（または `ALLOWED_USER_IDS`）
3. 依存関係をインストール
   - `uv sync`

## 起動

- サーバー起動：
  - `./start.ps1`
- ローカル 8000 ポートを公開（例: Cloudflare Tunnel）
- Telegram Webhook を設定：
  - `https://api.telegram.org/bot<BOT_TOKEN>/setWebhook?url=https://<public-domain>/webhook/<BOT_TOKEN>`

## Telegram コマンド

- `/help` ヘルプ表示
- `/new` 新しいセッション開始
- `/sessions` 最近のセッション一覧
- `/use <id>` 過去セッションへ切り替え

通常メッセージは現在のセッションに自動で継続されます。
