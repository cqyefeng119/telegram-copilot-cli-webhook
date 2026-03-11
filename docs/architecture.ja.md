# アーキテクチャメモ

> このドキュメントは、将来のメンテナンター（AI を含む）向けで、Webhook システムの内部構造、設計決定、拡張ポイントを記録しています。  
> 最終更新：2026-03-11

---

## 目次

1. [5 層システム概要](#1-5-層システム概要)
2. [静的分析層](#2-静的分析層)
3. [ポリシーエンジン](#3-ポリシーエンジン)
4. [承認フロー状態機械](#4-承認フロー状態機械)
5. [セッションと --resume セマンティクス](#5-セッションと---resume-セマンティクス)
6. [Agent / Model の基盤](#6-agent--model-の基盤)
7. [監査ログイベント目次](#7-監査ログイベント目次)
8. [モジュール構成](#8-モジュール構成)

---

## 1. 5 層システム概要

```
┌─────────────────────────────────────────────────────┐
│  層 0 │ 転送層         Telegram Bot API / HTTPS      │
│  層 1 │ 受信層         FastAPI Webhook (server.py)   │
│  層 2 │ ポリシー層     Policy Engine + Approval     │
│  層 3 │ 実行層         Copilot CLI subprocess        │
│  層 4 │ 監査層         audit_log.jsonl (追記型)      │
└─────────────────────────────────────────────────────┘
```

各層は明確に定義された 1 つの責任を持ちます。`server.py` はすべての層にまたがるオーケストレーション ポイントであり、ポリシーロジックを含みません。

**データフロー（通常実行パス）:**

```
Telegram Update
  → build_message_context()          # 層 1 解析
  → _decide_plan_policy()            # 層 2 承認が必要か判定
  → [承認ゲート（必要な場合）]        # 層 2 人間による認可待機
  → _plan_actions_with_copilot()     # 層 3 計画（CLI 呼び出し）
  → _execute_copilot()               # 層 3 実行（CLI 呼び出し）
  → _send_telegram_*()               # 層 0 返信
  → _append_audit()                  # 層 4 ログ記録
```

---

## 2. 静的分析層

メッセージテキストの段階で、CLI 呼び出しよりも前、ポリシー決定よりも前に、**構造的特徴**を評価します。

| チェック項目 | 関数 | 説明 |
|---|---|---|
| コードブロック検出 | `_has_code_block()` | メッセージに ` ```...``` ` を含む → `has_code=True` に設定 |
| パス検出 | `_looks_like_path()` | `/`, `\`, `~`, `.` などのパス指標を含む |
| リスクキーワード | `_estimate_risk_from_text()` | 既知の危険キーワードにマッチ、`risk_kind` を出力 |
| Shadow シグナル | `_recommend_shadow_strategy()` | 静的特徴に基づいて allow/challenge/deny + 信頼度を出力 |

**Shadow 分析 3 段階:**

```
allow     confidence < LOW_THRESHOLD   → エスカレーション不要
challenge LOW_THRESHOLD ≤ confidence   → 承認追加を推奨
deny      confidence ≥ HIGH_THRESHOLD  → 拒否を推奨
```

静的分析結果は `pipeline_context.MessageContext` に書き込まれ、`_decide_plan_policy()` への入力の 1 つになります。

---

## 3. ポリシーエンジン

**ファイル:** `core/policy_engine.py`  
**設計原則:** 純粋関数、I/O なし、副作用なし。

### 3.1 _decide_plan_policy()

`PlanPolicyResult`（TypedDict）を出力し、以下を含みます：

| フィールド | 型 | 説明 |
|---|---|---|
| `requires_approval` | bool | 実行前に人間の確認が必要か？ |
| `plan_fail_closed` | bool | 計画失敗時は強制拒否するか（vs. ダウングレード許可）？ |
| `effective_risk_kind` | str | 有効なリスクレベル（`low/medium/high/critical`） |
| `effective_reason` | str | 決定根拠（監査に記録） |
| `approval_source` | str | 決定元（`static_analysis / grant / config`） |

決定優先度（高から低）:

1. 既存の grant → 許可
2. プロジェクトレベルの `REQUIRE_APPROVAL_ALWAYS=true` → 承認を強制
3. `effective_risk_kind == critical` → 承認を強制
4. Shadow enforcement エスカレーション → 承認を強制するかもしれない
5. デフォルト → `DEFAULT_REQUIRE_APPROVAL` 設定を使用

### 3.2 _recommend_shadow_strategy()

```python
def _recommend_shadow_strategy(ctx: MessageContext) -> ShadowRecommendation:
    # 入力: 静的分析結果 (risk_kind, has_code, looks_like_path, ...)
    # 出力: {"strategy": "allow|challenge|deny", "confidence": float, "reason": str}
```

### 3.3 _decide_shadow_enforcement()

機能フラグ（`SHADOW_ENFORCEMENT_ENABLED`）で制御されます。  
有効な場合、`challenge` と `deny` の shadow 推奨は実際のポリシー制約に**強制的にアップグレード**され、`plan_fail_closed` に書き込まれます。

---

## 4. 承認フロー状態機械

**ファイル:** `core/approval_flow.py`  
依存性注入パターン（`ApprovalFlowDeps` dataclass）を使用し、テストとバックエンド交換が容易です。

### 4.1 4 レベルの認可スコープ

| スコープ | 説明 | 保存キー |
|---|---|---|
| `user` | このユーザーへのグローバル grant | `grants.user.<user_id>` |
| `agent` | 特定 agent への grant | `grants.agent.<agent_name>` |
| `project` | 特定プロジェクトパスへの grant | `grants.project.<hash>` |
| `conversation` | 単一会話有効（永続化されない） | メモリ内 dict |

### 4.2 状態遷移

```
[pending]
    ↓ ユーザーが Telegram inline ボタンをクリック
[approved / denied / denied_once]
    ↓ approved → grant ストアに書き込み
    ↓ denied   → deny ストアに書き込み + 通知
    ↓ denied_once → この回は拒否、永続化されない
```

### 4.3 承認キーボード

`build_approval_keyboard()` は Telegram `InlineKeyboardMarkup` を生成し、以下を含みます：

- **今回のみ承認** (`approve_once`)
- **この Agent を承認** (`approve_agent`)
- **このプロジェクトを承認** (`approve_project`)
- **常に承認** (`approve_always`)
- **拒否** (`deny`)
- **この Agent を永久拒否** (`deny_agent`)

---

## 5. セッションと --resume セマンティクス

### 5.1 `--resume` の意味

`--resume <session_id>` = **同じ Copilot CLI セッションコンテキストを継続使用する**。  
これは「スナップショットを復元する」ことではなく、むしろ「前の会話のコンテキストを使用して返信する」ことです。

効果:
- 背景を説明し直すことなく会話履歴が保持される
- 同じ `conversation_id` は同じ `--resume` セッション ID にマップ

### 5.2 セッション保存

```json
// approval_store.json → sessions
{
  "sessions": {
    "<user_id>": "<copilot_session_id>"
  }
}
```

`_RUNTIME_STORE["sessions"]` は実行成功後にディスクに書き戻されます。

### 5.3 Playwright クラッシュ時のフォールバック

Copilot CLI は Playwright subprocess クラッシュにより失敗することがあります。  
クラッシュシグナル検出時、システムは **`--resume` を積極的に破棄**して再試行し、セッション汚染を回避します。  
破棄後、新しいセッション ID がストアを上書きします。

---

## 6. Agent / Model の基盤

### 6.1 利用可能な Agent リスト

環境変数 `COPILOT_AGENTS`（JSON 配列）で設定：

```env
COPILOT_AGENTS=["coding", "research", "general"]
```

現在のアクティブ agent は `USER_ACTIVE_AGENT`（メモリ内 dict、user_id でインデックス）に保存されます。

### 6.2 利用可能なモデルリスト

環境変数 `COPILOT_MODELS`（JSON 配列）で設定：

```env
COPILOT_MODELS=["claude-sonnet-4-5", "gpt-4o", "o3"]
```

現在のアクティブモデルは `USER_ACTIVE_MODEL`（メモリ内 dict）に保存されます。  
`model_id` は計画と実行の両フェーズで `--model` パラメータ経由で CLI に渡されます。

### 6.3 CLI 呼び出しシグネチャ

**計画フェーズ:**
```bash
gh copilot suggest "<prompt>" \
  --agent <agent_name> \
  --model <model_id> \
  [--resume <session_id>]
```

**実行フェーズ:**
```bash
gh copilot suggest "<prompt>" \
  --agent <agent_name> \
  --model <model_id> \
  --allow-all-tools \
  [--resume <session_id>]
```

---

## 7. 監査ログイベント目次

**ファイル:** `audit_log.jsonl`（追記型、1 行 1 つの JSON オブジェクト）

すべてのイベントの共通フィールド:

| フィールド | 説明 |
|---|---|
| `timestamp` | ISO 8601 UTC |
| `event` | イベントタイプ（下表参照） |
| `user_id` | Telegram ユーザー ID |
| `model` | その時点でのアクティブモデル id（あれば） |

### イベントタイプ

| event | トリガー条件 | キー拡張フィールド |
|---|---|---|
| `message_received` | ユーザーがメッセージを送信 | `text`, `risk_kind`, `shadow_strategy` |
| `plan_policy_decided` | ポリシー決定完了 | `requires_approval`, `effective_risk_kind`, `approval_source` |
| `approval_requested` | 承認リクエスト送信 | `pending_id`, `prompt_preview` |
| `approval_grant` | ユーザーが承認クリック | `scope`, `model`, `grant_id` |
| `approval_deny_once` | ユーザーが拒否クリック | `scope`, `model` |
| `plan_started` | 計画開始 | `agent`, `session_id` |
| `plan_completed` | 計画完了 | `actions_count`, `duration_ms` |
| `execute_started` | 実行開始 | `agent`, `session_id`, `playwright_retry` |
| `execute_completed` | 実行完了 | `exit_code`, `duration_ms` |
| `execute_failed` | 実行失敗 | `error`, `exit_code` |

### オフライン分析ツール

```bash
# 異なる閾値の下での承認決定を再生、結果を比較
python scripts/audit_analyzer.py \
  --since 2026-03-01 \
  --replay-thresholds 0.6,0.7,0.8,0.9 \
  --json-out summary.json
```

---

## 8. モジュール構成

```
telegram-copilot-cli-webhook/
├── server.py                  # エントリ；FastAPI webhook；層オーケストレーション
├── approval_store.json        # ランタイム永続状態（grants、sessions、pending）
├── audit_log.jsonl            # 監査イベントストリーム（追記型）
├── core/
│   ├── __init__.py
│   ├── policy_engine.py       # 純粋関数型ポリシー決定（I/O なし）
│   ├── approval_flow.py       # 認可状態機械 + 依存性注入
│   ├── pipeline_context.py    # MessageContext dataclass + builder
│   ├── runtime_state.py       # 設定解析、ストア永続化、監査ログ
│   └── telegram_io.py         # すべての Telegram トランスポートヘルパー
├── scripts/
│   └── audit_analyzer.py      # 監査オフライン分析 + 閾値再生
├── docs/
│   ├── architecture.ja.md     # このファイル
│   └── images/
├── README.md
├── README.ja.md
├── server.py
├── start.ps1
└── restart.ps1
```

### 拡張ポイント

| したいこと | 変更箇所 |
|---|---|
| リスクキーワード追加 | `core/policy_engine.py` → `_RISK_KEYWORDS` |
| 承認スコープ追加 | `core/approval_flow.py` → `ApprovalScope` + `handle_callback_approval()` |
| ストレージバックエンド交換（例：SQLite） | `core/runtime_state.py` → `_load_store()` / `_save_store()` |
| 監査フィールド追加 | `core/runtime_state.py` → `_append_audit()` |
| Telegram 送信モード追加 | `core/telegram_io.py` |
| Agent 追加 | 環境変数 `COPILOT_AGENTS` + コード変更なし |
| Model 追加 | 環境変数 `COPILOT_MODELS` + コード変更なし |
