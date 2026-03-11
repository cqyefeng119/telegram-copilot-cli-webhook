from typing import Literal, TypedDict


class _PlanPolicyDecision(TypedDict):
    plan_fail_closed: bool
    requires_approval: bool
    effective_risk_kind: str
    effective_reason: str
    allow_scope: str | None
    approval_source: str


class _ShadowStrategyRecommendation(TypedDict):
    strategy: str
    reason_codes: list[str]
    confidence: float
    summary: str


def _decide_shadow_enforcement(
    *,
    enabled: bool,
    scope: Literal["deny_only", "deny_and_challenge"],
    strategy: str,
) -> Literal["pass", "force_challenge", "force_deny"]:
    if not enabled:
        return "pass"
    if strategy == "deny":
        return "force_deny"
    if scope == "deny_and_challenge" and strategy == "challenge":
        return "force_challenge"
    return "pass"


def _recommend_shadow_strategy(
    *,
    plan_fail_closed: bool,
    has_network_action: bool,
    unauthorized_domains: list[str],
    missing_risk_kinds: list[str],
    planned_action_types: list[str],
) -> _ShadowStrategyRecommendation:
    reason_codes: list[str] = []

    if plan_fail_closed:
        reason_codes.append("plan_fail_closed")
    if has_network_action and unauthorized_domains:
        reason_codes.append("unauthorized_domains")

    sensitive_action_types = {"destructive", "repo_write", "secret"}
    if missing_risk_kinds:
        reason_codes.append("missing_risk_kinds")
    if any(action_type in sensitive_action_types for action_type in planned_action_types):
        reason_codes.append("sensitive_action_type")

    if plan_fail_closed or (has_network_action and bool(unauthorized_domains)):
        strategy = "deny"
        confidence = 0.98
    elif missing_risk_kinds or any(action_type in sensitive_action_types for action_type in planned_action_types):
        strategy = "challenge"
        confidence = 0.87
    else:
        strategy = "allow"
        confidence = 0.72

    summary = f"shadow strategy={strategy}; reasons={','.join(reason_codes) if reason_codes else 'none'}"
    return {
        "strategy": strategy,
        "reason_codes": reason_codes,
        "confidence": confidence,
        "summary": summary,
    }


def _decide_plan_policy(
    *,
    plan_parse_ok: bool,
    plan_confidence: float,
    plan_confidence_threshold: float,
    missing_risk_kinds: list[str],
    has_network_action: bool,
    unauthorized_domains: list[str],
    risk_scopes: dict[str, str | None],
    network_allow_scope: str | None,
) -> _PlanPolicyDecision:
    plan_fail_closed = not plan_parse_ok or plan_confidence < plan_confidence_threshold
    requires_approval = plan_fail_closed or bool(missing_risk_kinds) or (has_network_action and bool(unauthorized_domains))

    effective_risk_kind = "plan"
    reason_parts: list[str] = []
    if plan_fail_closed:
        if not plan_parse_ok:
            reason_parts.append("action plan 解析失败（fail-closed）")
        else:
            reason_parts.append(f"action plan 置信度过低: {plan_confidence:.2f} < {plan_confidence_threshold:.2f}")
    if missing_risk_kinds:
        effective_risk_kind = missing_risk_kinds[0]
        reason_parts.append(f"高风险动作未授权: {', '.join(missing_risk_kinds)}")
    if has_network_action and unauthorized_domains:
        effective_risk_kind = "network"
        shown_domains = unauthorized_domains
        domain_text = ", ".join(shown_domains[:5])
        more = "" if len(shown_domains) <= 5 else f" (+{len(shown_domains) - 5})"
        reason_parts.append(f"未授权网络域名: {domain_text}{more}" if domain_text else "未授权网络访问")
    effective_reason = "；".join(reason_parts) if reason_parts else "需审批"

    allow_scope = None
    if risk_scopes:
        allow_scope = ",".join(sorted({scope for scope in risk_scopes.values() if scope}))

    approval_source = (
        f"auto:risk:{allow_scope}"
        if allow_scope
        else (f"auto:network:{network_allow_scope}" if network_allow_scope else "auto:low-risk")
    )

    return {
        "plan_fail_closed": plan_fail_closed,
        "requires_approval": requires_approval,
        "effective_risk_kind": effective_risk_kind,
        "effective_reason": effective_reason,
        "allow_scope": allow_scope,
        "approval_source": approval_source,
    }