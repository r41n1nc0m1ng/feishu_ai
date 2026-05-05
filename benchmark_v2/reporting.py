from __future__ import annotations

import json
from collections import Counter, defaultdict
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


def render_console_summary(summary: dict[str, Any]) -> str:
    perf = summary.get("performance") or {}
    recall = summary.get("recall_metrics") or {}
    interference = summary.get("interference_metrics") or {}
    conflict = summary.get("conflict_metrics") or {}
    write_quality = summary.get("write_quality_metrics") or {}
    retrieval_quality = summary.get("retrieval_quality_metrics") or {}
    lines = [
        "",
        "=== benchmark v2 summary ===",
        f"case: {summary.get('case_id', '')}",
        f"batches: {summary.get('total_batches', 0)}",
        f"pass: {summary.get('passed_batches', 0)}",
        f"fail: {summary.get('failed_batches', 0)}",
        f"checks: {summary.get('total_checks', 0)}",
        f"failed checks: {summary.get('failed_checks', 0)}",
        f"case runtime ms: {perf.get('case_total_runtime_ms')}",
        f"avg realtime latency ms: {perf.get('avg_realtime_latency_ms')}",
        f"avg write latency ms: {perf.get('avg_write_latency_ms')}",
        f"recall top1/top3: {recall.get('top1_hit_rate')}/{recall.get('top3_hit_rate')}",
        f"interference pass/match: {interference.get('batch_pass_rate')}/{interference.get('realtime_action_match_rate')}",
        f"conflict pass/match/guard: {conflict.get('batch_pass_rate')}/{conflict.get('relation_match_rate')}/{conflict.get('forbidden_relation_match_rate')}",
        f"write quality card/relation/topic: {write_quality.get('memory_card_match_rate')}/{write_quality.get('relation_match_rate')}/{write_quality.get('topic_match_rate')}",
        f"retrieval quality final/evidence: {retrieval_quality.get('final_memory_hit_rate')}/{retrieval_quality.get('evidence_hit_rate')}",
        f"result: {'PASS' if summary.get('overall_success') else 'FAIL'}",
    ]
    return "\n".join(lines)


def build_dimension_summary(summary: dict[str, Any]) -> dict[str, Any]:
    by_chat: dict[str, dict[str, int]] = defaultdict(lambda: {"batches": 0, "failed_batches": 0})
    by_tag: dict[str, dict[str, int]] = defaultdict(lambda: {"batches": 0, "failed_batches": 0})
    action_counter: Counter[str] = Counter()

    for batch in summary.get("batch_results") or []:
        chat_id = str(batch.get("chat_id") or "")
        tags = batch.get("tags") or []
        failed = bool(batch.get("failures"))

        if chat_id:
            by_chat[chat_id]["batches"] += 1
            by_chat[chat_id]["failed_batches"] += int(failed)

        for tag in tags:
            by_tag[str(tag)]["batches"] += 1
            by_tag[str(tag)]["failed_batches"] += int(failed)

        for action in batch.get("realtime_actions") or []:
            action_counter[str(action)] += 1

    return {
        "by_chat": dict(sorted(by_chat.items())),
        "by_tag": dict(sorted(by_tag.items())),
        "realtime_action_distribution": dict(sorted(action_counter.items())),
    }
