#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from statistics import mean
from typing import Any
from zoneinfo import ZoneInfo


DEFAULT_REPLAY_THRESHOLDS = "0.5,0.7,0.8,0.9"


@dataclass
class ParseStats:
    total_lines: int = 0
    malformed_lines: int = 0
    non_object_lines: int = 0
    filtered_user: int = 0
    filtered_time: int = 0
    filtered_time_unknown: int = 0
    kept_records: int = 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="离线审计日志分析器（C-Phase4）")
    parser.add_argument("--input", default="./audit_log.jsonl", help="输入 audit JSONL 路径")
    parser.add_argument("--since", default=None, help="起始时间（含），如 2026-03-09 或 2026-03-09T10:00:00")
    parser.add_argument("--until", default=None, help="结束时间（含），如 2026-03-09 或 2026-03-09T23:59:59")
    parser.add_argument("--user-id", default=None, help="仅分析指定 user_id")
    parser.add_argument("--tz", default="Asia/Shanghai", help="时区（默认 Asia/Shanghai）")
    parser.add_argument(
        "--replay-thresholds",
        default=DEFAULT_REPLAY_THRESHOLDS,
        help="回放阈值列表，逗号分隔（例：0.5,0.7,0.8,0.9）",
    )
    parser.add_argument("--top", type=int, default=5, help="Top N 展示数量（默认 5）")
    parser.add_argument("--json-out", default=None, help="将结构化分析结果写入 JSON 文件")
    return parser.parse_args()


def parse_thresholds(raw: str) -> list[float]:
    values: list[float] = []
    for part in (raw or "").split(","):
        text = part.strip()
        if not text:
            continue
        try:
            value = float(text)
        except ValueError:
            continue
        if 0.0 <= value <= 1.0:
            values.append(value)
    return sorted(set(values))


def parse_time(value: str | None, tz: tzinfo, end_of_day: bool = False) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None

    dt: datetime | None = None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%dT%H:%M",
            "%Y-%m-%d",
        ):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None

    if dt.tzinfo is None:
        if len(text) == 10 and end_of_day:
            dt = dt.replace(hour=23, minute=59, second=59)
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def parse_record_ts(raw_ts: Any, tz: tzinfo) -> datetime | None:
    if not isinstance(raw_ts, str):
        return None
    text = raw_ts.strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def ratio(a: int, b: int) -> float:
    if b <= 0:
        return 0.0
    return (a / b) * 100.0


def top_items(counter: Counter[str], n: int) -> list[tuple[str, int]]:
    return counter.most_common(max(n, 1))


def format_top(counter: Counter[str], n: int, empty: str = "（无）") -> str:
    items = top_items(counter, n)
    if not items:
        return empty
    return "、".join(f"{k}={v}" for k, v in items)


def get_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def get_float(value: Any) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v:
        return None
    return v


