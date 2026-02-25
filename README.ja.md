# Telegram Webhook（Copilot CLI）

この FastAPI サービスは Telegram メッセージを受信し、GitHub Copilot CLI の結果を Telegram に返信します。

## 前提条件

- Windows + PowerShell
- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/getting-started/installation/) のインストール
- GitHub Copilot CLI が利用可能（または `.env` で `COPILOT_COMMAND` を指定）
- [BotFather](https://t.me/BotFather) で発行した Telegram Bot Token

## セットアップ

1. `.env.example` を `.env` にコピー
2. 最低限以下を設定：
   - `BOT_TOKEN` — BotFather から取得したトークン
   - `ALLOWED_USER_IDS` — 後の手順で設定するため、今は空白のまま
3. 依存関係をインストール：
   ```powershell
   uv sync
   ```

## 初回起動手順

> **前提：** [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/) をインストールして PATH に追加してください。
> `start.ps1` が下記の手順 2・3 を自動で行います。

### 1. ワンコマンドで起動

```powershell
.\start.ps1
```

スクリプトは以下を自動実行します：
1. `http://0.0.0.0:8000` で uvicorn を起動
2. Cloudflare Tunnel を起動してパブリック URL を取得
3. その URL を Telegram Webhook に自動登録

`cloudflared` がインストールされていない場合はポート 8000 でサーバーのみ起動し、Webhook は手動で登録してください（下記参照）。

### 2. Telegram User ID を確認する

サーバー起動後にボットへメッセージを送り、ログを確認します：

```powershell
Get-Content .\uvicorn.log -Tail 20
```

以下のような行を探します：

```
[telegram] message from user_id=123456789 text='hello'
```

生の payload 全体を確認したい場合は、`server.py` の `data = await request.json()` の次の行に `print(data)` を一時的に追記し、サーバーを再起動してメッセージを送ります。ログ内で以下を探してください：

```json
"from": {
  "id": 123456789,
  ...
}
```

### 3. `.env` を更新して再起動する

```ini
ALLOWED_USER_IDS=123456789
```

```powershell
.\start.ps1
```

### 手動 Webhook 登録（cloudflared を使わない場合）

```powershell
curl -X POST "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook" `
     -d "url=https://<public-domain>/webhook/<BOT_TOKEN>"
```

## Telegram コマンド

| コマンド | 説明 |
|----------|------|
| `/help` | ヘルプを表示 |
| `/new` | 新しい Copilot セッションを開始 |
| `/sessions` | 最近のセッション一覧を表示 |
| `/use <id>` | 過去のセッションに切り替え |

通常メッセージは現在のセッションに自動で継続されます。
