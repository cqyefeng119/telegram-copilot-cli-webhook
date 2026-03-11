from dataclasses import dataclass
from datetime import datetime
import secrets
from typing import Any, Awaitable, Callable


@dataclass(frozen=True)
class ApprovalFlowDeps:
    runtime_store: dict[str, Any]
    save_store: Callable[[], None]
    append_audit: Callable[[str, dict[str, Any]], None]
    answer_callback_query: Callable[[str, str], Awaitable[None]]
    send_telegram_message: Callable[[int, str], Awaitable[None]]
    execute_copilot: Callable[..., Awaitable[None]]
    normalize_domain: Callable[[str], str | None]
    project_scope_key: str
    allowed_user_ids: set[int]


def iter_allow_grants(runtime_store: dict[str, Any]) -> list[dict[str, Any]]:
    grants = runtime_store.get("grants") or []
    if not isinstance(grants, list):
        return []
    return [grant for grant in grants if isinstance(grant, dict) and grant.get("allow") is True]


def match_grant(grant: dict[str, Any], scope: str, scope_id: str, risk_kind: str) -> bool:
    if grant.get("scope") != scope:
        return False
    if str(grant.get("scope_id") or "") != scope_id:
        return False
    grant_risk = str(grant.get("risk") or "")
    return grant_risk == risk_kind or grant_risk == "*"


def build_authorization_scopes(
    user_id: int,
    agent_name: str | None,
    current_session: str | None,
    project_scope_key: str,
) -> tuple[tuple[str, str], ...]:
    return (
        ("user", str(user_id)),
        ("agent", agent_name or ""),
        ("project", project_scope_key),
        ("conversation", current_session or "new-session"),
    )


def resolve_allow_scope(
    user_id: int,
    agent_name: str | None,
    current_session: str | None,
    risk_kind: str,
    runtime_store: dict[str, Any],
    project_scope_key: str,
) -> str | None:
    scopes = build_authorization_scopes(user_id, agent_name, current_session, project_scope_key)
    grants = iter_allow_grants(runtime_store)
    for scope, scope_id in scopes:
        if not scope_id:
            continue
        if any(match_grant(grant, scope, scope_id, risk_kind) for grant in grants):
            return scope
    return None


def upsert_allow_grant(
    scope: str,
    scope_id: str,
    risk_kind: str,
    by_user_id: int,
    runtime_store: dict[str, Any],
    save_store: Callable[[], None],
) -> None:
    if not scope_id:
        return
    grants = iter_allow_grants(runtime_store)
    for grant in grants:
        if match_grant(grant, scope, scope_id, risk_kind):
            return
    grants.append(
        {
            "scope": scope,
            "scope_id": scope_id,
            "risk": risk_kind,
            "allow": True,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "by_user_id": by_user_id,
        }
    )
    runtime_store["grants"] = grants
    save_store()


def iter_domain_grants(
    runtime_store: dict[str, Any],
    normalize_domain: Callable[[str], str | None],
) -> list[dict[str, Any]]:
    grants = runtime_store.get("domain_grants") or []
    if not isinstance(grants, list):
        return []
    return [
        grant
        for grant in grants
        if isinstance(grant, dict) and grant.get("allow") is True and normalize_domain(str(grant.get("domain") or ""))
    ]


def match_domain_grant(
    grant: dict[str, Any],
    scope: str,
    scope_id: str,
    domain: str,
    normalize_domain: Callable[[str], str | None],
) -> bool:
    if grant.get("scope") != scope:
        return False
    if str(grant.get("scope_id") or "") != scope_id:
        return False

    granted_domain = normalize_domain(str(grant.get("domain") or ""))
    requested_domain = normalize_domain(domain)
    if not granted_domain or not requested_domain:
        return False
    return requested_domain == granted_domain or requested_domain.endswith(f".{granted_domain}")