def replay_table(plan_events: list[dict[str, Any]], thresholds: list[float]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not plan_events or not thresholds:
        return rows

    total = len(plan_events)
    actual_requires = sum(1 for e in plan_events if bool(e.get("requires_approval")))
    missing_plan_first_mode_count = sum(1 for e in plan_events if "plan_first_mode" not in e)

    for threshold in thresholds:
        predicted_requires = 0
        predicted_plan_fail = 0
        matches = 0
        for event in plan_events:
            parse_ok = bool(event.get("plan_parse_ok"))
            confidence = get_float(event.get("plan_confidence"))
            confidence_value = confidence if confidence is not None else -1.0
            plan_first_mode = bool(event.get("plan_first_mode", True))

            missing_risk = event.get("missing_risk_kinds")
            missing_risk_count = len(missing_risk) if isinstance(missing_risk, list) else 0
            unauthorized_domains_count = get_int(event.get("unauthorized_domains_count"), 0)

            plan_fail_closed = plan_first_mode and ((not parse_ok) or (confidence_value < threshold))
            requires = plan_fail_closed or (missing_risk_count > 0) or (unauthorized_domains_count > 0)

            if requires:
                predicted_requires += 1
            if plan_fail_closed:
                predicted_plan_fail += 1
            if requires == bool(event.get("requires_approval")):
                matches += 1

        rows.append(
            {
                "threshold": round(threshold, 3),
                "predicted_requires": predicted_requires,
                "predicted_requires_rate": round(ratio(predicted_requires, total), 2),
                "predicted_plan_fail_closed": predicted_plan_fail,
                "predicted_plan_fail_closed_rate": round(ratio(predicted_plan_fail, total), 2),
                "assumed_plan_first_mode_count": missing_plan_first_mode_count,
                "assumed_plan_first_mode_rate": round(ratio(missing_plan_first_mode_count, total), 2),
                "delta_vs_actual": predicted_requires - actual_requires,
                "decision_match_rate": round(ratio(matches, total), 2),
            }
        )
    return rows


def build_tuning_hints(
    replay_rows: list[dict[str, Any]],
    execution_total: int,
    execution_success: int,
    approval_required: int,
    unauthorized_domain_hits: int,
    replay_assumed_plan_first_mode_count: int,
) -> list[str]:
    hints: list[str] = []
    success_rate = ratio(execution_success, execution_total)

    if replay_rows:
        best_match = max(replay_rows, key=lambda x: x.get("decision_match_rate", 0.0))
        hints.append(
            f"阈值回放拟合度最高为 {best_match['threshold']:.3f}（决策匹配率 {best_match['decision_match_rate']:.2f}%）。"
        )
        if replay_assumed_plan_first_mode_count > 0:
            hints.append(
                "注意：本次回放包含缺失 plan_first_mode 字段的样本，已按 True（启用）假设计算 fail-closed。"
            )

    if approval_required > 0 and success_rate >= 95.0:
        hints.append("审批后执行成功率较高，可小步下调阈值（如 -0.05）以减少人工审批压力。")
    elif execution_total > 0 and success_rate < 80.0:
        hints.append("执行成功率偏低，建议上调阈值并优先完善高风险场景的计划质量。")

    if unauthorized_domain_hits > 0:
        hints.append("出现未授权域名命中，优先优化域名白名单与域名提取准确率。")

    if not hints:
        hints.append("当前样本量较小或分布平稳，建议持续采样后再做阈值调整。")
    return hints


def resolve_timezone(tz_name: str) -> tuple[tzinfo, str]:
    try:
        tz = ZoneInfo(tz_name)
        return tz, tz_name
    except Exception:
        if tz_name != "Asia/Shanghai":
            try:
                tz = ZoneInfo("Asia/Shanghai")
                return tz, "Asia/Shanghai"
            except Exception:
                pass
        fallback = timezone(timedelta(hours=8), name="UTC+08:00")
        return fallback, "UTC+08:00(fallback)"


def main() -> int:
    args = parse_args()
    input_path = Path(args.input)
    if not input_path.exists():
        print(f"输入文件不存在: {input_path}")
        return 2

    tz, tz_label = resolve_timezone(args.tz)
    if tz_label != args.tz:
        print(f"无法识别时区 {args.tz}，回退到 {tz_label}")

    since = parse_time(args.since, tz, end_of_day=False)
    until = parse_time(args.until, tz, end_of_day=True)
    thresholds = parse_thresholds(args.replay_thresholds)
    if not thresholds:
        thresholds = parse_thresholds(DEFAULT_REPLAY_THRESHOLDS)

    stats = ParseStats()
    records: list[dict[str, Any]] = []

    with input_path.open("r", encoding="utf-8") as fp:
        for line in fp:
            stats.total_lines += 1
            text = line.strip()
            if not text:
                continue
            try:
                obj = json.loads(text)
            except json.JSONDecodeError:
                stats.malformed_lines += 1
                continue
            if not isinstance(obj, dict):
                stats.non_object_lines += 1
                continue

            if args.user_id is not None and str(obj.get("user_id")) != str(args.user_id):
                stats.filtered_user += 1
                continue

            event_dt = parse_record_ts(obj.get("ts"), tz)
            if since is not None or until is not None:
                if event_dt is None:
                    stats.filtered_time_unknown += 1
                    continue
                if since is not None and event_dt < since:
                    stats.filtered_time += 1
                    continue
                if until is not None and event_dt > until:
                    stats.filtered_time += 1
                    continue

            obj["_dt"] = event_dt.isoformat() if event_dt else None
            records.append(obj)
            stats.kept_records += 1

    event_counter: Counter[str] = Counter()
    approval_source_counter: Counter[str] = Counter()
    risk_counter: Counter[str] = Counter()
    domain_counter: Counter[str] = Counter()
    confidence_values: list[float] = []
    plan_events: list[dict[str, Any]] = []

    approval_required = 0
    approval_grant = 0
    approval_deny_once = 0

    execution_total = 0
    execution_success = 0
    execution_failure = 0
    execution_timeout = 0
    execution_launch_error = 0

    for rec in records:
        event = str(rec.get("event") or "unknown")
        event_counter[event] += 1

        if event == "approval_required":
            approval_required += 1
        elif event == "approval_grant":
            approval_grant += 1
        elif event == "approval_deny_once":
            approval_deny_once += 1

        if event == "execution":
            execution_total += 1
            rc = get_int(rec.get("return_code"), default=1)
            if rc == 0:
                execution_success += 1
            else:
                execution_failure += 1
        elif event == "execution_timeout":
            execution_timeout += 1
        elif event == "execution_launch_error":
            execution_launch_error += 1

        approval_source = rec.get("approval_source")
        if approval_source is not None:
            approval_source_counter[str(approval_source)] += 1

        rk = rec.get("risk_kind")
        if isinstance(rk, str) and rk.strip():
            risk_counter[rk.strip()] += 1
        rks = rec.get("risk_kinds")
        if isinstance(rks, list):
            for item in rks:
                if isinstance(item, str) and item.strip():
                    risk_counter[item.strip()] += 1

        domains = rec.get("domains")
        if isinstance(domains, list):
            for item in domains:
                if isinstance(item, str) and item.strip():
                    domain_counter[item.strip().lower()] += 1

        plan_conf = get_float(rec.get("plan_confidence"))
        if plan_conf is not None:
            confidence_values.append(plan_conf)

        if event == "plan_policy_decision":
            plan_events.append(rec)

    replay_rows = replay_table(plan_events, thresholds)
    replay_assumed_plan_first_mode_count = (
        get_int(replay_rows[0].get("assumed_plan_first_mode_count"), 0) if replay_rows else 0
    )
    unauthorized_domain_hits = sum(
        1 for rec in records if get_int(rec.get("unauthorized_domains_count"), 0) > 0
    )
    hints = build_tuning_hints(
        replay_rows=replay_rows,
        execution_total=execution_total,
        execution_success=execution_success,
        approval_required=approval_required,
        unauthorized_domain_hits=unauthorized_domain_hits,
        replay_assumed_plan_first_mode_count=replay_assumed_plan_first_mode_count,
    )

    confidence_min = min(confidence_values) if confidence_values else None
    confidence_max = max(confidence_values) if confidence_values else None
    confidence_avg = mean(confidence_values) if confidence_values else None

    period_start = None
    period_end = None
    dts = [r.get("_dt") for r in records if r.get("_dt")]
    if dts:
        period_start = min(dts)
        period_end = max(dts)

    summary = {
        "input": str(input_path),
        "timezone": tz_label,
        "filters": {
            "since": since.isoformat() if since else None,
            "until": until.isoformat() if until else None,
            "user_id": str(args.user_id) if args.user_id is not None else None,
        },
        "parse": {
            "total_lines": stats.total_lines,
            "malformed_lines": stats.malformed_lines,
            "non_object_lines": stats.non_object_lines,
            "filtered_user": stats.filtered_user,
            "filtered_time": stats.filtered_time,
            "filtered_time_unknown": stats.filtered_time_unknown,
            "kept_records": stats.kept_records,
        },
        "period": {"start": period_start, "end": period_end},
        "counts": {
            "event_distribution": dict(event_counter),
            "execution_total": execution_total,
            "execution_success": execution_success,
            "execution_failure": execution_failure,
            "execution_timeout": execution_timeout,
            "execution_launch_error": execution_launch_error,
            "approval_required": approval_required,
            "approval_grant": approval_grant,
            "approval_deny_once": approval_deny_once,
        },
        "quality": {
            "execution_success_rate": round(ratio(execution_success, execution_total), 2),
            "approval_grant_rate": round(ratio(approval_grant, approval_required), 2),
            "approval_deny_rate": round(ratio(approval_deny_once, approval_required), 2),
            "top_approval_source": top_items(approval_source_counter, args.top),
        },
        "risk_domain_confidence": {
            "top_risks": top_items(risk_counter, args.top),
            "top_domains": top_items(domain_counter, args.top),
            "plan_confidence": {
                "count": len(confidence_values),
                "min": round(confidence_min, 4) if confidence_min is not None else None,
                "avg": round(confidence_avg, 4) if confidence_avg is not None else None,
                "max": round(confidence_max, 4) if confidence_max is not None else None,
            },
        },
        "threshold_replay": {
            "base_events": len(plan_events),
            "thresholds": thresholds,
            "rows": replay_rows,
            "hints": hints,
        },
    }

    print("=== 审计离线分析（C-Phase4）===")
    print(f"输入文件: {input_path}")
    print(
        f"时间范围: {since.isoformat() if since else '-∞'} ~ {until.isoformat() if until else '+∞'} | "
        f"用户: {args.user_id if args.user_id is not None else '全部'} | 时区: {tz_label}"
    )

    print("\n[1] 基础计数与事件分布")
    print(
        f"- 原始行={stats.total_lines}，保留={stats.kept_records}，坏行={stats.malformed_lines}，"
        f"非对象={stats.non_object_lines}，用户过滤={stats.filtered_user}，时间过滤={stats.filtered_time}"
    )
    print(
        f"- 覆盖时间: {period_start or 'N/A'} ~ {period_end or 'N/A'}"
    )
    print(f"- 事件分布 Top{max(args.top, 1)}: {format_top(event_counter, args.top)}")

    print("\n[2] 审批漏斗与执行质量")
    print(
        f"- 审批漏斗: required={approval_required} -> grant={approval_grant} -> deny_once={approval_deny_once}"
    )
    print(
        f"- 执行质量: execution={execution_total}，success={execution_success}，fail={execution_failure}，"
        f"timeout={execution_timeout}，launch_error={execution_launch_error}"
    )
    print(
        f"- 比率: grant_rate={ratio(approval_grant, approval_required):.2f}% | "
        f"deny_rate={ratio(approval_deny_once, approval_required):.2f}% | "
        f"exec_success_rate={ratio(execution_success, execution_total):.2f}%"
    )
    print(f"- approval_source Top{max(args.top, 1)}: {format_top(approval_source_counter, args.top)}")

    print("\n[3] 风险 / 域名 / 置信度统计")
    print(f"- 风险 Top{max(args.top, 1)}: {format_top(risk_counter, args.top)}")
    print(f"- 域名 Top{max(args.top, 1)}: {format_top(domain_counter, args.top)}")
    if confidence_values:
        print(
            f"- plan_confidence: count={len(confidence_values)} min={confidence_min:.4f} "
            f"avg={confidence_avg:.4f} max={confidence_max:.4f}"
        )
    else:
        print("- plan_confidence: 无可用样本")

    print("\n[4] 阈值回放与调参建议")
    if replay_rows:
        if replay_assumed_plan_first_mode_count > 0:
            print(
                f"- 注意：{replay_assumed_plan_first_mode_count} 条样本缺失 plan_first_mode，"
                "回放按 True（启用）假设计算 fail-closed。"
            )
        print("- 回放表（基于 plan_policy_decision）：")
        print("  threshold | pred_requires | pred_rate | pred_fail_closed | delta_vs_actual | match_rate")
        for row in replay_rows:
            print(
                "  "
                f"{row['threshold']:.3f}"
                f" | {row['predicted_requires']}"
                f" | {row['predicted_requires_rate']:.2f}%"
                f" | {row['predicted_plan_fail_closed']}"
                f" | {row['delta_vs_actual']:+d}"
                f" | {row['decision_match_rate']:.2f}%"
            )
    else:
        print("- 回放表: 无 plan_policy_decision 样本，无法进行阈值回放")
    for idx, hint in enumerate(hints, start=1):
        print(f"- 建议{idx}: {hint}")

    if args.json_out:
        output_path = Path(args.json_out)
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"\n已输出 JSON: {output_path}")
        except Exception as exc:
            print(f"\n写入 JSON 失败: {output_path} ({exc})")
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
