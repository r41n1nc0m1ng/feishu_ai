import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from unittest.mock import AsyncMock, patch

from benchmark.full_demo_dual_channel_test import DualChannelReplayRunner


class BenchmarkReplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_write_mode_uses_full_write_entry(self):
        case = {
            "chat_id": "c1",
            "batches": [
                {
                    "batch_id": "b1",
                    "messages": [
                        {
                            "message_id": "m1",
                            "msg_type": "text",
                            "create_time": "2026-05-03 10:00:00",
                            "sender": {"id": "u1", "sender_type": "user"},
                            "content": "{\"text\": \"你好\"}",
                        }
                    ],
                }
            ],
        }
        batch = case["batches"][0]
        runner = DualChannelReplayRunner()

        with patch("benchmark.full_demo_dual_channel_test.load_benchmark_case", return_value=case), \
             patch.object(runner.adapter, "send_realtime_message", new=AsyncMock(return_value=type("R", (), {"skipped": True, "ok": True, "message_id": "m1", "action": ""})())), \
             patch.object(runner.adapter, "send_full_write_batch", new=AsyncMock(return_value=type("W", (), {"ok": True, "input_count": 1, "result_count": 1, "ignored_message_ids": []})())), \
             patch.dict(os.environ, {"FULL_WRITE": "1"}):
            summary = await runner.run_case("dummy.json")

        self.assertTrue(summary["overall_success"])


if __name__ == "__main__":
    unittest.main()
