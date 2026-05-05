from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from benchmark_v2.dual_channel_runner import OfflineReplayRunner
from benchmark_v2.evaluator import BenchmarkEvaluator, EvaluatorSummary, CheckResult
from benchmark_v2.reporting import render_console_summary
from memory import store
from memory.schemas import CardStatus, MemoryCard, MemoryRelation, MemoryRelationType, EvidenceBlock, EvidenceMessage
from datetime import datetime, timezone


class BenchmarkV2RunnerTests(unittest.TestCase):
    def test_filtered_run_skips_case_level_eval(self):
        case = {
            "schema_version": "dual_channel_benchmark_v2",
            "case_id": "mini_case",
            "description": "mini",
            "chat_id": "oc_test",
            "replay_policy": {"mode": "dual_channel_batch_replay"},
            "chat_profiles": {"oc_test": {"theme": "测试群"}},
            "batches": [
                {
                    "batch_id": "batch_001",
                    "scenario": "refine",
                    "tags": ["refine"],
                    "fetch_time": "2026-05-05 10:00",
                    "expected_brief": "mini",
                    "messages": [
                        {
                            "message_id": "m1",
                            "msg_type": "text",
                            "create_time": "2026-05-05 10:00",
                            "sender": {"id": "ou_1", "sender_type": "user"},
                            "content": json.dumps({"text": "补充规则"}, ensure_ascii=False),
                        }
                    ],
                    "expected": {
                        "realtime_actions": ["noop"],
                        "write_result_count": 1,
                    },
                }
            ],
            "expected": {
                "final_memory_checks": [
                    {
                        "query": "整体方案",
                        "expected_keywords": ["永远不会命中"],
                    }
                ]
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "case.json"
            path.write_text(json.dumps(case, ensure_ascii=False), encoding="utf-8")

            async def fake_send_realtime_message(raw_msg, *, case, batch):
                from benchmark.replay_adapter import ReplayResult

                return ReplayResult(
                    channel="realtime",
                    ok=True,
                    skipped=False,
                    message_id=raw_msg["message_id"],
                    batch_id=batch["batch_id"],
                    action="noop",
                )

            async def fake_send_write_batch(batch, *, case):
                from benchmark.replay_adapter import ReplayResult

                return ReplayResult(
                    channel="write",
                    ok=True,
                    batch_id=batch["batch_id"],
                    result_count=1,
                    input_count=1,
                )

            runner = OfflineReplayRunner()
            runner.adapter.send_realtime_message = fake_send_realtime_message
            runner.adapter.send_write_batch = fake_send_write_batch

            with patch.dict(os.environ, {"BENCHMARK_V2_DEEP_EVAL": "1"}, clear=False):
                runner.evaluator.deep_eval_enabled = True
                summary = asyncio.run(runner.run_case(path, include_tags={"refine"}))

        self.assertTrue(summary["overall_success"])
        self.assertEqual(summary["case_eval_mode"], "skipped_for_filtered_run")
        self.assertEqual(summary["case_checks"][0]["detail"], "skipped:filtered run")

    def test_performance_metrics_and_p95_are_reported(self):
        runner = OfflineReplayRunner()
        outcomes = []
        for idx, (rt, wt, units) in enumerate(
            [
                ([10.0], 100.0, 1),
                ([20.0], 200.0, 2),
                ([30.0], 300.0, 3),
                ([40.0], 400.0, 4),
            ],
            start=1,
        ):
            from benchmark_v2.dual_channel_runner import BatchOutcome

            outcome = BatchOutcome(batch_id=f"b{idx}")
            outcome.realtime_latency_ms = rt
            outcome.write_latency_ms = wt
            outcome.write_result_count = units
            outcome.write_input_count = units
            outcomes.append(outcome)

        with patch("benchmark_v2.dual_channel_runner.time.perf_counter", return_value=2.0):
            perf = runner._build_performance_metrics(outcomes, case_started=1.0)

        self.assertEqual(perf["case_total_runtime_ms"], 1000.0)
        self.assertEqual(perf["total_realtime_messages"], 4)
        self.assertEqual(perf["total_write_batches"], 4)
        self.assertEqual(perf["total_write_result_units"], 10)
        self.assertEqual(perf["avg_realtime_latency_ms"], 25.0)
        self.assertEqual(perf["p95_realtime_latency_ms"], 40.0)
        self.assertEqual(perf["avg_write_latency_ms"], 250.0)
        self.assertEqual(perf["p95_write_latency_ms"], 400.0)
        self.assertEqual(perf["realtime_throughput_msgs_per_sec"], 40.0)
        self.assertEqual(perf["total_write_input_messages"], 10)
        self.assertEqual(perf["total_write_result_units"], 10)
        self.assertEqual(perf["write_input_throughput_msgs_per_sec"], 10.0)
        self.assertEqual(perf["write_result_throughput_units_per_sec"], 10.0)

    def test_interference_metrics_use_checked_batches_only(self):
        runner = OfflineReplayRunner()
        case = {
            "batches": [
                {
                    "tags": ["noise"],
                    "expected": {"realtime_actions": ["noop"], "write_result_count": 1},
                },
                {
                    "tags": ["noise"],
                    "expected": {},
                },
            ]
        }

        from benchmark_v2.dual_channel_runner import BatchOutcome

        outcome1 = BatchOutcome(batch_id="b1")
        outcome1.realtime_actions = ["noop"]
        outcome1.write_result_count = 1
        outcome1.write_input_count = 1
        outcome2 = BatchOutcome(batch_id="b2")
        metrics = runner._build_interference_metrics(case, [outcome1, outcome2])

        self.assertEqual(metrics["realtime_action_check_batches"], 1)
        self.assertEqual(metrics["write_count_check_batches"], 1)
        self.assertEqual(metrics["realtime_action_match_rate"], 1.0)
        self.assertEqual(metrics["write_count_match_rate"], 1.0)

    def test_recall_metrics_are_built_from_final_memory_checks(self):
        evaluator = BenchmarkEvaluator()
        chat_id = "oc_recall_test"

        with store._conn() as conn:
            conn.execute("DELETE FROM memory_cards WHERE chat_id=?", (chat_id,))
            conn.execute("DELETE FROM memory_relations WHERE chat_id=?", (chat_id,))
            conn.execute("DELETE FROM topic_summaries WHERE chat_id=?", (chat_id,))
            conn.execute("DELETE FROM evidence_blocks WHERE chat_id=?", (chat_id,))
            conn.execute("DELETE FROM chat_spaces WHERE chat_id=?", (chat_id,))

        try:
            store.save_memory_card(
                MemoryCard(
                    chat_id=chat_id,
                    decision_object="发布方案",
                    title="发布窗口",
                    decision="本周五晚上八点灰度开放",
                    reason="先控量观察",
                    status=CardStatus.ACTIVE,
                )
            )
            store.save_memory_card(
                MemoryCard(
                    chat_id=chat_id,
                    decision_object="直播预案",
                    title="备用方案",
                    decision="直播挂了就切备用链接",
                    reason="保证路演不中断",
                    status=CardStatus.ACTIVE,
                )
            )

            metrics = evaluator._build_recall_metrics(
                {"chat_id": chat_id},
                [
                    {
                        "query": "现在发布怎么安排",
                        "expected_granularity": "memory_card",
                        "expected_keywords": ["灰度开放"],
                    },
                    {
                        "query": "直播出问题怎么办",
                        "expected_granularity": "memory_card",
                        "expected_keywords": ["备用链接"],
                    },
                ],
            )
        finally:
            with store._conn() as conn:
                conn.execute("DELETE FROM memory_cards WHERE chat_id=?", (chat_id,))
                conn.execute("DELETE FROM memory_relations WHERE chat_id=?", (chat_id,))
                conn.execute("DELETE FROM topic_summaries WHERE chat_id=?", (chat_id,))
                conn.execute("DELETE FROM evidence_blocks WHERE chat_id=?", (chat_id,))
                conn.execute("DELETE FROM chat_spaces WHERE chat_id=?", (chat_id,))

        self.assertEqual(metrics["queries"], 2)
        self.assertEqual(metrics["top1_hits"], 2)
        self.assertEqual(metrics["top3_hits"], 2)
        self.assertEqual(metrics["top5_hits"], 2)
        self.assertEqual(metrics["top1_hit_rate"], 1.0)
        self.assertEqual(metrics["top3_hit_rate"], 1.0)
        self.assertEqual(metrics["top5_hit_rate"], 1.0)
        self.assertEqual(metrics["mean_first_hit_rank"], 1.0)
        self.assertEqual(metrics["median_first_hit_rank"], 1)
        self.assertEqual(len(metrics["details"]), 2)
        self.assertTrue(all(detail["matched_rank"] == 1 for detail in metrics["details"]))
        self.assertTrue(all(detail["retrieval_latency_ms"] is not None for detail in metrics["details"]))

    def test_dimension_summary_reports_difficulty_mix(self):
        summary = {
            "batch_results": [
                {"chat_id": "c1", "tags": ["parallel", "classification", "noise", "query"], "message_count": 5, "failures": []},
                {"chat_id": "c1", "tags": ["query"], "message_count": 2, "failures": ["x"]},
            ]
        }
        from benchmark_v2.reporting import build_dimension_summary

        dims = build_dimension_summary(summary)
        self.assertEqual(dims["difficulty_distribution"]["multi_tag"], 1)
        self.assertEqual(dims["difficulty_distribution"]["long_batch"], 1)
        self.assertEqual(dims["difficulty_distribution"]["hard_mix"], 1)

    def test_special_metrics_are_reported_on_console_summary(self):
        summary = {
            "case_id": "case",
            "total_batches": 2,
            "passed_batches": 2,
            "failed_batches": 0,
            "total_checks": 4,
            "failed_checks": 0,
            "overall_success": True,
            "performance": {"case_total_runtime_ms": 1, "avg_realtime_latency_ms": 2, "avg_write_latency_ms": 3},
            "recall_metrics": {"top1_hit_rate": 1.0, "top3_hit_rate": 1.0},
            "interference_metrics": {"batch_pass_rate": 1.0, "realtime_action_match_rate": 1.0},
            "conflict_metrics": {"batch_pass_rate": 1.0, "relation_match_rate": 1.0, "forbidden_relation_match_rate": 1.0},
            "write_quality_metrics": {"memory_card_match_rate": 0.5, "relation_match_rate": 0.25, "topic_match_rate": 1.0},
            "retrieval_quality_metrics": {"final_memory_hit_rate": 0.5, "evidence_hit_rate": 1.0},
            "batch_results": [
                {
                    "chat_id": "c1",
                    "tags": ["noise"],
                    "realtime_actions": ["noop"],
                    "failures": [],
                }
            ],
        }
        rendered = render_console_summary(summary)
        self.assertIn("interference pass/match: 1.0/1.0", rendered)
        self.assertIn("conflict pass/match/guard: 1.0/1.0/1.0", rendered)
        self.assertIn("write quality card/relation/topic: 0.5/0.25/1.0", rendered)
        self.assertIn("retrieval quality final/evidence: 0.5/1.0", rendered)

    def test_full_run_summary_contains_quality_metrics(self):
        case = {
            "schema_version": "dual_channel_benchmark_v2",
            "case_id": "mini_case_full",
            "description": "mini",
            "chat_id": "oc_test",
            "replay_policy": {"mode": "dual_channel_batch_replay"},
            "chat_profiles": {"oc_test": {"theme": "测试群"}},
            "batches": [
                {
                    "batch_id": "batch_001",
                    "scenario": "conflict",
                    "tags": ["query", "conflict"],
                    "fetch_time": "2026-05-05 10:00",
                    "expected_brief": "mini",
                    "messages": [
                        {
                            "message_id": "m1",
                            "msg_type": "text",
                            "create_time": "2026-05-05 10:00",
                            "sender": {"id": "ou_1", "sender_type": "user"},
                            "content": json.dumps({"text": "@机器人 之前怎么定的"}, ensure_ascii=False),
                        }
                    ],
                    "expected": {
                        "realtime_actions": ["query"],
                        "write_result_count": 0,
                    },
                    "expected_write_result": {
                        "expected_memory_cards": [{"expected_keywords": ["a"]}],
                        "expected_relations": [{"relation_type": "supersedes", "new_expected_keywords": ["b"]}],
                    },
                }
            ],
            "expected": {
                "final_memory_checks": [
                    {
                        "query": "整体方案",
                        "expected_granularity": "memory_card",
                        "expected_keywords": ["a"],
                    }
                ],
                "evidence_checks": [],
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "case.json"
            path.write_text(json.dumps(case, ensure_ascii=False), encoding="utf-8")

            async def fake_send_realtime_message(raw_msg, *, case, batch):
                from benchmark.replay_adapter import ReplayResult

                return ReplayResult(
                    channel="realtime",
                    ok=True,
                    skipped=False,
                    message_id=raw_msg["message_id"],
                    batch_id=batch["batch_id"],
                    action="query",
                )

            async def fake_send_write_batch(batch, *, case):
                from benchmark.replay_adapter import ReplayResult

                return ReplayResult(
                    channel="write",
                    ok=True,
                    batch_id=batch["batch_id"],
                    result_count=0,
                    input_count=0,
                    ignored_message_ids=[],
                )

            runner = OfflineReplayRunner()
            runner.adapter.send_realtime_message = fake_send_realtime_message
            runner.adapter.send_write_batch = fake_send_write_batch
            runner.evaluator.evaluate_case = lambda _case: EvaluatorSummary(
                passed=False,
                checks=[CheckResult(name="final_memory_checks[0]", passed=False, detail="整体方案")],
                metrics={
                    "total_checks": 1,
                    "passed_checks": 0,
                    "failed_checks": 1,
                    "recall_metrics": {"top1_hit_rate": 0.0, "top3_hit_rate": 0.0},
                },
            )

            summary = asyncio.run(runner.run_case(path))

        self.assertIn("write_quality_metrics", summary)
        self.assertIn("retrieval_quality_metrics", summary)
        self.assertIn("interference_metrics", summary)
        self.assertIn("conflict_metrics", summary)
        self.assertIn("memory_card_checks", summary["write_quality_metrics"])
        self.assertIn("relation_checks", summary["write_quality_metrics"])
        self.assertEqual(summary["retrieval_quality_metrics"]["mode"], "skipped")
        self.assertIsNone(summary["retrieval_quality_metrics"]["final_memory_hit_rate"])

    def test_forbidden_relation_type_is_checked(self):
        evaluator = BenchmarkEvaluator()
        chat_id = "oc_relation_guard"

        with store._conn() as conn:
            conn.execute("DELETE FROM memory_cards WHERE chat_id=?", (chat_id,))
            conn.execute("DELETE FROM memory_relations WHERE chat_id=?", (chat_id,))

        try:
            old_card = MemoryCard(
                chat_id=chat_id,
                decision_object="社区",
                title="旧边界",
                decision="不做公开社区",
                reason="先聚焦个人场景",
                status=CardStatus.ACTIVE,
            )
            new_card = MemoryCard(
                chat_id=chat_id,
                decision_object="分享图",
                title="分享图",
                decision="支持用户主动导出分享图",
                reason="方便外部分享",
                status=CardStatus.ACTIVE,
            )
            store.save_memory_card(old_card)
            store.save_memory_card(new_card)
            store.save_relation(
                MemoryRelation(
                    chat_id=chat_id,
                    source_id=new_card.memory_id,
                    target_id=old_card.memory_id,
                    relation_type=MemoryRelationType.RELATED_TO,
                )
            )

            checks = evaluator._check_relation_specs(
                {"chat_id": chat_id},
                [
                    {
                        "relation_type": "related_to",
                        "old_expected_keywords": ["不做公开社区"],
                        "new_expected_keywords": ["分享图"],
                        "forbidden_relation_type": "supersedes",
                    }
                ],
                prefix="forbidden_relations",
            )
        finally:
            with store._conn() as conn:
                conn.execute("DELETE FROM memory_relations WHERE chat_id=?", (chat_id,))
                conn.execute("DELETE FROM memory_cards WHERE chat_id=?", (chat_id,))

        self.assertEqual(len(checks), 1)
        self.assertTrue(checks[0].passed)

    def test_expected_source_message_ids_are_checked(self):
        evaluator = BenchmarkEvaluator()
        chat_id = "oc_evidence_guard"
        ts = datetime.now(timezone.utc)

        with store._conn() as conn:
            conn.execute("DELETE FROM evidence_blocks WHERE chat_id=?", (chat_id,))
            conn.execute("DELETE FROM memory_cards WHERE chat_id=?", (chat_id,))

        try:
            block = EvidenceBlock(
                chat_id=chat_id,
                start_time=ts,
                end_time=ts,
                messages=[
                    EvidenceMessage(message_id="m1", sender_id="u1", timestamp=ts, text="不要写完全替代人工筛选"),
                    EvidenceMessage(message_id="m2", sender_id="u2", timestamp=ts, text="统一说辅助初筛"),
                ],
            )
            store.save_evidence_block(block)
            card = MemoryCard(
                chat_id=chat_id,
                decision_object="公告措辞",
                title="公告口径",
                decision="统一说辅助初筛",
                reason="避免过度承诺",
                status=CardStatus.ACTIVE,
                source_block_ids=[block.block_id],
            )
            store.save_memory_card(card)

            checks = evaluator._check_evidence_specs(
                {"chat_id": chat_id},
                [
                    {
                        "expected_source_message_ids": ["m1", "m2"],
                        "expected_keywords": ["辅助初筛"],
                    }
                ],
                prefix="case_evidence_checks",
            )
        finally:
            with store._conn() as conn:
                conn.execute("DELETE FROM memory_cards WHERE chat_id=?", (chat_id,))
                conn.execute("DELETE FROM evidence_blocks WHERE chat_id=?", (chat_id,))

        self.assertEqual(len(checks), 1)
        self.assertTrue(checks[0].passed)


if __name__ == "__main__":
    unittest.main()
