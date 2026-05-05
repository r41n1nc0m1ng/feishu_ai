import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from benchmark.replay_adapter import DualChannelReplayAdapter
import benchmark.special_case.special_case_replay as replay


def _msg(message_id: str, create_time: str, text: str = "text"):
    return {
        "message_id": message_id,
        "msg_type": "text",
        "create_time": create_time,
        "content": json.dumps({"text": text}, ensure_ascii=False),
        "sender": {"id": "ou_1", "sender_type": "user"},
        "deleted": False,
        "updated": False,
    }


class SplitTimeBatchesTests(unittest.TestCase):
    def test_same_hour_goes_to_one_batch(self):
        messages = [
            _msg("m1", "2026-04-21 09:00"),
            _msg("m2", "2026-04-21 09:59"),
        ]

        batches = replay.split_time_batches(messages, batch_hours=1, overlap=3)

        self.assertEqual(len(batches), 1)
        self.assertEqual([m["message_id"] for m in batches[0].messages], ["m1", "m2"])

    def test_cross_hour_generates_batches_and_skips_empty_windows(self):
        messages = [
            _msg("m1", "2026-04-21 09:00"),
            _msg("m2", "2026-04-21 11:05"),
        ]

        batches = replay.split_time_batches(messages, batch_hours=1, overlap=0)

        self.assertEqual(len(batches), 2)
        self.assertEqual([m["message_id"] for m in batches[0].messages], ["m1"])
        self.assertEqual([m["message_id"] for m in batches[1].messages], ["m2"])

    def test_overlap_prepends_previous_non_empty_window_tail(self):
        messages = [
            _msg("m1", "2026-04-21 09:00"),
            _msg("m2", "2026-04-21 09:10"),
            _msg("m3", "2026-04-21 09:20"),
            _msg("m4", "2026-04-21 09:30"),
            _msg("m5", "2026-04-21 10:01"),
        ]

        batches = replay.split_time_batches(messages, batch_hours=1, overlap=3)

        self.assertEqual(len(batches), 2)
        self.assertEqual(
            [m["message_id"] for m in batches[1].messages],
            ["m2", "m3", "m4", "m5"],
        )

    def test_zero_overlap_does_not_prepend(self):
        messages = [
            _msg("m1", "2026-04-21 09:00"),
            _msg("m2", "2026-04-21 10:00"),
        ]

        batches = replay.split_time_batches(messages, batch_hours=1, overlap=0)

        self.assertEqual([m["message_id"] for m in batches[1].messages], ["m2"])


