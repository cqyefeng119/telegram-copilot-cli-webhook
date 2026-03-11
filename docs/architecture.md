# Architecture Memo

> This document is for future maintainers (including AI) and records the internal structure, design decisions, and extension points of the Webhook system.  
> Last updated: 2026-03-11

---

## Table of Contents

1. [Five-Layer System Overview](#1-five-layer-system-overview)
2. [Static Analysis Layer](#2-static-analysis-layer)
3. [Policy Engine](#3-policy-engine)
4. [Approval Flow State Machine](#4-approval-flow-state-machine)
5. [Session and --resume Semantics](#5-session-and---resume-semantics)
6. [Agent / Model Foundation](#6-agent--model-foundation)
7. [Audit Log Event Catalog](#7-audit-log-event-catalog)
8. [Module Layout](#8-module-layout)

---

## 1. Five-Layer System Overview

```
┌─────────────────────────────────────────────────────┐
│  Layer 0 │ Transport        Telegram Bot API / HTTPS │
│  Layer 1 │ Reception        FastAPI Webhook (server.py) │
│  Layer 2 │ Policy           Policy Engine + Approval │
│  Layer 3 │ Execution        Copilot CLI subprocess    │
│  Layer 4 │ Audit            audit_log.jsonl (append-only)│
└─────────────────────────────────────────────────────┘
```

Each layer has a single, well-defined responsibility. `server.py` is the orchestration point across all layers and contains no policy logic itself.

**Data Flow (Normal Execution Path):**

```
Telegram Update
  → build_message_context()          # Layer 1 parse
  → _decide_plan_policy()            # Layer 2 decide if approval needed
  → [approval gate if required]      # Layer 2 wait for human authorization
  → _plan_actions_with_copilot()     # Layer 3 plan (CLI call)
  → _execute_copilot()               # Layer 3 execute (CLI call)
  → _send_telegram_*()               # Layer 0 reply
  → _append_audit()                  # Layer 4 log
```

---

## 2. Static Analysis Layer

Static analysis at the message text stage evaluates **structural features** before CLI invocation and before policy decisions.

| Check | Function | Description |
|---|---|---|
| Code block detection | `_has_code_block()` | Message contains ` ```...``` ` → mark `has_code=True` |
| Path detection | `_looks_like_path()` | Contains `/`, `\`, `~`, `.` and other path indicators |
| Risk keywords | `_estimate_risk_from_text()` | Match known dangerous keywords, output `risk_kind` |
| Shadow signal | `_recommend_shadow_strategy()` | Based on static features, output allow/challenge/deny + confidence |

**Shadow Analysis Three Tiers:**

```
allow     confidence < LOW_THRESHOLD   → no escalation needed
challenge LOW_THRESHOLD ≤ confidence   → recommend adding approval
deny      confidence ≥ HIGH_THRESHOLD  → recommend rejection
```

Static analysis results are written into `pipeline_context.MessageContext` and serve as one input to `_decide_plan_policy()`.

---

## 3. Policy Engine

**File:** `core/policy_engine.py`  
**Design Principle:** Pure functions, no I/O, no side effects.

### 3.1 _decide_plan_policy()

Outputs a `PlanPolicyResult` (TypedDict) containing:

| Field | Type | Description |
|---|---|---|
| `requires_approval` | bool | Does execution require human review first? |
| `plan_fail_closed` | bool | If planning fails, force reject (vs. downgrade and allow)? |
| `effective_risk_kind` | str | Final risk tier in effect (`low/medium/high/critical`) |
| `effective_reason` | str | Decision rationale (goes into audit) |
| `approval_source` | str | Decision origin (`static_analysis / grant / config`) |

Decision priority (high to low):

1. Existing grant → allow
2. Project-level `REQUIRE_APPROVAL_ALWAYS=true` → force approval
3. `effective_risk_kind == critical` → force approval
4. Shadow enforcement escalation → may force approval
5. Default → use `DEFAULT_REQUIRE_APPROVAL` config

### 3.2 _recommend_shadow_strategy()

```python
def _recommend_shadow_strategy(ctx: MessageContext) -> ShadowRecommendation:
    # Input: static analysis result (risk_kind, has_code, looks_like_path, ...)
    # Output: {"strategy": "allow|challenge|deny", "confidence": float, "reason": str}
```

### 3.3 _decide_shadow_enforcement()

Controlled by feature flag (`SHADOW_ENFORCEMENT_ENABLED`).  
When enabled, shadow recommendations of `challenge` and `deny` are **force-upgraded** to actual policy constraints and written into `plan_fail_closed`.

---

## 4. Approval Flow State Machine

**File:** `core/approval_flow.py`  
Uses dependency injection pattern (`ApprovalFlowDeps` dataclass) for easy testing and backend swapping.

### 4.1 Four-Level Authorization Scopes

| Scope | Description | Storage Key |
|---|---|---|
| `user` | Global grant for this user | `grants.user.<user_id>` |
| `agent` | Grant for specific agent | `grants.agent.<agent_name>` |
| `project` | Grant for specific project path | `grants.project.<hash>` |
| `conversation` | Valid for single convo (not persisted) | In-memory dict |

### 4.2 State Transitions

```
[pending]
    ↓ user clicks Telegram inline button
[approved / denied / denied_once]
    ↓ approved → write to grant store
    ↓ denied   → write to deny store + notify
    ↓ denied_once → reject this time, not persistent
```

### 4.3 Approval Keyboard

`build_approval_keyboard()` generates a Telegram `InlineKeyboardMarkup` with:

- **Approve once** (`approve_once`)
- **Approve this Agent** (`approve_agent`)
- **Approve this project** (`approve_project`)
- **Always approve** (`approve_always`)
- **Deny** (`deny`)
- **Deny this Agent forever** (`deny_agent`)

---

## 5. Session and --resume Semantics

### 5.1 Meaning of `--resume`

`--resume <session_id>` = **continue using the same Copilot CLI session context**.  
This is **not** "restore a snapshot," but rather "use the previous conversation's context to reply."

Effects:
- Conversation history is retained without re-explaining background
- Same `conversation_id` maps to same `--resume` session id

### 5.2 Session Storage

```json
// approval_store.json → sessions
{
  "sessions": {
    "<user_id>": "<copilot_session_id>"
  }
}
```

`_RUNTIME_STORE["sessions"]` is written back to disk after every successful execution.

### 5.3 Playwright Crash Fallback

Copilot CLI sometimes fails due to Playwright subprocess crash.  
When a crash signal is detected, the system **actively discards `--resume`** and retries once to avoid session pollution.  
After discard, a new session id overwrites the store.

---

## 6. Agent / Model Foundation

### 6.1 Available Agent List

Configured via environment variable `COPILOT_AGENTS` (JSON array):

```env
COPILOT_AGENTS=["coding", "research", "general"]
```

Current active agent is stored in `USER_ACTIVE_AGENT` (in-memory dict, indexed by user_id).

### 6.2 Available Model List

Configured via environment variable `COPILOT_MODELS` (JSON array):

```env
COPILOT_MODELS=["claude-sonnet-4-5", "gpt-4o", "o3"]
```

Current active model is stored in `USER_ACTIVE_MODEL` (in-memory dict).  
`model_id` is passed to CLI via `--model` parameter in both planning and execution phases.

### 6.3 CLI Call Signature

**Planning phase:**
```bash
gh copilot suggest "<prompt>" \
  --agent <agent_name> \
  --model <model_id> \
  [--resume <session_id>]
```

**Execution phase:**
```bash
gh copilot suggest "<prompt>" \
  --agent <agent_name> \
  --model <model_id> \
  --allow-all-tools \
  [--resume <session_id>]
```

---

## 7. Audit Log Event Catalog

**File:** `audit_log.jsonl` (append-only, one JSON object per line)

Common fields for all events:

| Field | Description |
|---|---|
| `timestamp` | ISO 8601 UTC |
| `event` | Event type (see table below) |
| `user_id` | Telegram user ID |
| `model` | Active model id at the time (if any) |

### Event Types

| event | Triggered When | Key Extra Fields |
|---|---|---|
| `message_received` | User sends message | `text`, `risk_kind`, `shadow_strategy` |
| `plan_policy_decided` | Policy decision completes | `requires_approval`, `effective_risk_kind`, `approval_source` |
| `approval_requested` | Approval request sent | `pending_id`, `prompt_preview` |
| `approval_grant` | User clicks approve | `scope`, `model`, `grant_id` |
| `approval_deny_once` | User clicks deny | `scope`, `model` |
| `plan_started` | Planning begins | `agent`, `session_id` |
| `plan_completed` | Planning finishes | `actions_count`, `duration_ms` |
| `execute_started` | Execution begins | `agent`, `session_id`, `playwright_retry` |
| `execute_completed` | Execution finishes | `exit_code`, `duration_ms` |
| `execute_failed` | Execution fails | `error`, `exit_code` |

### Offline Analysis Tool

```bash
# Replay approval decisions under different thresholds, compare outcomes
python scripts/audit_analyzer.py \
  --since 2026-03-01 \
  --replay-thresholds 0.6,0.7,0.8,0.9 \
  --json-out summary.json
```

---

## 8. Module Layout

```
telegram-copilot-cli-webhook/
├── server.py                  # Entry; FastAPI webhook; layer orchestration
├── approval_store.json        # Runtime persistent state (grants, sessions, pending)
├── audit_log.jsonl            # Audit event stream (append-only)
├── core/
│   ├── __init__.py
│   ├── policy_engine.py       # Pure-function policy decisions (no I/O)
│   ├── approval_flow.py       # Authorization state machine + dependency injection
│   ├── pipeline_context.py    # MessageContext dataclass + builder
│   ├── runtime_state.py       # Config parsing, store persistence, audit logging
│   └── telegram_io.py         # All Telegram transport helpers
├── scripts/
│   └── audit_analyzer.py      # Audit offline analysis + threshold replay
├── docs/
│   ├── architecture.md        # This file
│   └── images/
├── README.md
├── README.ja.md
├── README.zh-CN.md
├── server.py
├── start.ps1
└── restart.ps1
```

### Extension Points

| What to do | Where to change |
|---|---|
| Add new risk keywords | `core/policy_engine.py` → `_RISK_KEYWORDS` |
| Add new approval scope | `core/approval_flow.py` → `ApprovalScope` + `handle_callback_approval()` |
| Swap storage backend (e.g., SQLite) | `core/runtime_state.py` → `_load_store()` / `_save_store()` |
| Add audit field | `core/runtime_state.py` → `_append_audit()` |
| Add Telegram send mode | `core/telegram_io.py` |
| Add Agent | Environment variable `COPILOT_AGENTS` + no code change |
| Add Model | Environment variable `COPILOT_MODELS` + no code change |