def resolve_domain_allow_scope(
    user_id: int,
    agent_name: str | None,
    current_session: str | None,
    domains: list[str],
    runtime_store: dict[str, Any],
    normalize_domain: Callable[[str], str | None],
    project_scope_key: str,
) -> str | None:
    normalized_domains = [domain for domain in (normalize_domain(item) for item in domains) if domain]
    if not normalized_domains:
        return None

    scopes = build_authorization_scopes(user_id, agent_name, current_session, project_scope_key)
    grants = iter_domain_grants(runtime_store, normalize_domain)

    for scope, scope_id in scopes:
        if not scope_id:
            continue
        if all(
            any(match_domain_grant(grant, scope, scope_id, domain, normalize_domain) for grant in grants)
            for domain in normalized_domains
        ):
            return scope

    if all(
        any(
            match_domain_grant(grant, scope, scope_id, domain, normalize_domain)
            for scope, scope_id in scopes
            if scope_id
            for grant in grants
        )
        for domain in normalized_domains
    ):
        return "mixed"
    return None


def upsert_domain_grant(
    scope: str,
    scope_id: str,
    domain: str,
    by_user_id: int,
    runtime_store: dict[str, Any],
    save_store: Callable[[], None],
    normalize_domain: Callable[[str], str | None],
) -> None:
    if not scope_id:
        return
    normalized = normalize_domain(domain)
    if not normalized:
        return

    grants = iter_domain_grants(runtime_store, normalize_domain)
    for grant in grants:
        if match_domain_grant(grant, scope, scope_id, normalized, normalize_domain):
            return

    grants.append(
        {
            "scope": scope,
            "scope_id": scope_id,
            "domain": normalized,
            "allow": True,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "by_user_id": by_user_id,
        }
    )
    runtime_store["domain_grants"] = grants
    save_store()


def create_pending(payload: dict[str, Any], runtime_store: dict[str, Any], save_store: Callable[[], None]) -> str:
    pending_id = f"p{datetime.now().strftime('%m%d%H%M%S')}{secrets.token_hex(2)}"
    pending = runtime_store.get("pending") or {}
    pending[pending_id] = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        **payload,
    }
    runtime_store["pending"] = pending
    save_store()
    return pending_id


def pop_pending(pending_id: str, runtime_store: dict[str, Any], save_store: Callable[[], None]) -> dict[str, Any] | None:
    pending = runtime_store.get("pending") or {}
    if pending_id not in pending:
        return None
    item = pending.pop(pending_id)
    runtime_store["pending"] = pending
    save_store()
    return item


def build_approval_keyboard(pending_id: str, has_agent_scope: bool) -> list[list[dict[str, str]]]:
    keyboard: list[list[dict[str, str]]] = [
        [
            {"text": "✅ 仅本次", "callback_data": f"ap:{pending_id}:once"},
            {"text": "🔁 本对话允许同类", "callback_data": f"ap:{pending_id}:conversation"},
        ],
        [
            {"text": "📁 本项目允许同类", "callback_data": f"ap:{pending_id}:project"},
        ],
    ]
    if has_agent_scope:
        keyboard.append([
            {"text": "🤖 本Agent允许同类", "callback_data": f"ap:{pending_id}:agent"},
        ])
    keyboard.append([
        {"text": "❌ 拒绝", "callback_data": f"ap:{pending_id}:deny"},
    ])
    return keyboard


def render_approval_prompt(
    pending_id: str,
    risk_kind: str,
    reason: str,
    prompt: str,
    domains: list[str] | None = None,
    planned_actions: list[dict[str, Any]] | None = None,
) -> str:
    compact_prompt = (prompt or "").strip().replace("\n", " ")
    if len(compact_prompt) > 200:
        compact_prompt = compact_prompt[:200] + "..."
    domain_summary = ""
    if domains:
        shown = ", ".join(domains[:5])
        extra = "" if len(domains) <= 5 else f" (+{len(domains) - 5})"
        domain_summary = f"\n域名摘要: {shown}{extra}"
    action_summary = ""
    if planned_actions:
        snippets: list[str] = []
        for action in planned_actions[:3]:
            action_type = str(action.get("type") or "other")
            summary = str(action.get("summary") or "").strip()
            if summary:
                snippets.append(f"- {action_type}: {summary}")
        if snippets:
            action_summary = "\n动作摘要:\n" + "\n".join(snippets)
    return (
        "高风险操作默认拒绝，需审批。\n"
        f"审批单: {pending_id}\n"
        f"风险类型: {risk_kind}\n"
        f"命中规则: {reason}\n"
        f"{domain_summary}"
        f"{action_summary}\n"
        f"请求摘要: {compact_prompt}"
    )


