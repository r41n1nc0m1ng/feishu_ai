from __future__ import annotations

import argparse
import asyncio
import math
import json
import os
import sys
import time
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
    message_count: int = 0
    expected_brief: str = ""
    realtime_actions: list[str] = field(default_factory=list)
    write_result_count: int = 0
    write_input_count: int = 0
    realtime_latency_ms: list[float] = field(default_factory=list)
    write_latency_ms: float = 0.0
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
        case_started = time.perf_counter()
        original_case = load_case(fixture_path)
        case = self._filter_case(original_case, include_tags=include_tags, include_chats=include_chats)
        outcomes: list[BatchOutcome] = []

        self._reset_local_state(case)
        for batch in case["batches"]:
            outcomes.append(await self.run_batch(case, batch))

        run_case_eval = not bool(include_tags or include_chats)
        case_eval = self.evaluator.evaluate_case(case) if run_case_eval else self.evaluator.skipped_case_eval(
            "filtered run"
        )
        case_failures = [check.detail or check.name for check in case_eval.checks if not check.passed]
        overall_success = all(not o.failures for o in outcomes) and case_eval.passed
        summary = {
            "case_id": case.get("case_id", ""),
            "fixture": str(fixture_path),
            "filters": {
                "tags": sorted(include_tags or []),
                "chats": sorted(include_chats or []),
            },
            "case_eval_mode": "full" if run_case_eval else "skipped_for_filtered_run",
            "deep_eval_enabled": self.evaluator.deep_eval_enabled,
            "full_write_enabled": os.getenv("FULL_WRITE", "").strip().lower() in {"1", "true", "yes", "on"},
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
        summary["performance"] = self._build_performance_metrics(outcomes, case_started)
        if run_case_eval:
            summary["interference_metrics"] = self._build_interference_metrics(case, outcomes)
            summary["conflict_metrics"] = self._build_conflict_metrics(case, outcomes)
            summary["write_quality_metrics"] = self._build_write_quality_metrics(outcomes)
            summary["retrieval_quality_metrics"] = self._build_retrieval_quality_metrics(case, case_eval)
        if case_eval.metrics.get("recall_metrics") is not None:
            summary["recall_metrics"] = case_eval.metrics.get("recall_metrics")
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
        outcome.message_count = len(batch.get("messages") or [])
        outcome.expected_brief = str(batch.get("expected_brief", "") or "")
        realtime_results: list[ReplayResult] = []
        for raw_msg in batch.get("messages") or []:
            started = time.perf_counter()
            result = await self.adapter.send_realtime_message(raw_msg, case=case, batch=batch)
            outcome.realtime_latency_ms.append(round((time.perf_counter() - started) * 1000, 3))
            realtime_results.append(result)
            if result.ok and not result.skipped:
                outcome.realtime_actions.append(result.action)
            if not result.ok:
                outcome.failures.append(f"realtime:{result.message_id}:{result.error}")

        write_started = time.perf_counter()
        if os.getenv("FULL_WRITE", "").strip().lower() in {"1", "true", "yes", "on"}:
            write_result = await self.adapter.send_full_write_batch(batch, case=case)
        else:
            write_result = await self.adapter.send_write_batch(batch, case=case)
        outcome.write_latency_ms = round((time.perf_counter() - write_started) * 1000, 3)

        outcome.write_result_count = write_result.result_count
        outcome.write_input_count = getattr(write_result, "input_count", 0)
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
        outcome.metrics["avg_realtime_latency_ms"] = (
            round(sum(outcome.realtime_latency_ms) / len(outcome.realtime_latency_ms), 3)
            if outcome.realtime_latency_ms else None
        )
        outcome.metrics["write_latency_ms"] = outcome.write_latency_ms
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

    def _build_performance_metrics(self, outcomes: list[BatchOutcome], case_started: float) -> dict[str, Any]:
        realtime_latencies = [ms for outcome in outcomes for ms in outcome.realtime_latency_ms]
        write_latencies = [outcome.write_latency_ms for outcome in outcomes]
        total_realtime_messages = sum(len(outcome.realtime_latency_ms) for outcome in outcomes)
        total_write_input_messages = sum(outcome.write_input_count for outcome in outcomes)
        total_write_result_units = sum(outcome.write_result_count for outcome in outcomes)
        total_case_runtime_ms = round((time.perf_counter() - case_started) * 1000, 3)

        def pct(values: list[float], ratio: float) -> float | None:
            if not values:
                return None
            ordered = sorted(values)
            idx = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * ratio) - 1))
            return round(ordered[idx], 3)

        realtime_total_seconds = sum(realtime_latencies) / 1000 if realtime_latencies else 0.0
        write_total_seconds = sum(write_latencies) / 1000 if write_latencies else 0.0
        return {
            "case_total_runtime_ms": total_case_runtime_ms,
            "total_realtime_messages": total_realtime_messages,
            "total_write_batches": len(outcomes),
            "total_write_input_messages": total_write_input_messages,
            "total_write_result_units": total_write_result_units,
            "avg_realtime_latency_ms": round(sum(realtime_latencies) / len(realtime_latencies), 3) if realtime_latencies else None,
            "p95_realtime_latency_ms": pct(realtime_latencies, 0.95),
            "avg_write_latency_ms": round(sum(write_latencies) / len(write_latencies), 3) if write_latencies else None,
            "p95_write_latency_ms": pct(write_latencies, 0.95),
            "realtime_throughput_msgs_per_sec": round(total_realtime_messages / realtime_total_seconds, 3) if realtime_total_seconds else None,
            "write_input_throughput_msgs_per_sec": round(total_write_input_messages / write_total_seconds, 3) if write_total_seconds else None,
            "write_result_throughput_units_per_sec": round(total_write_result_units / write_total_seconds, 3) if write_total_seconds else None,
        }

    def _build_interference_metrics(self, case: dict[str, Any], outcomes: list[BatchOutcome]) -> dict[str, Any]:
        interference_tags = {"noise", "anti_noise", "anti_interference", "query", "schedule", "task", "multi_group", "topic_boundary", "parallel", "parallel_discussion", "classification", "cross_group_drift"}
        total_batches = 0
        passed_batches = 0
        difficult_batches = 0
        difficult_passed = 0
        multi_intent_batches = 0
        multi_intent_passed = 0
        near_miss_batches = 0
        near_miss_passed = 0
        action_check_batches = 0
        action_match_batches = 0
        write_check_batches = 0
        write_match_batches = 0
        ignore_rule_batches = 0
        ignore_rule_passed = 0
        tag_counts: dict[str, int] = {}

        for batch, outcome in zip(case.get("batches") or [], outcomes):
            tags = {str(tag) for tag in (batch.get("tags") or [])}
            relevant = tags & interference_tags
            if not relevant:
                continue
            total_batches += 1
            batch_passed = int(not outcome.failures)
            passed_batches += batch_passed
            for tag in relevant:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1

            is_multi_intent = len(relevant) >= 2
            is_difficult = len(batch.get("messages") or []) >= 5 or bool(
                tags & {"parallel", "parallel_discussion", "classification", "multi_group", "topic_boundary"}
            )
            is_near_miss = "near_miss" in tags or bool(tags & {"policy", "risk"})
            if is_multi_intent:
                multi_intent_batches += 1
                multi_intent_passed += batch_passed
            if is_difficult:
                difficult_batches += 1
                difficult_passed += batch_passed
            if is_near_miss:
                near_miss_batches += 1
                near_miss_passed += batch_passed

            expected = batch.get("expected") or {}
            expected_actions = expected.get("realtime_actions")
            if expected_actions is not None:
                action_check_batches += 1
                action_match_batches += int(outcome.realtime_actions == expected_actions)
            expected_write = expected.get("write_result_count")
            if expected_write is not None:
                write_check_batches += 1
                write_match_batches += int(outcome.write_result_count == expected_write)

            should_ignore = (batch.get("expected_write_result") or {}).get("should_ignore_message_ids") or []
            if should_ignore:
                ignore_rule_batches += 1
                ignore_rule_passed += int(any(check.get("name") == "should_ignore_message_ids" and check.get("passed") for check in outcome.checks))

        return {
            "batches": total_batches,
            "passed_batches": passed_batches,
            "batch_pass_rate": round(passed_batches / total_batches, 4) if total_batches else None,
            "difficult_batches": difficult_batches,
            "difficult_batch_pass_rate": round(difficult_passed / difficult_batches, 4) if difficult_batches else None,
            "multi_intent_batches": multi_intent_batches,
            "multi_intent_batch_pass_rate": round(multi_intent_passed / multi_intent_batches, 4) if multi_intent_batches else None,
            "near_miss_batches": near_miss_batches,
            "near_miss_batch_pass_rate": round(near_miss_passed / near_miss_batches, 4) if near_miss_batches else None,
            "realtime_action_check_batches": action_check_batches,
            "realtime_action_match_rate": round(action_match_batches / action_check_batches, 4) if action_check_batches else None,
            "write_count_check_batches": write_check_batches,
            "write_count_match_rate": round(write_match_batches / write_check_batches, 4) if write_check_batches else None,
            "ignore_rule_match_rate": round(ignore_rule_passed / ignore_rule_batches, 4) if ignore_rule_batches else None,
            "tag_counts": dict(sorted(tag_counts.items())),
        }

    def _build_conflict_metrics(self, case: dict[str, Any], outcomes: list[BatchOutcome]) -> dict[str, Any]:
        conflict_tags = {"refine", "refine_candidate", "supersede", "supersede_candidate", "conflict"}
        total_batches = 0
        passed_batches = 0
        hard_conflict_batches = 0
        hard_conflict_passed = 0
        memory_card_checks = 0
        memory_card_passed = 0
        relation_checks = 0
        relation_passed = 0
        forbidden_relation_checks = 0
        forbidden_relation_passed = 0
        relation_type_counts: dict[str, int] = {}
        relation_type_passed: dict[str, int] = {}
        relation_type_total: dict[str, int] = {}

        for batch, outcome in zip(case.get("batches") or [], outcomes):
            tags = {str(tag) for tag in (batch.get("tags") or [])}
            if not (tags & conflict_tags):
                continue
            total_batches += 1
            batch_passed = int(not outcome.failures)
            passed_batches += batch_passed
            is_hard_conflict = "conflict" in tags and ("supersede" in tags or "refine" in tags or "supersede_candidate" in tags)
            if is_hard_conflict:
                hard_conflict_batches += 1
                hard_conflict_passed += batch_passed

            expected_write = batch.get("expected_write_result") or {}
            relation_specs = expected_write.get("expected_relations") or []
            forbidden_specs = expected_write.get("forbidden_relations") or []
            for idx, spec in enumerate(relation_specs):
                relation_type = str(spec.get("relation_type") or "")
                relation_type_counts[relation_type] = relation_type_counts.get(relation_type, 0) + 1
                if self.evaluator.deep_eval_enabled:
                    relation_checks += 1
                    relation_type_total[relation_type] = relation_type_total.get(relation_type, 0) + 1
                    matched = any(
                        check.get("name") == f"expected_relations[{idx}]" and check.get("passed")
                        for check in outcome.checks
                    )
                    relation_passed += int(matched)
                    relation_type_passed[relation_type] = relation_type_passed.get(relation_type, 0) + int(matched)

            for idx, spec in enumerate(forbidden_specs):
                relation_type = str(spec.get("forbidden_relation_type") or "")
                relation_type_counts[f"forbidden:{relation_type}"] = relation_type_counts.get(f"forbidden:{relation_type}", 0) + 1
                if self.evaluator.deep_eval_enabled:
                    forbidden_relation_checks += 1
                    matched = any(
                        check.get("name") == f"forbidden_relations[{idx}]" and check.get("passed")
                        for check in outcome.checks
                    )
                    forbidden_relation_passed += int(matched)
                    relation_type_passed[f"forbidden:{relation_type}"] = relation_type_passed.get(f"forbidden:{relation_type}", 0) + int(matched)
                    relation_type_total[f"forbidden:{relation_type}"] = relation_type_total.get(f"forbidden:{relation_type}", 0) + 1

            memory_specs = expected_write.get("expected_memory_cards") or []
            for idx, _spec in enumerate(memory_specs):
                if self.evaluator.deep_eval_enabled:
                    memory_card_checks += 1
                    matched = any(
                        check.get("name") == f"expected_memory_cards[{idx}]" and check.get("passed")
                        for check in outcome.checks
                    )
                    memory_card_passed += int(matched)

        return {
            "mode": "full" if self.evaluator.deep_eval_enabled else "skipped",
            "batches": total_batches,
            "passed_batches": passed_batches,
            "batch_pass_rate": round(passed_batches / total_batches, 4) if total_batches else None,
            "hard_conflict_batches": hard_conflict_batches,
            "hard_conflict_batch_pass_rate": round(hard_conflict_passed / hard_conflict_batches, 4) if hard_conflict_batches else None,
            "memory_card_match_rate": round(memory_card_passed / memory_card_checks, 4) if memory_card_checks else None,
            "relation_match_rate": round(relation_passed / relation_checks, 4) if relation_checks else None,
            "forbidden_relation_match_rate": round(forbidden_relation_passed / forbidden_relation_checks, 4) if forbidden_relation_checks else None,
            "relation_type_counts": dict(sorted(relation_type_counts.items())),
            "relation_type_passed": dict(sorted(relation_type_passed.items())) if self.evaluator.deep_eval_enabled else {},
            "relation_type_match_rate": {
                relation_type: round(relation_type_passed.get(relation_type, 0) / total, 4)
                for relation_type, total in sorted(relation_type_total.items())
                if total
            } if self.evaluator.deep_eval_enabled else None,
        }

    def _build_write_quality_metrics(self, outcomes: list[BatchOutcome]) -> dict[str, Any]:
        memory_card_checks = 0
        memory_card_passed = 0
        relation_checks = 0
        relation_passed = 0
        topic_checks = 0
        topic_passed = 0
        ignore_checks = 0
        ignore_passed = 0
        optional_progress_checks = 0
        optional_progress_matched = 0

        for outcome in outcomes:
            for check in outcome.checks:
                name = str(check.get("name") or "")
                passed = bool(check.get("passed"))
                detail = str(check.get("detail") or "")
                if name.startswith("expected_memory_cards["):
                    memory_card_checks += 1
                    memory_card_passed += int(passed)
                elif name.startswith("expected_relations["):
                    relation_checks += 1
                    relation_passed += int(passed)
                elif name.startswith("expected_topic_summaries["):
                    topic_checks += 1
                    topic_passed += int(passed)
                elif name == "should_ignore_message_ids":
                    ignore_checks += 1
                    ignore_passed += int(passed)
                elif name.startswith("optional_progress_cards["):
                    optional_progress_checks += 1
                    optional_progress_matched += int(detail == "matched")

        return {
            "memory_card_checks": memory_card_checks,
            "memory_card_passed": memory_card_passed,
            "memory_card_match_rate": round(memory_card_passed / memory_card_checks, 4) if memory_card_checks else None,
            "relation_checks": relation_checks,
            "relation_passed": relation_passed,
            "relation_match_rate": round(relation_passed / relation_checks, 4) if relation_checks else None,
            "topic_checks": topic_checks,
            "topic_passed": topic_passed,
            "topic_match_rate": round(topic_passed / topic_checks, 4) if topic_checks else None,
            "ignore_checks": ignore_checks,
            "ignore_passed": ignore_passed,
            "ignore_match_rate": round(ignore_passed / ignore_checks, 4) if ignore_checks else None,
            "optional_progress_checks": optional_progress_checks,
            "optional_progress_matched": optional_progress_matched,
        }

    def _build_retrieval_quality_metrics(self, case: dict[str, Any], case_eval: Any) -> dict[str, Any]:
        if not self.evaluator.deep_eval_enabled:
            return {
                "mode": "skipped",
                "reason": "deep_eval_disabled",
                "final_memory_checks": 0,
                "final_memory_passed": 0,
                "final_memory_hit_rate": None,
                "evidence_checks": 0,
                "evidence_passed": 0,
                "evidence_hit_rate": None,
                "granularity_counts": {},
                "granularity_hit_rate": {},
                "recall_top1_hit_rate": None,
                "recall_top3_hit_rate": None,
            }
        final_specs = (case.get("expected") or {}).get("final_memory_checks") or []
        evidence_specs = (case.get("expected") or {}).get("evidence_checks") or []
        check_map = {check.name: check for check in case_eval.checks}
        granularity_counts: dict[str, int] = {}
        granularity_passed: dict[str, int] = {}

        final_hits = 0
        for idx, spec in enumerate(final_specs):
            name = f"final_memory_checks[{idx}]"
            target = str(spec.get("expected_granularity") or "memory_card")
            passed = bool(getattr(check_map.get(name), "passed", False))
            final_hits += int(passed)
            granularity_counts[target] = granularity_counts.get(target, 0) + 1
            granularity_passed[target] = granularity_passed.get(target, 0) + int(passed)

        evidence_hits = 0
        for idx, _spec in enumerate(evidence_specs):
            name = f"case_evidence_checks[{idx}]"
            evidence_hits += int(bool(getattr(check_map.get(name), "passed", False)))

        granularity_hit_rate = {
            target: round(granularity_passed.get(target, 0) / count, 4)
            for target, count in sorted(granularity_counts.items())
            if count
        }
        recall = case_eval.metrics.get("recall_metrics") or {}
        return {
            "final_memory_checks": len(final_specs),
            "final_memory_passed": final_hits,
            "final_memory_hit_rate": round(final_hits / len(final_specs), 4) if final_specs else None,
            "evidence_checks": len(evidence_specs),
            "evidence_passed": evidence_hits,
            "evidence_hit_rate": round(evidence_hits / len(evidence_specs), 4) if evidence_specs else None,
            "granularity_counts": dict(sorted(granularity_counts.items())),
            "granularity_hit_rate": granularity_hit_rate,
            "recall_top1_hit_rate": recall.get("top1_hit_rate"),
            "recall_top3_hit_rate": recall.get("top3_hit_rate"),
        }


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
