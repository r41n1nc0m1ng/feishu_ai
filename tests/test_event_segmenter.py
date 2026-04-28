"""
event_segmenter 单元测试 — 纯逻辑，无外部依赖。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from datetime import datetime, timedelta, timezone

from memory.schemas import EvidenceMessage, FetchBatch
from preprocessor.event_segmenter import MAX_BLOCK_MESSAGES, BLOCK_GAP_SECONDS, segment

BASE = datetime(2026, 4, 28, 10, 0, 0, tzinfo=timezone.utc)


def _msg(offset_seconds: int, text: str = "内容") -> EvidenceMessage:
    return EvidenceMessage(
        message_id=f"msg_{offset_seconds}",
        sender_id="u1",
        sender_name="A",
        timestamp=BASE + timedelta(seconds=offset_seconds),
        text=text,
    )


def _batch(messages: list) -> FetchBatch:
    return FetchBatch(
        chat_id="oc_test",
        fetch_start=messages[0].timestamp if messages else BASE,
        fetch_end=messages[-1].timestamp if messages else BASE,
        messages=messages,
    )


class EventSegmenterTests(unittest.TestCase):

    def test_empty_batch_returns_no_blocks(self):
        result = segment(FetchBatch(chat_id="c1", fetch_start=BASE, fetch_end=BASE, messages=[]))
        self.assertEqual(result, [])

    def test_single_message_returns_one_block(self):
        blocks = segment(_batch([_msg(0)]))
        self.assertEqual(len(blocks), 1)
        self.assertEqual(len(blocks[0].messages), 1)

    def test_messages_within_gap_stay_in_one_block(self):
        msgs = [_msg(0), _msg(60), _msg(120)]   # 2 分钟内，远小于 BLOCK_GAP_SECONDS
        blocks = segment(_batch(msgs))
        self.assertEqual(len(blocks), 1)
        self.assertEqual(len(blocks[0].messages), 3)

    def test_gap_exceeding_threshold_splits_blocks(self):
        gap = BLOCK_GAP_SECONDS + 1
        msgs = [_msg(0, "第一段"), _msg(gap, "第二段")]
        blocks = segment(_batch(msgs))
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0].messages[0].text, "第一段")
        self.assertEqual(blocks[1].messages[0].text, "第二段")

    def test_max_messages_forces_split(self):
        msgs = [_msg(i * 10) for i in range(MAX_BLOCK_MESSAGES + 1)]
        blocks = segment(_batch(msgs))
        self.assertGreater(len(blocks), 1)
        # 每个块不超过 MAX_BLOCK_MESSAGES 条
        for b in blocks:
            self.assertLessEqual(len(b.messages), MAX_BLOCK_MESSAGES)

    def test_block_timestamps_are_correct(self):
        msgs = [_msg(0), _msg(100), _msg(200)]
        blocks = segment(_batch(msgs))
        self.assertEqual(blocks[0].start_time, msgs[0].timestamp)
        self.assertEqual(blocks[0].end_time, msgs[-1].timestamp)

    def test_messages_sorted_by_timestamp(self):
        # 乱序输入，应按时间排序后切分
        msgs = [_msg(200), _msg(0), _msg(100)]
        blocks = segment(_batch(msgs))
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].messages[0].timestamp, BASE)

    def test_multiple_gaps_produce_multiple_blocks(self):
        gap = BLOCK_GAP_SECONDS + 10
        msgs = [_msg(0), _msg(gap), _msg(gap * 2)]
        blocks = segment(_batch(msgs))
        self.assertEqual(len(blocks), 3)


if __name__ == "__main__":
    unittest.main()