async def handle_callback_approval(callback_query: dict[str, Any] | None, deps: ApprovalFlowDeps) -> bool:
    if not callback_query:
        return False

    callback_id = callback_query.get("id")
    callback_data = callback_query.get("data") or ""
    callback_user_id = callback_query.get("from", {}).get("id")
    callback_chat_id = (callback_query.get("message") or {}).get("chat", {}).get("id")

    if not callback_id:
        return True

    if deps.allowed_user_ids and callback_user_id not in deps.allowed_user_ids:
        await deps.answer_callback_query(callback_id, "Not allowed")
        return True

    if not callback_data.startswith("ap:"):
        await deps.answer_callback_query(callback_id, "Unknown action")
        return True

    parts = callback_data.split(":", maxsplit=2)
    if len(parts) != 3:
        await deps.answer_callback_query(callback_id, "Malformed action")
        return True

    pending_id = parts[1]
    action = parts[2]
    valid_actions = {"once", "conversation", "project", "agent", "deny"}
    if action not in valid_actions:
        await deps.answer_callback_query(callback_id, "Unknown action")
        return True

    pending_map = deps.runtime_store.get("pending") or {}
    pending = pending_map.get(pending_id)
    if not pending:
        await deps.answer_callback_query(callback_id, "审批单不存在或已处理")
        return True

    if int(pending.get("user_id", 0) or 0) != int(callback_user_id or 0):
        await deps.answer_callback_query(callback_id, "只能由原请求用户审批")
        return True

    pending_payload = pending
    risk_kind = str(pending_payload.get("risk_kind") or "general")
    risk_kinds_raw = pending_payload.get("risk_kinds") or []
    risk_kinds = [str(item).strip() for item in risk_kinds_raw if str(item).strip()]
    if not risk_kinds and risk_kind != "general":
        risk_kinds = [risk_kind]
    agent_name = pending_payload.get("agent_name")
    model_id = pending_payload.get("model_id")
    current_session = pending_payload.get("current_session")

    if action == "deny":
        pop_pending(pending_id, deps.runtime_store, deps.save_store)
        await deps.answer_callback_query(callback_id, "已拒绝（仅本次）")
        if callback_chat_id:
            await deps.send_telegram_message(callback_chat_id, "已拒绝本次高风险操作。")
        deps.append_audit("approval_deny_once", {
            "pending_id": pending_id,
            "user_id": callback_user_id,
            "risk_kind": risk_kind,
            "risk_kinds": risk_kinds,
            "model": model_id,
        })
        return True

    if action in {"conversation", "project", "agent"}:
        if action == "conversation":
            scope_id = current_session or "new-session"
        elif action == "project":
            scope_id = deps.project_scope_key
        else:
            if not agent_name:
                await deps.answer_callback_query(callback_id, "当前请求无 agent 上下文")
                return True
            scope_id = agent_name
        for item in risk_kinds:
            upsert_allow_grant(action, scope_id, item, int(callback_user_id), deps.runtime_store, deps.save_store)
        for domain in pending_payload.get("domains") or []:
            upsert_domain_grant(
                action,
                scope_id,
                str(domain),
                int(callback_user_id),
                deps.runtime_store,
                deps.save_store,
                deps.normalize_domain,
            )
        deps.append_audit("approval_grant", {
            "pending_id": pending_id,
            "user_id": callback_user_id,
            "scope": action,
            "scope_id": scope_id,
            "risk_kind": risk_kind,
            "risk_kinds": risk_kinds,
            "domains": pending_payload.get("domains") or [],
            "model": model_id,
        })

    pending_payload = pop_pending(pending_id, deps.runtime_store, deps.save_store)
    if not pending_payload:
        await deps.answer_callback_query(callback_id, "审批单已过期")
        return True

    await deps.answer_callback_query(callback_id, "已批准，开始执行")
    await deps.execute_copilot(
        user_id=int(pending_payload["user_id"]),
        chat_id=int(pending_payload["chat_id"]),
        copilot_prompt=str(pending_payload["copilot_prompt"]),
        current_session=current_session,
        agent_name=agent_name,
        model_id=model_id,
        evidence_requested=bool(pending_payload.get("evidence_requested", False)),
        approval_source=f"callback:{action}",
    )
    return True