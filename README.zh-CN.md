# Telegram Webhook（Copilot CLI）

---

## 概要

本项目这是一个：

* 高透明
* 高可控
* 低复杂
* 可扩展

的最小可信起点。
是建立 AI 与个人数据之间信任关系的第一层结构。提供一个**最小可控架构**：

```
Telegram（手机）
      ↓
Cloudflare Tunnel（固定域名 / HTTPS）
      ↓
localhost:8000（Webhook）
      ↓
单一 Agent（Copilot CLI，本地执行）
```

核心原则：

* Agent 永远在本地运行
* 所有操作可见
* 权限逐步放开
* 随时可人工接管

这不是“把 AI 接入一切”，而是建立**可审计的信任路径**。

![Telegram 交互实例](docs/images/telegram-demo.jpg)

---


## 解决的问题

多数 AI Agent 在沙盒环境里，不让接触私人数据的原因只有一个：**不可控风险**。

一旦接入：

* 私人日历
* 本地文档
* 聊天软件
* 浏览器操作

如果缺乏可见性与权限边界，就会产生安全隐患。

本架构通过：

* 本地执行
* Telegram 作为可视化控制台
* Tunnel 作为安全入口

实现“远程操作，本地执行”。

---

## 可实现的场景

| 场景   | 运作方式                     |
| ---- | ------------------------ |
| 餐馆预约 | Agent 浏览器操作 → 发送截图 → 你确认 |
| 文档整理 | 在本地目录运行，不上传云端            |
| 聊天起草 | 先生成草稿 → 审核后发送            |
| 合同录入 | Agent 处理格式 → 人工校对关键字段    |

重点在于：

> 先给读权限，再给写权限。
> 先看结果，再授权执行。

---

## 使用前提

* Windows + PowerShell
* Python 3.11+
* 已安装 uv
* 可用 Copilot CLI（或自定义命令）
* 已从Telegram获取了Bot Token

---

## 基本配置流程

### 1. 环境准备

```
复制 .env.example 为 .env
填写 BOT_TOKEN
运行 uv sync
```
### 2. 启动方式

#### 方式 A：固定公网 URL（推荐）

在 `.env` 中配置：
```
PUBLIC_URL=https://your-domain.example.com
```
然后执行
```
.\start.ps1
```

#### 方式 B：Cloudflare Quick Tunnel

直接执行
```
.\start.ps1
```

Quick Tunnel 每次重启 URL 会变化。
生产环境建议使用固定 Tunnel。

---

## 安全配置

### 1. 用户白名单

服务启动后，向机器人发送任意消息，然后查看日志：

```powershell
Get-Content .\uvicorn.log -Tail 20
```

找到类似这样的一行：

```
[telegram] message from user_id=123456789 text='hello'
```

将其写入.env并重启：

```ini
ALLOWED_USER_IDS=123456789
```

```powershell
.\start.ps1
```

### 2. 权限控制思路

* 默认只读
* 关键步骤人工确认
* 高风险命令逐条审核
* 可随时停止进程

审批模型（MVP）：

* 默认启用 plan-first（动作计划）。
* 计划解析失败或置信度不足时，fail-closed 进入审批流程。
* 网络动作出现新域名/未授权域名时，必须审批。
* 证据截图由计划中的 `needs_evidence` 控制。
* 高风险请求默认拒绝，并发起 Telegram 审批卡片。
* Inline 按钮：
      * ✅ 仅本次
      * 🔁 本对话允许同类
      * 📁 本项目允许同类
      * 🤖 本Agent允许同类（仅设置 agent 时显示）
      * ❌ 拒绝
* 权限层级：user > agent > project > conversation > 单次。
* allow 向下继承；deny 仅对当前待审批请求生效。

执行回执格式：

* 所有执行回复统一为两段：
      * `1,结果...`
      * `2,过程详细...`
* 已移除静默模式（不再使用 `--silent`）。

该模型支持“逐步放权”。

### Shadow Enforcement（特性开关）

* 默认关闭：`SHADOW_ENFORCEMENT_ENABLED=false` 时，决策/执行路径保持既有逻辑；shadow 审计事件仅作为增量记录。
* 作用域 `SHADOW_ENFORCEMENT_SCOPE=deny_only`：
      * 当 shadow 策略为 `deny` 时直接硬阻断（不创建 pending、不执行）。
* 作用域 `SHADOW_ENFORCEMENT_SCOPE=deny_and_challenge`：
      * 包含 `deny_only` 的行为；
      * 当 shadow 策略为 `challenge` 时强制进入审批流程。
* 若作用域配置无效，会自动回退到 `deny_only`。


## Telegram 命令

| 命令 | 说明 |
|------|------|
| `/help` | 查看帮助 |
| `/new` | 开始新的 Copilot 会话 |
| `/sessions` | 查看最近会话列表 |
| `/session <id>` | 按 `/sessions` 数字编号切换会话 |
| `/agents` | 列出可用 agent（数字编号，`1` 为 `none`） |
| `/agent <id>` | 按数字编号设置当前 agent（执行时透传 `--agent <name>`） |
| `/models` | 列出可用模型（数字编号，显示倍率 `x0/x1/x3`） |
| `/model <id>` | 按数字编号设置当前模型（执行时透传 `--model <id>`） |

普通消息自动续接当前会话。

---

## 持久化与审计

* `approval_store.json`：保存 grants、pending、用户 session/agent/model 映射。
* `audit_log.jsonl`：追加写入审批/执行审计事件。
* 支持处理 `callback_query`，并正确调用 `answerCallbackQuery`。

### 离线审计分析器（C-Phase4）

可使用仅依赖标准库的脚本离线分析审计质量，并回放不同置信度阈值：

```powershell
# 基础分析（默认读取 ./audit_log.jsonl，时区 Asia/Shanghai）
python .\scripts\audit_analyzer.py

# 按时间/用户过滤 + 自定义阈值回放 + 导出 JSON
python .\scripts\audit_analyzer.py --since 2026-03-09 --until 2026-03-09T23:59:59 --user-id 7212596491 --replay-thresholds 0.6,0.7,0.8,0.9 --json-out .\audit_summary.json
```

注意：历史记录可能缺少 `plan_first_mode` 等字段；回放会在输出/建议中明确提示所使用的假设。