# 架构备忘录

> 本文档面向未来维护者（包括 AI），记录 Webhook 系统的内部结构、设计决策与扩展点。  
> 最后更新：2026-03-11

---

## 目录

1. [系统分层概览](#1-系统分层概览)
2. [静态分析层](#2-静态分析层)
3. [策略引擎（Policy Engine）](#3-策略引擎policy-engine)
4. [授权状态机（Approval Flow）](#4-授权状态机approval-flow)
5. [Session 与 --resume 语义](#5-session-与---resume-语义)
6. [Agent / Model 基盘](#6-agent--model-基盘)
7. [审计日志事件目录](#7-审计日志事件目录)
8. [模块布局](#8-模块布局)

---

## 1. 系统分层概览

```
┌─────────────────────────────────────────────────────┐
│  Layer 0 │ 传输层         Telegram Bot API / HTTPS   │
│  Layer 1 │ 接收层         FastAPI Webhook (server.py) │
│  Layer 2 │ 策略层         Policy Engine + Approval   │
│  Layer 3 │ 执行层         Copilot CLI subprocess      │
│  Layer 4 │ 审计层         audit_log.jsonl (append-only)│
└─────────────────────────────────────────────────────┘
```

每一层职责严格单一。`server.py` 是各层的编排点，自身不包含策略判断逻辑。

**数据流（正常执行路径）：**

```
Telegram Update
  → build_message_context()          # Layer 1 解析
  → _decide_plan_policy()            # Layer 2 判断是否需要审批
  → [approval gate if required]      # Layer 2 等待人工授权
  → _plan_actions_with_copilot()     # Layer 3 规划（CLI call）
  → _execute_copilot()               # Layer 3 执行（CLI call）
  → _send_telegram_*()               # Layer 0 回复
  → _append_audit()                  # Layer 4 写入
```

---

## 2. 静态分析层

静态分析在消息文本阶段对**结构特征**进行判断，早于 CLI 调用，早于策略决策。

| 检查项 | 函数 | 说明 |
|---|---|---|
| 代码块检测 | `_has_code_block()` | 消息含 ` ```...``` ` 则标记 `has_code=True` |
| 路径检测 | `_looks_like_path()` | 含 `/`、`\`、`~`、`.` 等路径特征 |
| 风险关键词 | `_estimate_risk_from_text()` | 匹配已知危险词汇列表，输出 `risk_kind` |
| Shadow 信号 | `_recommend_shadow_strategy()` | 基于静态特征给出 allow/challenge/deny + 置信度 |

**Shadow 分析三档：**

```
allow     confidence < LOW_THRESHOLD   → 无需升级
challenge LOW_THRESHOLD ≤ confidence   → 建议加审
deny      confidence ≥ HIGH_THRESHOLD  → 建议拒绝
```

静态分析结果会写入 `pipeline_context.MessageContext`，并作为 `_decide_plan_policy()` 的输入之一。

---

## 3. 策略引擎（Policy Engine）

**文件：** `core/policy_engine.py`  
设计原则：**纯函数，无 IO，无副作用。**

### 3.1 _decide_plan_policy()

输出一个 `PlanPolicyResult`（TypedDict），包含：

| 字段 | 类型 | 说明 |
|---|---|---|
| `requires_approval` | bool | 是否需要人工审批后才能执行 |
| `plan_fail_closed` | bool | 规划失败时是否强制拒绝（而非降级放行） |
| `effective_risk_kind` | str | 最终生效的风险等级（`low/medium/high/critical`） |
| `effective_reason` | str | 决策依据描述（写入审计） |
| `approval_source` | str | 决策来源（`static_analysis / grant / config`） |

决策优先级（高到低）：

1. 已存在的授权 grant → 放行
2. 项目级 `REQUIRE_APPROVAL_ALWAYS=true` → 强制审批
3. `effective_risk_kind == critical` → 强制审批
4. Shadow enforcement 升级 → 可能强制审批
5. 默认 → 按配置 `DEFAULT_REQUIRE_APPROVAL`

### 3.2 _recommend_shadow_strategy()

```python
def _recommend_shadow_strategy(ctx: MessageContext) -> ShadowRecommendation:
    # 输入: 静态分析结果 (risk_kind, has_code, looks_like_path, ...)
    # 输出: {"strategy": "allow|challenge|deny", "confidence": float, "reason": str}
```

### 3.3 _decide_shadow_enforcement()

Feature flag 控制（`SHADOW_ENFORCEMENT_ENABLED`）。  
当启用时，`challenge` 和 `deny` 的 shadow 建议会被**强制提升**为实际策略约束，写入 `plan_fail_closed`。

---

## 4. 授权状态机（Approval Flow）

**文件：** `core/approval_flow.py`  
使用依赖注入模式（`ApprovalFlowDeps` dataclass），便于测试与替换存储后端。

### 4.1 四级授权作用域

| 作用域 | 说明 | 存储键 |
|---|---|---|
| `user` | 针对该用户的全局授权 | `grants.user.<user_id>` |
| `agent` | 针对特定 agent 的授权 | `grants.agent.<agent_name>` |
| `project` | 针对特定项目路径的授权 | `grants.project.<hash>` |
| `conversation` | 单次对话有效（不持久化） | 内存 dict |

### 4.2 状态流转

```
[pending]
    ↓ 用户点击 Telegram inline button
[approved / denied / denied_once]
    ↓ approved → 写入 grant store
    ↓ denied   → 写入 deny store + 通知用户
    ↓ denied_once → 本次拒绝，不持久
```

### 4.3 Approval 键盘

`build_approval_keyboard()` 生成 Telegram `InlineKeyboardMarkup`，包含：

- **允许一次**（`approve_once`）
- **允许此 Agent**（`approve_agent`）
- **允许此项目**（`approve_project`）
- **始终允许**（`approve_always`）
- **拒绝**（`deny`）
- **永久拒绝此 Agent**（`deny_agent`）

---

## 5. Session 与 --resume 语义

### 5.1 `--resume` 的含义

`--resume <session_id>` = **继续使用同一 Copilot CLI 会话上下文**。  
这**不是**"恢复快照"，而是告诉 CLI"使用之前这次对话的上下文继续回复"。

效果：
- 保留对话历史，无需重新描述背景
- 同一个 `conversation_id` 对应同一组 `--resume` session

### 5.2 Session 存储

```json
// approval_store.json → sessions
{
  "sessions": {
    "<user_id>": "<copilot_session_id>"
  }
}
```

`_RUNTIME_STORE["sessions"]` 在每次成功执行后写回磁盘。

### 5.3 Playwright 崩溃时的降级

Copilot CLI 有时因 Playwright subprocess 崩溃而失败。  
检测到崩溃信号时，系统会**主动丢弃 `--resume`** 重试一次，以避免污染会话。  
丢弃后，新的 session id 会覆盖存储。

---

## 6. Agent / Model 基盘

### 6.1 可用 Agent 列表

通过环境变量 `COPILOT_AGENTS` （JSON 数组）配置：

```env
COPILOT_AGENTS=["coding", "research", "general"]
```

当前活跃 agent 存储在 `USER_ACTIVE_AGENT`（内存 dict，按 user_id 索引）。

### 6.2 可用 Model 列表

通过环境变量 `COPILOT_MODELS` （JSON 数组）配置：

```env
COPILOT_MODELS=["claude-sonnet-4-5", "gpt-4o", "o3"]
```

当前活跃 model 存储在 `USER_ACTIVE_MODEL`（内存 dict）。  
`model_id` 在规划和执行阶段均通过 `--model` 参数传入 CLI。

### 6.3 CLI 调用签名

**规划阶段：**
```bash
gh copilot suggest "<prompt>" \
  --agent <agent_name> \
  --model <model_id> \
  [--resume <session_id>]
```

**执行阶段：**
```bash
gh copilot suggest "<prompt>" \
  --agent <agent_name> \
  --model <model_id> \
  --allow-all-tools \
  [--resume <session_id>]
```

---

## 7. 审计日志事件目录

**文件：** `audit_log.jsonl`（append-only，每行一个 JSON 对象）

所有事件的公共字段：

| 字段 | 说明 |
|---|---|
| `timestamp` | ISO 8601 UTC |
| `event` | 事件类型（见下表） |
| `user_id` | Telegram user ID |
| `model` | 当时活跃的 model id（如有） |

### 事件类型

| event | 触发时机 | 关键额外字段 |
|---|---|---|
| `message_received` | 收到用户消息 | `text`, `risk_kind`, `shadow_strategy` |
| `plan_policy_decided` | 策略决策完成 | `requires_approval`, `effective_risk_kind`, `approval_source` |
| `approval_requested` | 发出审批请求 | `pending_id`, `prompt_preview` |
| `approval_grant` | 用户点击授权 | `scope`, `model`, `grant_id` |
| `approval_deny_once` | 用户拒绝本次 | `scope`, `model` |
| `plan_started` | 开始规划 | `agent`, `session_id` |
| `plan_completed` | 规划完成 | `actions_count`, `duration_ms` |
| `execute_started` | 开始执行 | `agent`, `session_id`, `playwright_retry` |
| `execute_completed` | 执行完成 | `exit_code`, `duration_ms` |
| `execute_failed` | 执行失败 | `error`, `exit_code` |

### 离线分析工具

```bash
# 重放不同阈值下的审批决策，对比效果
python scripts/audit_analyzer.py \
  --since 2026-03-01 \
  --replay-thresholds 0.6,0.7,0.8,0.9 \
  --json-out summary.json
```

---

## 8. 模块布局

```
telegram-copilot-cli-webhook/
├── server.py                  # 入口；FastAPI webhook；各层编排
├── approval_store.json        # 运行时持久化状态（grants, sessions, pending）
├── audit_log.jsonl            # 审计事件流（append-only）
├── core/
│   ├── __init__.py
│   ├── policy_engine.py       # 纯函数策略决策（无 IO）
│   ├── approval_flow.py       # 授权状态机 + 依赖注入
│   ├── pipeline_context.py    # MessageContext dataclass + builder
│   ├── runtime_state.py       # 配置解析、store 持久化、审计写入
│   └── telegram_io.py         # 所有 Telegram 传输辅助函数
├── scripts/
│   └── audit_analyzer.py      # 审计离线分析 + 阈值重放
├── docs/
│   ├── architecture.zh-CH.md  # 本文件
│   └── images/
├── README.zh-CH.md
├── server.py
├── start.ps1
└── restart.ps1
```

### 扩展点

| 想做什么 | 在哪里改 |
|---|---|
| 新增风险关键词 | `core/policy_engine.py` → `_RISK_KEYWORDS` |
| 新增授权作用域 | `core/approval_flow.py` → `ApprovalScope` + `handle_callback_approval()` |
| 更换存储后端（如 SQLite） | `core/runtime_state.py` → `_load_store()` / `_save_store()` |
| 新增审计字段 | `core/runtime_state.py` → `_append_audit()` |
| 新增 Telegram 发送模式 | `core/telegram_io.py` |
| 新增 Agent | 环境变量 `COPILOT_AGENTS` + 无需改代码 |
| 新增 Model | 环境变量 `COPILOT_MODELS` + 无需改代码 |
