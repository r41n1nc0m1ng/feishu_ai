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
    lines = [
        "",
        "=== benchmark v2 summary ===",
        f"case: {summary.get('case_id', '')}",
        f"batches: {summary.get('total_batches', 0)}",
        f"pass: {summary.get('passed_batches', 0)}",
        f"fail: {summary.get('failed_batches', 0)}",
        f"checks: {summary.get('total_checks', 0)}",
        f"failed checks: {summary.get('failed_checks', 0)}",
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