class SpecialCaseReplayFlowTests(unittest.IsolatedAsyncioTestCase):
    def _write_case_file(self, directory: Path) -> Path:
        case = {
            "schema_version": "test_single_stream_v1",
            "case_id": "case_001",
            "description": "minimal case",
            "chat_id": "oc_case_001",
            "test_type": "anti_noise",
            "replay_policy": {"mode": "single_stream_with_final_queries"},
            "messages": [_msg("m1", "2026-04-21 09:00", "决定只做教材")],
            "final_query_messages": [_msg("q1", "2026-04-21 10:00", "@机器人 之前怎么定的")],
            "expected": {
                "final_memory_checks": [
                    {
                        "query_message_id": "q1",
                        "query": "之前怎么定的",
                        "expected_answer": "只做教材",
                        "expected_keywords": ["教材"],
                        "forbidden_keywords": ["衣服"],
                    }
                ]
            },
        }
        path = directory / "case.json"
        path.write_text(json.dumps(case, ensure_ascii=False), encoding="utf-8")
        return path

    async def test_run_case_assembles_report_with_actual_and_expected(self):
        with tempfile.TemporaryDirectory() as tmp:
            case_path = self._write_case_file(Path(tmp))

            with patch.object(replay, "reset_test_data", new=AsyncMock()), \
                 patch.object(replay, "ensure_graphiti_ready", new=AsyncMock()), \
                 patch.object(replay, "write_case_history", new=AsyncMock(return_value=[{"batch_id": "batch_001"}])), \
                 patch.object(replay, "run_queries", new=AsyncMock(return_value=[{
                     "query_message_id": "q1",
                     "query": "@机器人 之前怎么定的",
                     "action": "query",
                     "actual_reply": "根据当前群记忆：只做教材",
                     "expected_answer": "只做教材",
                     "keyword_check": {"passed": True},
                     "llm_judge": {},
                 }])), \
                 patch.object(replay, "sqlite_summary", return_value={
                     "db_path": "memory_store.db",
                     "memory_card_count": 1,
                     "evidence_block_count": 1,
                     "memory_relation_count": 0,
                     "topic_summary_count": 0,
                     "memory_cards": [],
                     "memory_relations": [],
                 }), \
                 patch.object(replay, "relation_diagnostics", return_value=[]):
                report = await replay.run_case(
                    case_path,
                    batch_hours=1,
                    overlap=3,
                    llm_judge=False,
                    reset=True,
                    retriever_backend="graphiti",
                    llm_concurrency=1,
                    batch_concurrency=1,
                )

        self.assertEqual(report["case_id"], "case_001")
        self.assertEqual(report["retriever_backend"], "graphiti")
        self.assertEqual(report["llm_concurrency"], 1)
        self.assertEqual(report["batch_concurrency"], 1)
        self.assertEqual(report["query_results"][0]["actual_reply"], "根据当前群记忆：只做教材")
        self.assertEqual(report["query_results"][0]["expected_answer"], "只做教材")

    async def test_sqlite_keyword_backend_skips_graphiti_reset_and_init(self):
        with tempfile.TemporaryDirectory() as tmp:
            case_path = self._write_case_file(Path(tmp))

            with patch.object(replay, "reset_test_data_sqlite_only", new=AsyncMock()) as reset_sqlite, \
                 patch.object(replay, "reset_test_data", new=AsyncMock()) as reset_graphiti, \
                 patch.object(replay, "ensure_graphiti_ready", new=AsyncMock()) as init_graphiti, \
                 patch.object(replay, "write_case_history", new=AsyncMock(return_value=[])), \
                 patch.object(replay, "run_queries", new=AsyncMock(return_value=[])), \
                 patch.object(replay, "sqlite_summary", return_value={
                     "db_path": "memory_store.db",
                     "memory_card_count": 0,
                     "evidence_block_count": 0,
                     "memory_relation_count": 0,
                     "topic_summary_count": 0,
                     "memory_cards": [],
                     "memory_relations": [],
                 }), \
                 patch.object(replay, "relation_diagnostics", return_value=[]):
                report = await replay.run_case(
                    case_path,
                    batch_hours=1,
                    overlap=3,
                    llm_judge=False,
                    reset=True,
                    retriever_backend="sqlite-keyword",
                    llm_concurrency=1,
                    batch_concurrency=1,
                )

        reset_sqlite.assert_awaited_once()
        reset_graphiti.assert_not_awaited()
        init_graphiti.assert_not_awaited()
        self.assertEqual(report["retriever_backend"], "sqlite-keyword")
        self.assertFalse(report["graphiti_initialized"])

    async def test_sqlite_keyword_retriever_ranks_memory_cards(self):
        card = SimpleNamespace(
            chat_id="c1",
            status=replay.CardStatus.ACTIVE,
            decision_object="P1 二手市场范围",
            title="P1 只做教材资料",
            decision="P1 不做全品类二手市场，只做教材和考试资料",
            reason="全品类审核和纠纷复杂",
            memory_type=SimpleNamespace(value="decision"),
        )
        other = SimpleNamespace(
            chat_id="c1",
            status=replay.CardStatus.ACTIVE,
            decision_object="无关议题",
            title="午饭",
            decision="今天吃面",
            reason="",
            memory_type=SimpleNamespace(value="decision"),
        )
        with patch.object(replay.store, "get_cards_for_chat", return_value=[other, card]):
            results = await replay.SQLiteKeywordRetriever().retrieve(
                "c1",
                "为什么 P1 不做全品类二手市场",
                limit=1,
            )

        self.assertEqual(results[0].decision, card.decision)

    async def test_write_fetch_batch_can_process_blocks_concurrently_when_selected(self):
        blocks = [
            SimpleNamespace(chat_id="c1", block_id=f"b{i}")
            for i in range(3)
        ]

        class FakeEvidenceStore:
            async def save(self, block):
                return None

        class FakeCardGenerator:
            async def generate(self, block):
                await asyncio.sleep(0.05)
                return SimpleNamespace(memory_id=block.block_id)

        with patch.object(replay, "segment_async", new=AsyncMock(return_value=blocks)), \
             patch.object(replay, "EvidenceStore", return_value=FakeEvidenceStore()), \
             patch.object(replay, "CardGenerator", side_effect=lambda: FakeCardGenerator()), \
             patch.object(replay.TopicManager, "rebuild_topics", new=AsyncMock()):
            start = time.perf_counter()
            result = await replay.write_fetch_batch(
                SimpleNamespace(chat_id="c1"),
                llm_concurrency=3,
            )
            elapsed = time.perf_counter() - start

        self.assertEqual(result["card_count"], 3)
        self.assertLess(elapsed, 0.12)

    async def test_write_case_history_can_process_batches_concurrently_when_selected(self):
        messages = [
            _msg("m1", "2026-04-21 09:00"),
            _msg("m2", "2026-04-21 10:05"),
            _msg("m3", "2026-04-21 11:10"),
        ]
        case = {"chat_id": "c1", "messages": messages}
        adapter = DualChannelReplayAdapter()

        async def slow_write(fetch_batch, *, llm_concurrency=1):
            await asyncio.sleep(0.05)
            return {"block_count": 1, "card_count": 1}

        with patch.object(replay.store, "save_chat_space"), \
             patch.object(replay, "write_fetch_batch", side_effect=slow_write):
            start = time.perf_counter()
            reports = await replay.write_case_history(
                case,
                batch_hours=1,
                overlap=0,
                adapter=adapter,
                llm_concurrency=1,
                batch_concurrency=3,
            )
            elapsed = time.perf_counter() - start

        self.assertEqual([item["batch_id"] for item in reports], ["batch_001", "batch_002", "batch_003"])
        self.assertTrue(all(item["batch_concurrency"] == 3 for item in reports))
        self.assertLess(elapsed, 0.12)

    async def test_llm_judge_disabled_does_not_call_model(self):
        case = {
            "chat_id": "c1",
            "final_query_messages": [_msg("q1", "2026-04-21 10:00", "@机器人 之前怎么定的")],
            "expected": {"final_memory_checks": [{"query_message_id": "q1", "expected_answer": "A"}]},
        }

        class FakeHandler:
            async def handle_query_message(self, message):
                return SimpleNamespace(action="query")

        adapter = DualChannelReplayAdapter()
        with patch.object(replay, "dispatch_message", new=AsyncMock(return_value=SimpleNamespace(action="query"))), \
             patch.object(replay.RealtimeQueryHandler, "__init__", return_value=None), \
             patch.object(replay.RealtimeActionHandler, "__init__", return_value=None), \
             patch.object(replay, "judge_answer", new=AsyncMock()) as judge:
            results = await replay.run_queries(case, adapter=adapter, llm_judge=False)

        self.assertEqual(results[0]["llm_judge"], {})
        judge.assert_not_awaited()

    async def test_llm_judge_parses_single_digit_scores(self):
        with patch.object(replay, "_call_openai_judge", new=AsyncMock(return_value="2")):
            result = await replay.judge_answer(
                query="Q",
                expected_answer="A",
                actual_reply="A",
            )

        self.assertEqual(result, {"score": 2})

    async def test_llm_judge_records_parse_errors(self):
        with patch.object(replay, "_call_openai_judge", new=AsyncMock(return_value="not a score")):
            result = await replay.judge_answer(
                query="Q",
                expected_answer="A",
                actual_reply="B",
            )

        self.assertIn("judge_error", result)


if __name__ == "__main__":
    unittest.main()
