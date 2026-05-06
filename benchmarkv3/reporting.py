"""
benchmark v3 报告生成器：与 v2 不复用，因为 v3 指标形状不同。

输出三大块：
  query_metrics      —— 每 batch 松检查 + 总末严格检查（按粒度细分）
  conflict_metrics   —— 实跑链路推断的矛盾更新 + 模拟样例
  anti_interference  —— batch_006 noise 块判别正确率

每个 check 同时落到 .json 报告与控制台摘要。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


REPORT_DIR = Path(__file__).with_name("reports")


def ensure_report_dir() -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    return REPORT_DIR


def write_json_report(name: str, payload: dict[str, Any]) -> Path:
    report_dir = ensure_report_dir()
    path = report_dir / name
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


# ── query_metrics ─────────────────────────────────────────────────────────────


def build_query_metrics(
    *,
    per_batch_checks: list[dict[str, Any]],     # [{batch_id, name, passed, detail, query_type}]
    final_decision_checks: list[dict[str, Any]],
    final_evidence_checks: list[dict[str, Any]],
    final_topic_checks: list[dict[str, Any]],
    granularity_breakdown: dict[str, dict[str, int]],  # {memory_card: {expected:n, actual_correct:m}, ...}
) -> dict[str, Any]:
    def _rate(passed: int, total: int) -> float | None:
        return round(passed / total, 4) if total else None

    decision_passed = sum(1 for c in final_decision_checks if c["passed"])
    evidence_passed = sum(1 for c in final_evidence_checks if c["passed"])
    topic_passed    = sum(1 for c in final_topic_checks    if c["passed"])
    final_total     = len(final_decision_checks) + len(final_evidence_checks) + len(final_topic_checks)
    final_passed    = decision_passed + evidence_passed + topic_passed

    per_batch_passed = sum(1 for c in per_batch_checks if c["passed"])

    granularity_rates = {
        gran: round(v["actual_correct"] / v["expected"], 4) if v["expected"] else None
        for gran, v in granularity_breakdown.items()
    }

    return {
        "per_batch": {
            "total":      len(per_batch_checks),
            "passed":     per_batch_passed,
            "pass_rate":  _rate(per_batch_passed, len(per_batch_checks)),
            "details":    per_batch_checks,
        },
        "final": {
            "total":          final_total,
            "passed":         final_passed,
            "overall_pass_rate":   _rate(final_passed, final_total),
            "decision_pass_rate":  _rate(decision_passed, len(final_decision_checks)),
            "evidence_pass_rate":  _rate(evidence_passed, len(final_evidence_checks)),
            "topic_pass_rate":     _rate(topic_passed, len(final_topic_checks)),
            "decision_details":    final_decision_checks,
            "evidence_details":    final_evidence_checks,
            "topic_details":       final_topic_checks,
        },
        "granularity_accuracy": {
            "rates": granularity_rates,
            "raw":   granularity_breakdown,
        },
    }


# ── conflict_metrics ─────────────────────────────────────────────────────────


def build_conflict_metrics(
    *,
    chat_checks: list[dict[str, Any]],     # 从 result.json 推断的实跑断言
    simulated_checks: list[dict[str, Any]],  # 3 条模拟样例断言
) -> dict[str, Any]:
    def _rate(passed: int, total: int) -> float | None:
        return round(passed / total, 4) if total else None

    chat_passed = sum(1 for c in chat_checks if c["passed"])
    sim_passed  = sum(1 for c in simulated_checks if c["passed"])

    by_op: dict[str, dict[str, int]] = {}
    for c in chat_checks + simulated_checks:
        op = c.get("operation", "unknown")
        by_op.setdefault(op, {"total": 0, "passed": 0})
        by_op[op]["total"] += 1
        if c["passed"]:
            by_op[op]["passed"] += 1

    return {
        "from_chat": {
            "total":     len(chat_checks),
            "passed":    chat_passed,
            "pass_rate": _rate(chat_passed, len(chat_checks)),
            "details":   chat_checks,
        },
        "simulated": {
            "total":     len(simulated_checks),
            "passed":    sim_passed,
            "pass_rate": _rate(sim_passed, len(simulated_checks)),
            "details":   simulated_checks,
        },
        "by_operation": {
            op: {
                "total":     stats["total"],
                "passed":    stats["passed"],
                "pass_rate": _rate(stats["passed"], stats["total"]),
            }
            for op, stats in sorted(by_op.items())
        },
    }


# ── anti_interference ────────────────────────────────────────────────────────


def build_anti_interference_metrics(
    *,
    blocks_total: int,
    blocks_noise: int,
    noise_correct_rate: float | None,
    cards_unexpected: int,
) -> dict[str, Any]:
    return {
        "blocks_total":         blocks_total,
        "blocks_noise":         blocks_noise,
        "noise_correct_rate":   noise_correct_rate,
        "cards_unexpected":     cards_unexpected,
        "passed":               blocks_total > 0 and blocks_noise == blocks_total and cards_unexpected == 0,
    }


# ── 控制台摘要 ────────────────────────────────────────────────────────────────


def render_console_summary(report: dict[str, Any]) -> str:
    qm = report.get("query_metrics", {})
    cm = report.get("conflict_metrics", {})
    am = report.get("anti_interference_metrics", {})

    lines: list[str] = [
        "",
        "═══ benchmark v3 summary ═══",
        f"case: {report.get('case_id', '')}",
        "",
        "─ Query metrics ─",
        f"  per-batch loose:      {qm.get('per_batch', {}).get('passed')} / {qm.get('per_batch', {}).get('total')}    "
        f"rate={qm.get('per_batch', {}).get('pass_rate')}",
        f"  final overall:        {qm.get('final', {}).get('passed')} / {qm.get('final', {}).get('total')}    "
        f"rate={qm.get('final', {}).get('overall_pass_rate')}    "
        f"target≥0.7  {'PASS' if (qm.get('final', {}).get('overall_pass_rate') or 0) >= 0.7 else 'FAIL'}",
        f"  decision/evidence/topic: "
        f"{qm.get('final', {}).get('decision_pass_rate')} / "
        f"{qm.get('final', {}).get('evidence_pass_rate')} / "
        f"{qm.get('final', {}).get('topic_pass_rate')}",
        f"  granularity accuracy: {qm.get('granularity_accuracy', {}).get('rates')}",
        "",
        "─ Conflict update metrics ─",
        f"  from_chat:    {cm.get('from_chat', {}).get('passed')} / {cm.get('from_chat', {}).get('total')}    "
        f"rate={cm.get('from_chat', {}).get('pass_rate')}",
        f"  simulated:    {cm.get('simulated', {}).get('passed')} / {cm.get('simulated', {}).get('total')}    "
        f"rate={cm.get('simulated', {}).get('pass_rate')}",
        f"  by_operation: {cm.get('by_operation', {})}",
        "",
        "─ Anti-interference (batch_006) ─",
        f"  blocks total/noise:    {am.get('blocks_total')} / {am.get('blocks_noise')}    "
        f"rate={am.get('noise_correct_rate')}",
        f"  unexpected cards:      {am.get('cards_unexpected')}    "
        f"verdict={'PASS' if am.get('passed') else 'FAIL'}",
        "",
        f"report: {report.get('report_path', '(in-memory)')}",
        "",
    ]
    return "\n".join(lines)
