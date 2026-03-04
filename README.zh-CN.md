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

多数 AI Agent 无法接触真实数据的原因只有一个：**不可控风险**。

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

该模型支持“逐步放权”。


## Telegram 命令

| 命令 | 说明 |
|------|------|
| `/help` | 查看帮助 |
| `/new` | 开始新的 Copilot 会话 |
| `/sessions` | 查看最近会话列表 |
| `/use <id>` | 切换到历史会话 |

普通消息自动续接当前会话。