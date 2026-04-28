"""
retriever 批处理侧扩展测试 — 验证 expand_evidence 和 get_card_by_id。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from datetime import datetime, timezone

import memory.evidence_store as store_module
import memory.card_generator as gen_module
from memory.retriever import MemoryRetriever
from memory.schemas import (
    CardStatus, EvidenceBlock, EvidenceMessage, MemoryCard,
)

BASE = datetime(2026, 4, 28, 10, 0, tzinfo=timezone.utc)


def _make_block(chat_id: str = "oc_test") -> EvidenceBlock:
    msgs = [EvidenceMessage(message_id="m1", sender_id="u1", sender_name="A", timestamp=BASE, text="原话")]
    return EvidenceBlock(chat_id=chat_id, start_time=BASE, end_time=BASE, messages=msgs)


def _make_card(chat_id: str = "oc_test") -> MemoryCard:
    return MemoryCard(
        chat_id=chat_id,
        decision_object="测试议题",
        title="测试标题",
        decision="测试决策",
        reason="测试理由",
    )


class RetrieverBatchTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        store_module._block_cache.clear()
        gen_module._card_cache.clear()
        gen_module._cards_by_object.clear()

    # ── expand_evidence ────────────────────────────────────────────────────────

    async def test_expand_evidence_returns_cached_block(self):
        block = _make_block()
        store_module._block_cache[block.block_id] = block

        result = await MemoryRetriever().expand_evidence(block.block_id)
        self.assertEqual(result, block)
        self.assertEqual(result.messages[0].text, "原话")

    async def test_expand_evidence_returns_none_for_unknown(self):
        result = await MemoryRetriever().expand_evidence("nonexistent_block")
        self.assertIsNone(result)

    async def test_expand_evidence_returns_messages_intact(self):
        block = _make_block()
        block.messages[0].sender_name = "张三"
        store_module._block_cache[block.block_id] = block

        result = await MemoryRetriever().expand_evidence(block.block_id)
        self.assertEqual(result.messages[0].sender_name, "张三")

    # ── get_card_by_id ─────────────────────────────────────────────────────────

    async def test_get_card_by_id_returns_cached_card(self):
        card = _make_card()
        gen_module._card_cache[card.memory_id] = card

        result = await MemoryRetriever().get_card_by_id(card.memory_id)
        self.assertEqual(result, card)
        self.assertEqual(result.decision_object, "测试议题")

    async def test_get_card_by_id_returns_none_for_unknown(self):
        result = await MemoryRetriever().get_card_by_id("nonexistent_card")
        self.assertIsNone(result)

    async def test_expand_evidence_with_multiple_blocks(self):
        b1, b2 = _make_block(), _make_block()
        store_module._block_cache[b1.block_id] = b1
        store_module._block_cache[b2.block_id] = b2

        r1 = await MemoryRetriever().expand_evidence(b1.block_id)
        r2 = await MemoryRetriever().expand_evidence(b2.block_id)
        self.assertEqual(r1.block_id, b1.block_id)
        self.assertEqual(r2.block_id, b2.block_id)


if __name__ == "__main__":
    unittest.main()
