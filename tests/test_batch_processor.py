"""
batch_processor 单元测试 — mock FeishuAPIClient 和存储层，只测调度逻辑。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import memory.batch_processor as bp_module
import memory.store as store_module
from memory.batch_processor import BatchProcessor
from feishu.api_client import InvalidChatError
from memory.schemas import EvidenceBlock, EvidenceMessage, FeishuMessage

# 测试期间 mock 掉 store 的 SQLite 读写，避免污染生产数据库
_STORE_PATCHES = [
    patch.object(store_module, "save_chat_space"),
    patch.object(store_module, "load_all_chat_spaces", return_value=[]),
    patch.object(store_module, "delete_chat_space"),
]

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
        bp_module._cache_restored = True   # 跳过 SQLite 恢复，避免污染生产数据库
        for p in _STORE_PATCHES:
            p.start()

    def tearDown(self):
        for p in _STORE_PATCHES:
            p.stop()

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
        bp_module._cache_restored = True
        for p in _STORE_PATCHES:
            p.start()

    def tearDown(self):
        for p in _STORE_PATCHES:
            p.stop()

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
             patch("memory.batch_processor.segment_async", AsyncMock(return_value=[mock_block])) as mock_seg, \
             patch("memory.batch_processor.EvidenceStore") as MockStore, \
             patch("memory.batch_processor.CardGenerator") as MockGen:

            MockClient.return_value.fetch_messages = AsyncMock(return_value=(fetched, BASE))
            MockStore.return_value.save = AsyncMock()
            MockGen.return_value.generate = AsyncMock(return_value=None)

            await BatchProcessor()._process_chat("oc_pipe")

        mock_seg.assert_awaited_once()
        MockStore.return_value.save.assert_called_once_with(mock_block)
        MockGen.return_value.generate.assert_called_once_with(mock_block)

    async def test_process_chat_skips_when_no_messages(self):
        from memory.schemas import ChatMemorySpace
        bp_module._active_chats["oc_empty"] = ChatMemorySpace(chat_id="oc_empty")

        with patch("memory.batch_processor.FeishuAPIClient") as MockClient, \
             patch("memory.batch_processor.segment_async", AsyncMock(return_value=[])) as mock_seg:

            MockClient.return_value.fetch_messages = AsyncMock(return_value=([], None))
            await BatchProcessor()._process_chat("oc_empty")

        mock_seg.assert_not_awaited()

    async def test_process_chat_updates_cursor(self):
        from memory.schemas import ChatMemorySpace
        bp_module._active_chats["oc_cursor"] = ChatMemorySpace(chat_id="oc_cursor")

        fetched = [_evidence_msg(0)]
        mock_block = MagicMock(spec=EvidenceBlock)
        mock_block.chat_id = "oc_cursor"
        mock_block.end_time = BASE

        with patch("memory.batch_processor.FeishuAPIClient") as MockClient, \
             patch("memory.batch_processor.segment_async", AsyncMock(return_value=[mock_block])), \
             patch("memory.batch_processor.EvidenceStore") as MockStore, \
             patch("memory.batch_processor.CardGenerator") as MockGen:

            MockClient.return_value.fetch_messages = AsyncMock(return_value=(fetched, BASE))
            MockStore.return_value.save = AsyncMock()
            MockGen.return_value.generate = AsyncMock(return_value=None)

            await BatchProcessor()._process_chat("oc_cursor")

        # 游标应更新为最后一条消息的时间戳
        self.assertEqual(
            bp_module._active_chats["oc_cursor"].last_fetch_at,
            fetched[-1].timestamp,
        )

    async def test_process_chat_includes_lookback_context(self):
        from memory.schemas import ChatMemorySpace, EvidenceMessage
        bp_module._active_chats["oc_lookback"] = ChatMemorySpace(
            chat_id="oc_lookback",
            last_fetch_at=BASE,
        )

        old_msg = EvidenceMessage(
            message_id="old_1",
            sender_id="u1",
            sender_name="A",
            timestamp=BASE,
            text="前文上下文",
        )
        new_msg = EvidenceMessage(
            message_id="new_1",
            sender_id="u1",
            sender_name="A",
            timestamp=BASE.replace(second=BASE.second + 1),
            text="最新消息",
        )
        mock_block = MagicMock(spec=EvidenceBlock)
        mock_block.chat_id = "oc_lookback"
        mock_block.end_time = BASE

        with patch("memory.batch_processor.FeishuAPIClient") as MockClient, \
             patch("memory.batch_processor.segment_async", AsyncMock(return_value=[mock_block])) as mock_seg, \
             patch("memory.batch_processor.EvidenceStore") as MockStore, \
             patch("memory.batch_processor.CardGenerator") as MockGen:

            MockClient.return_value.fetch_messages = AsyncMock(return_value=([old_msg, new_msg], new_msg.timestamp))
            MockStore.return_value.save = AsyncMock()
            MockGen.return_value.generate = AsyncMock(return_value=None)

            await BatchProcessor()._process_chat("oc_lookback")

        sent_batch = mock_seg.await_args.args[0]
        self.assertEqual(len(sent_batch.messages), 2)

    async def test_process_chat_unregisters_invalid_chat(self):
        from memory.schemas import ChatMemorySpace
        bp_module._active_chats["oc_invalid"] = ChatMemorySpace(chat_id="oc_invalid")

        with patch("memory.batch_processor.FeishuAPIClient") as MockClient, \
             patch("memory.batch_processor.segment_async", AsyncMock(return_value=[])) as mock_seg:
            MockClient.return_value.fetch_messages = AsyncMock(side_effect=InvalidChatError("oc_invalid"))

            await BatchProcessor()._process_chat("oc_invalid")

        self.assertNotIn("oc_invalid", bp_module._active_chats)
        store_module.delete_chat_space.assert_called_once_with("oc_invalid")
        mock_seg.assert_not_awaited()


class ProcessNowTests(unittest.IsolatedAsyncioTestCase):


    def setUp(self):
        bp_module._active_chats.clear()
        bp_module._cache_restored = True
        for p in _STORE_PATCHES:
            p.start()

    def tearDown(self):
        for p in _STORE_PATCHES:
            p.stop()

    async def test_process_now_runs_pipeline_for_registered_chat(self):
        """process_now 对已注册群立即执行一次完整流水线。"""
        from memory.schemas import ChatMemorySpace
        bp_module._active_chats["oc_now"] = ChatMemorySpace(chat_id="oc_now")

        fetched = [_evidence_msg(0), _evidence_msg(1)]
        mock_block = MagicMock(spec=EvidenceBlock)
        mock_block.chat_id = "oc_now"
        mock_block.end_time = BASE

        with patch("memory.batch_processor.FeishuAPIClient") as MockClient, \
             patch("memory.batch_processor.segment_async", AsyncMock(return_value=[mock_block])), \
             patch("memory.batch_processor.EvidenceStore") as MockStore, \
             patch("memory.batch_processor.CardGenerator") as MockGen:

            MockClient.return_value.fetch_messages = AsyncMock(return_value=(fetched, BASE))
            MockStore.return_value.save = AsyncMock()
            MockGen.return_value.generate = AsyncMock(return_value=None)

            await BatchProcessor().process_now("oc_now")

        MockStore.return_value.save.assert_called_once_with(mock_block)

    async def test_process_now_skips_unregistered_chat(self):
        """process_now 对未注册的 chat_id 静默跳过，不抛出异常。"""
        with patch("memory.batch_processor.FeishuAPIClient") as MockClient:
            MockClient.return_value.fetch_messages = AsyncMock(return_value=([], None))
            await BatchProcessor().process_now("oc_not_registered")  # 不应抛出

    async def test_register_then_process_now_full_flow(self):
        """register_chat_by_id + process_now 模拟 bot 入群完整流程。"""
        fetched = [_evidence_msg(0)]
        mock_block = MagicMock(spec=EvidenceBlock)
        mock_block.chat_id = "oc_join"
        mock_block.end_time = BASE

        with patch("memory.batch_processor.FeishuAPIClient") as MockClient, \
             patch("memory.batch_processor.segment_async", AsyncMock(return_value=[mock_block])), \
             patch("memory.batch_processor.EvidenceStore") as MockStore, \
             patch("memory.batch_processor.CardGenerator") as MockGen:

            MockClient.return_value.get_chat_info = AsyncMock(return_value={"name": "新群"})
            MockClient.return_value.fetch_messages = AsyncMock(return_value=(fetched, BASE))
            MockStore.return_value.save = AsyncMock()
            MockGen.return_value.generate = AsyncMock(return_value=None)

            proc = BatchProcessor()
            await proc.register_chat_by_id("oc_join", "新群")
            await proc.process_now("oc_join")

        self.assertIn("oc_join", bp_module._active_chats)
        MockStore.return_value.save.assert_called_once()


if __name__ == "__main__":
    unittest.main()
