from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from types import SimpleNamespace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmark.replay_adapter import DualChannelReplayAdapter, ReplayResult
from benchmark_v2.evaluator import BenchmarkEvaluator
from benchmark_v2.reporting import build_dimension_summary, render_console_summary, write_json_report
from realtime.triggers import classify_realtime_action, is_topic_list_query


DEFAULT_FIXTURE_PATH = Path(__file__).with_name("full_demo_case_v2.json")


def load_case(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("batches"), list):
        raise ValueError("fixture must be an object with a batches list")
    return data


@dataclass
class BatchOutcome:
    batch_id: str
    scenario: str = ""
    chat_id: str = ""
    tags: list[str] = field(default_factory=list)
    expected_brief: str = ""
    realtime_actions: list[str] = field(default_factory=list)
    write_result_count: int = 0
    failures: list[str] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)


class OfflineReplayRunner:
    def __init__(self, adapter: DualChannelReplayAdapter | None = None):
        self.adapter = adapter or DualChannelReplayAdapter(realtime_entry=self._benchmark_realtime_entry)
        self.evaluator = BenchmarkEvaluator()

    async def _benchmark_realtime_entry(self, message):
        action = classify_realtime_action(message)
        if is_topic_list_query(getattr(message, "text", "")):
            action = "topic_list"
        return SimpleNamespace(action=action)

    async def run_case(
        self,
        fixture_path: str | Path = DEFAULT_FIXTURE_PATH,
        *,
        include_tags: set[str] | None = None,
        include_chats: set[str] | None = None,
    ) -> dict[str, Any]:
        case = load_case(fixture_path)
        case = self._filter_case(case, include_tags=include_tags, include_chats=include_chats)
        outcomes: list[BatchOutcome] = []

        self._reset_local_state(case)
        for batch in case["batches"]:
            outcomes.append(await self.run_batch(case, batch))

        case_eval = self.evaluator.evaluate_case(case)
        case_failures = [check.detail or check.name for check in case_eval.checks if not check.passed]
        overall_success = all(not o.failures for o in outcomes) and case_eval.passed
        summary = {
            "case_id": case.get("case_id", ""),
            "fixture": str(fixture_path),
            "filters": {
                "tags": sorted(include_tags or []),
                "chats": sorted(include_chats or []),
            },
            "total_batches": len(outcomes),
            "passed_batches": sum(1 for outcome in outcomes if not outcome.failures),
            "failed_batches": sum(1 for outcome in outcomes if outcome.failures),
            "total_checks": sum(outcome.metrics.get("total_checks", 0) for outcome in outcomes) + case_eval.metrics.get("total_checks", 0),
            "failed_checks": sum(outcome.metrics.get("failed_checks", 0) for outcome in outcomes) + case_eval.metrics.get("failed_checks", 0),
            "overall_success": overall_success,
            "batch_results": [o.__dict__ for o in outcomes],
            "case_checks": [check.__dict__ for check in case_eval.checks],
            "case_failures": case_failures,
        }
        summary["dimensions"] = build_dimension_summary(summary)
        report_path = write_json_report("benchmark_v2_latest.json", summary)
        print(render_console_summary(summary))
        print(f"report: {report_path}")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return summary

    async def run_batch(self, case: dict[str, Any], batch: dict[str, Any]) -> BatchOutcome:
        outcome = BatchOutcome(batch_id=str(batch.get("batch_id", "")))
        outcome.scenario = str(batch.get("scenario", "") or "")
        outcome.chat_id = str(batch.get("chat_id") or case.get("chat_id") or "")
        outcome.tags = [str(tag) for tag in (batch.get("tags") or [])]
        outcome.expected_brief = str(batch.get("expected_brief", "") or "")
        realtime_results: list[ReplayResult] = []
        for raw_msg in batch.get("messages") or []:
            result = await self.adapter.send_realtime_message(raw_msg, case=case, batch=batch)
            realtime_results.append(result)
            if result.ok and not result.skipped:
                outcome.realtime_actions.append(result.action)
            if not result.ok:
                outcome.failures.append(f"realtime:{result.message_id}:{result.error}")

        if os.getenv("FULL_WRITE", "").strip().lower() in {"1", "true", "yes", "on"}:
            write_result = await self.adapter.send_full_write_batch(batch, case=case)
        else:
            write_result = await self.adapter.send_write_batch(batch, case=case)

        outcome.write_result_count = write_result.result_count
        if not write_result.ok:
            outcome.failures.append(f"write:{write_result.error}")

        expected = batch.get("expected") or {}
        expected_actions = expected.get("realtime_actions")
        if expected_actions is not None and outcome.realtime_actions != expected_actions:
            outcome.failures.append(
                f"realtime_actions expected {expected_actions}, got {outcome.realtime_actions}"
            )
        expected_write = expected.get("write_result_count")
        if expected_write is not None and outcome.write_result_count != expected_write:
            outcome.failures.append(
                f"write_result_count expected {expected_write}, got {outcome.write_result_count}"
            )

        evaluation = self.evaluator.evaluate_batch(
            case=case,
            batch=batch,
            write_result=write_result,
            realtime_actions=outcome.realtime_actions,
        )
        outcome.checks = [check.__dict__ for check in evaluation.checks]
        outcome.metrics = evaluation.metrics
        for check in evaluation.checks:
            if not check.passed:
                outcome.failures.append(f"{check.name}:{check.detail}")
        return outcome

    def _reset_local_state(self, case: dict[str, Any]) -> None:
        from memory import store
        from memory.card_generator import _card_cache, _cards_by_object
        from memory.evidence_store import _block_cache
        from realtime.query_handler import _LAST_QUERY_CARD_BY_CHAT

        chat_ids = {
            str(case.get("chat_id") or ""),
            *[str(batch.get("chat_id") or "") for batch in case.get("batches") or []],
            *[str(chat_id) for chat_id in (case.get("chat_profiles") or {}).keys()],
        }
        chat_ids.discard("")

        with store._conn() as conn:
            for chat_id in chat_ids:
                conn.execute("DELETE FROM evidence_blocks WHERE chat_id=?", (chat_id,))
                conn.execute("DELETE FROM memory_cards WHERE chat_id=?", (chat_id,))
                conn.execute("DELETE FROM memory_relations WHERE chat_id=?", (chat_id,))
                conn.execute("DELETE FROM topic_summaries WHERE chat_id=?", (chat_id,))
                conn.execute("DELETE FROM chat_spaces WHERE chat_id=?", (chat_id,))

        _card_cache.clear()
        _cards_by_object.clear()
        _block_cache.clear()
        _LAST_QUERY_CARD_BY_CHAT.clear()

    def _filter_case(
        self,
        case: dict[str, Any],
        *,
        include_tags: set[str] | None,
        include_chats: set[str] | None,
    ) -> dict[str, Any]:
        batches = []
        for batch in case.get("batches") or []:
            tags = {str(tag) for tag in (batch.get("tags") or [])}
            chat_id = str(batch.get("chat_id") or case.get("chat_id") or "")
            if include_tags and not (tags & include_tags):
                continue
            if include_chats and chat_id not in include_chats:
                continue
            batches.append(batch)

        filtered = dict(case)
        filtered["batches"] = batches
        return filtered


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("fixture", nargs="?", default=str(DEFAULT_FIXTURE_PATH))
    parser.add_argument("--tag", action="append", default=[])
    parser.add_argument("--chat", action="append", default=[])
    args = parser.parse_args()
    summary = await OfflineReplayRunner().run_case(
        args.fixture,
        include_tags=set(args.tag or []),
        include_chats=set(args.chat or []),
    )
    raise SystemExit(0 if summary["overall_success"] else 1)


if __name__ == "__main__":
    asyncio.run(main())
