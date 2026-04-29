"""
evidence_store 单元测试 — mock Graphiti，只测内存缓存和写入调用。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import memory.evidence_store as store_module
from memory.evidence_store import EvidenceStore
from memory.schemas import EvidenceBlock, EvidenceMessage

BASE = datetime(2026, 4, 28, 10, 0, tzinfo=timezone.utc)


def _make_block(n_msgs: int = 2, chat_id: str = "oc_test") -> EvidenceBlock:
    msgs = [
        EvidenceMessage(
            message_id=f"msg_{i}",
            sender_id="u1",
            sender_name="A",
            timestamp=BASE,
            text=f"消息 {i}",
        )
        for i in range(n_msgs)
    ]
    return EvidenceBlock(chat_id=chat_id, start_time=BASE, end_time=BASE, messages=msgs)


class EvidenceStoreTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        store_module._block_cache.clear()
        self._old_flag = os.environ.get("INDEX_EVIDENCE_IN_GRAPHITI")

    def tearDown(self):
        if self._old_flag is None:
            os.environ.pop("INDEX_EVIDENCE_IN_GRAPHITI", None)
        else:
            os.environ["INDEX_EVIDENCE_IN_GRAPHITI"] = self._old_flag

    async def test_save_puts_block_in_cache(self):
        block = _make_block()
        with patch("memory.evidence_store.GraphitiClient") as MockG:
            MockG.return_value.g = None
            await EvidenceStore().save(block)
        self.assertIn(block.block_id, store_module._block_cache)

    async def test_get_returns_saved_block(self):
        block = _make_block()
        store_module._block_cache[block.block_id] = block
        result = await EvidenceStore().get(block.block_id)
        self.assertEqual(result, block)

    async def test_get_returns_none_for_unknown_id(self):
        result = await EvidenceStore().get("does_not_exist")
        self.assertIsNone(result)

    async def test_save_calls_graphiti_add_episode(self):
        block = _make_block()
        mock_g = AsyncMock()
        os.environ["INDEX_EVIDENCE_IN_GRAPHITI"] = "true"
        with patch("memory.evidence_store.GraphitiClient") as MockG:
            MockG.return_value.g = mock_g
            await EvidenceStore().save(block)
        mock_g.add_episode.assert_called_once()
        kwargs = mock_g.add_episode.call_args.kwargs
        self.assertIn(block.block_id, kwargs["name"])
        self.assertEqual(kwargs["group_id"], block.chat_id)

    async def test_save_skips_graphiti_by_default(self):
        block = _make_block()
        mock_g = AsyncMock()
        with patch("memory.evidence_store.GraphitiClient") as MockG:
            MockG.return_value.g = mock_g
            await EvidenceStore().save(block)
        mock_g.add_episode.assert_not_called()

    async def test_save_skips_graphiti_when_not_initialized(self):
        block = _make_block()
        with patch("memory.evidence_store.GraphitiClient") as MockG:
            MockG.return_value.g = None
            await EvidenceStore().save(block)   # should not raise
        self.assertIn(block.block_id, store_module._block_cache)

    async def test_all_block_ids_lists_cache_keys(self):
        b1, b2 = _make_block(), _make_block()
        store_module._block_cache[b1.block_id] = b1
        store_module._block_cache[b2.block_id] = b2
        ids = EvidenceStore().all_block_ids()
        self.assertIn(b1.block_id, ids)
        self.assertIn(b2.block_id, ids)


if __name__ == "__main__":
    unittest.main()
