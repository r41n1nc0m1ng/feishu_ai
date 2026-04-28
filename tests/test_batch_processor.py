"""
batch_processor 单元测试 — mock FeishuAPIClient 和存储层，只测调度逻辑。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import memory.batch_processor as bp_module
from memory.batch_processor import BatchProcessor
from memory.schemas import EvidenceBlock, EvidenceMessage, FeishuMessage

BASE = datetime(2026, 4, 28, 10, 0, tzinfo=timezone.utc)


def _feishu_msg(chat_id: str = "oc_test") -> FeishuMessage:
    return FeishuMessage(
        message_id="m1",
        sender_id="u1",
        chat_id=chat_id,
        chat_type="group",
        text="普通消息",
        timestamp=BASE,
    )


def _evidence_msg(offset: int = 0) -> EvidenceMessage:
    return EvidenceMessage(
        message_id=f"e{offset}",
        sender_id="u1",
        sender_name="A",
        timestamp=BASE,
        text=f"消息 {offset}",
    )


class BatchProcessorRegisterTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        bp_module._active_chats.clear()

    async def test_register_adds_chat_to_registry(self):
        msg = _feishu_msg("oc_a")
        with patch("memory.batch_processor.FeishuAPIClient") as MockClient:
            MockClient.return_value.get_chat_info = AsyncMock(return_value={"name": "测试群 A"})
            await BatchProcessor().register_chat(msg)

        self.assertIn("oc_a", bp_module._active_chats)
        self.assertEqual(bp_module._active_chats["oc_a"].group_name, "测试群 A")

    async def test_register_does_not_duplicate(self):
        msg = _feishu_msg("oc_b")
        with patch("memory.batch_processor.FeishuAPIClient") as MockClient:
            MockClient.return_value.get_chat_info = AsyncMock(return_value={"name": "群 B"})
            proc = BatchProcessor()
            await proc.register_chat(msg)
            await proc.register_chat(msg)   # 第二次应跳过

        self.assertEqual(len(bp_module._active_chats), 1)

    async def test_register_multiple_chats(self):
        with patch("memory.batch_processor.FeishuAPIClient") as MockClient:
            MockClient.return_value.get_chat_info = AsyncMock(return_value={"name": ""})
            proc = BatchProcessor()
            await proc.register_chat(_feishu_msg("oc_1"))
            await proc.register_chat(_feishu_msg("oc_2"))
            await proc.register_chat(_feishu_msg("oc_3"))

        self.assertEqual(len(bp_module._active_chats), 3)

    async def test_register_handles_api_failure_gracefully(self):
        msg = _feishu_msg("oc_err")
        with patch("memory.batch_processor.FeishuAPIClient") as MockClient:
            MockClient.return_value.get_chat_info = AsyncMock(side_effect=Exception("网络错误"))
            await BatchProcessor().register_chat(msg)   # 不应抛出异常

        # chat_id 仍然应被注册，只是 group_name 为空
        self.assertIn("oc_err", bp_module._active_chats)


class BatchProcessorPipelineTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        bp_module._active_chats.clear()

    async def test_process_chat_calls_full_pipeline(self):
        """验证 _process_chat 依次调用拉取→切分→存储→生成。"""
        from memory.schemas import ChatMemorySpace
        bp_module._active_chats["oc_pipe"] = ChatMemorySpace(chat_id="oc_pipe")

        fetched = [_evidence_msg(0), _evidence_msg(1)]
        mock_block = MagicMock(spec=EvidenceBlock)
        mock_block.block_id = "blk_001"
        mock_block.chat_id = "oc_pipe"
        mock_block.end_time = BASE

        with patch("memory.batch_processor.FeishuAPIClient") as MockClient, \
             patch("memory.batch_processor.segment", return_value=[mock_block]) as mock_seg, \
             patch("memory.batch_processor.EvidenceStore") as MockStore, \
             patch("memory.batch_processor.CardGenerator") as MockGen:

            MockClient.return_value.fetch_messages = AsyncMock(return_value=fetched)
            MockStore.return_value.save = AsyncMock()
            MockGen.return_value.generate = AsyncMock(return_value=None)

            await BatchProcessor()._process_chat("oc_pipe")

        mock_seg.assert_called_once()
        MockStore.return_value.save.assert_called_once_with(mock_block)
        MockGen.return_value.generate.assert_called_once_with(mock_block)

    async def test_process_chat_skips_when_no_messages(self):
        from memory.schemas import ChatMemorySpace
        bp_module._active_chats["oc_empty"] = ChatMemorySpace(chat_id="oc_empty")

        with patch("memory.batch_processor.FeishuAPIClient") as MockClient, \
             patch("memory.batch_processor.segment") as mock_seg:

            MockClient.return_value.fetch_messages = AsyncMock(return_value=[])
            await BatchProcessor()._process_chat("oc_empty")

        mock_seg.assert_not_called()

    async def test_process_chat_updates_cursor(self):
        from memory.schemas import ChatMemorySpace
        bp_module._active_chats["oc_cursor"] = ChatMemorySpace(chat_id="oc_cursor")

        fetched = [_evidence_msg(0)]
        mock_block = MagicMock(spec=EvidenceBlock)
        mock_block.chat_id = "oc_cursor"
        mock_block.end_time = BASE

        with patch("memory.batch_processor.FeishuAPIClient") as MockClient, \
             patch("memory.batch_processor.segment", return_value=[mock_block]), \
             patch("memory.batch_processor.EvidenceStore") as MockStore, \
             patch("memory.batch_processor.CardGenerator") as MockGen:

            MockClient.return_value.fetch_messages = AsyncMock(return_value=fetched)
            MockStore.return_value.save = AsyncMock()
            MockGen.return_value.generate = AsyncMock(return_value=None)

            await BatchProcessor()._process_chat("oc_cursor")

        # 游标应更新为最后一条消息的时间戳
        self.assertEqual(
            bp_module._active_chats["oc_cursor"].last_fetch_at,
            fetched[-1].timestamp,
        )


if __name__ == "__main__":
    unittest.main()
