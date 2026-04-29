"""
P0 功能测试 — 验证基本流程端到端跑通。

模拟场景：一段含决策的群聊对话经过完整批处理流水线，
最终用户 @机器人 提问时能召回 MemoryCard 并展开原始消息来源。

P0 完成标准（逐一验证）：
  [1] 群消息能稳定进入系统
  [2] 后台能沉淀出可追溯的证据（EvidenceBlock）
  [3] 证据能形成可检索的记忆（MemoryCard）
  [4] 用户问"之前怎么定的"，系统能答出来
  [5] 用户追问"依据是什么"，系统能给到来源

外部依赖全部 mock（无需 Ollama / Neo4j / 飞书 API）。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import memory.batch_processor as bp_module
import memory.card_generator as gen_module
import memory.evidence_store as store_module
from memory.batch_processor import BatchProcessor
from memory.card_generator import CardGenerator
from memory.evidence_store import EvidenceStore
from memory.retriever import MemoryRetriever
from memory.schemas import (
    ChatMemorySpace, EvidenceMessage, FetchBatch, FeishuMessage,
)
from preprocessor.event_segmenter import segment
from realtime.dispatcher import dispatch_message
from realtime.query_handler import RealtimeQueryHandler

# ── 测试数据 ─────────────────────────────────────────────────────────────────

CHAT_ID = "oc_p0_test_group"
BASE_TIME = datetime(2026, 4, 28, 10, 0, 0, tzinfo=timezone.utc)

# 模拟一段含明确决策的群聊
CONVERSATION = [
    (0,   "user_A", "小王", "我觉得我们不应该做企业级记忆，权限太复杂了。"),
    (120, "user_B", "小李", "同意，群聊边界更自然，Demo 也更好讲。"),
    (240, "user_A", "小王", "那就定了，专注群聊决策记忆，不做企业级。"),
    (360, "user_B", "小李", "那我们接下来讨论 Benchmark 怎么设计。"),
]

# mock LLM 返回一个真实的 ADD 操作
LLM_CARD_RESPONSE = {
    "operation": "ADD",
    "decision_object": "企业级记忆是否进入 MVP",
    "title": "MVP 阶段不做企业级记忆",
    "decision": "MVP 阶段专注群聊决策记忆，不做企业级记忆。",
    "reason": "企业级记忆权限复杂，群聊边界更自然，Demo 更清晰。",
    "memory_type": "decision",
}


def _make_evidence_messages():
    return [
        EvidenceMessage(
            message_id=f"msg_{i}",
            sender_id=uid,
            sender_name=name,
            timestamp=BASE_TIME + timedelta(seconds=offset),
            text=text,
        )
        for i, (offset, uid, name, text) in enumerate(CONVERSATION)
    ]


def _make_fetch_batch():
    msgs = _make_evidence_messages()
    return FetchBatch(
        chat_id=CHAT_ID,
        fetch_start=msgs[0].timestamp,
        fetch_end=msgs[-1].timestamp,
        messages=msgs,
    )


def _make_feishu_message(text: str, is_at_bot: bool = False) -> FeishuMessage:
    return FeishuMessage(
        message_id="query_msg",
        sender_id="user_C",
        chat_id=CHAT_ID,
        chat_type="group",
        text=text,
        timestamp=BASE_TIME + timedelta(hours=1),
        mentions=["bot"] if is_at_bot else [],
        is_at_bot=is_at_bot,
    )


# ── 辅助：从内存缓存检索（绕过 Graphiti，功能测试专用）──────────────────────

class CacheRetriever:
    """直接从内存缓存返回 MemoryCard，无需 Graphiti。"""

    async def retrieve(self, chat_id: str, query: str, limit: int = 3):
        return [
            c for c in gen_module._card_cache.values()
            if c.chat_id == chat_id
        ][:limit]


# ── 功能测试主体 ──────────────────────────────────────────────────────────────

class P0FunctionalTest(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        bp_module._active_chats.clear()
        store_module._block_cache.clear()
        gen_module._card_cache.clear()
        gen_module._cards_by_object.clear()

    # [1] 群消息能稳定进入系统 ─────────────────────────────────────────────────

    async def test_1_messages_enter_system_via_register(self):
        """普通群消息经由 dispatcher noop 分支注册到批处理器。"""
        msg = _make_feishu_message("收到，我晚点看")  # 普通消息，不触发实时通道

        registered = []

        async def fake_register(m):
            registered.append(m.chat_id)

        await dispatch_message(msg, legacy_ingest=fake_register)

        self.assertEqual(registered, [CHAT_ID],
                         "普通消息应经由 noop 分支送达 legacy_ingest（批处理注册钩子）")

    async def test_1b_register_chat_creates_memory_space(self):
        """register_chat 在注册表中建立 ChatMemorySpace。"""
        msg = _make_feishu_message("随便说一句")
        with patch("memory.batch_processor.FeishuAPIClient") as MockClient:
            MockClient.return_value.get_chat_info = AsyncMock(return_value={"name": "P0 测试群"})
            await BatchProcessor().register_chat(msg)

        self.assertIn(CHAT_ID, bp_module._active_chats)
        self.assertEqual(bp_module._active_chats[CHAT_ID].group_name, "P0 测试群")

    # [2] 后台能沉淀出可追溯的证据（EvidenceBlock） ──────────────────────────

    async def test_2_segmentation_produces_evidence_block(self):
        """一段时间窗口内的对话切分为至少一个 EvidenceBlock。"""
        batch = _make_fetch_batch()
        blocks = segment(batch)

        self.assertGreater(len(blocks), 0, "应产生至少一个 EvidenceBlock")
        total_msgs = sum(len(b.messages) for b in blocks)
        self.assertEqual(total_msgs, len(CONVERSATION), "所有消息应被保留")

    async def test_2b_evidence_block_is_saved_to_store(self):
        """EvidenceBlock 写入存储后可按 block_id 精确查询。"""
        batch = _make_fetch_batch()
        blocks = segment(batch)
        block = blocks[0]

        with patch("memory.evidence_store.GraphitiClient") as MockG:
            MockG.return_value.g = None
            await EvidenceStore().save(block)

        stored = await EvidenceStore().get(block.block_id)
        self.assertIsNotNone(stored)
        self.assertEqual(stored.chat_id, CHAT_ID)
        self.assertEqual(len(stored.messages), len(block.messages))

    async def test_2c_original_messages_are_preserved(self):
        """EvidenceBlock 中的原始消息内容、发送人、时间完整保留。"""
        batch = _make_fetch_batch()
        blocks = segment(batch)

        with patch("memory.evidence_store.GraphitiClient") as MockG:
            MockG.return_value.g = None
            await EvidenceStore().save(blocks[0])

        stored = await EvidenceStore().get(blocks[0].block_id)
        first_msg = stored.messages[0]
        self.assertEqual(first_msg.sender_name, "小王")
        self.assertIn("企业级记忆", first_msg.text)

    # [3] 证据能形成可检索的记忆（MemoryCard） ──────────────────────────────

    async def test_3_card_generator_produces_memory_card(self):
        """CardGenerator 基于 EvidenceBlock 生成 MemoryCard，写入内存缓存。"""
        batch = _make_fetch_batch()
        blocks = segment(batch)

        with patch.object(CardGenerator, "_call_llm", new=AsyncMock(return_value=LLM_CARD_RESPONSE)), \
             patch("memory.card_generator.GraphitiClient") as MockG:
            MockG.return_value.g = None
            card = await CardGenerator().generate(blocks[0])

        self.assertIsNotNone(card, "应生成 MemoryCard，非 NOOP")
        self.assertEqual(card.chat_id, CHAT_ID)
        self.assertIn(blocks[0].block_id, card.source_block_ids, "source_block_ids 应指向来源块")
        self.assertIn(card.memory_id, gen_module._card_cache, "卡片应写入内存缓存")

    async def test_3b_full_batch_pipeline_end_to_end(self):
        """完整批处理流水线：拉取 → 切分 → 存证据 → 生成卡片。"""
        bp_module._active_chats[CHAT_ID] = ChatMemorySpace(chat_id=CHAT_ID)
        fetched = _make_evidence_messages()

        with patch("memory.batch_processor.FeishuAPIClient") as MockClient, \
             patch.object(CardGenerator, "_call_llm", new=AsyncMock(return_value=LLM_CARD_RESPONSE)), \
             patch("memory.evidence_store.GraphitiClient") as MockEG, \
             patch("memory.card_generator.GraphitiClient") as MockCG:

            MockClient.return_value.fetch_messages = AsyncMock(return_value=(fetched, fetched[-1].timestamp))
            MockEG.return_value.g = None
            MockCG.return_value.g = None

            await BatchProcessor()._process_chat(CHAT_ID)

        self.assertGreater(len(store_module._block_cache), 0, "应有 EvidenceBlock 写入")
        self.assertGreater(len(gen_module._card_cache), 0, "应有 MemoryCard 写入")

    # [4] 用户问"之前怎么定的"，系统能答出来 ──────────────────────────────────

    async def test_4_realtime_query_returns_memory_card(self):
        """@机器人 提问经由 RealtimeQueryHandler 返回 MemoryCard 内容。"""
        # 先通过批处理写入一张卡片
        batch = _make_fetch_batch()
        blocks = segment(batch)
        with patch.object(CardGenerator, "_call_llm", new=AsyncMock(return_value=LLM_CARD_RESPONSE)), \
             patch("memory.card_generator.GraphitiClient") as MockG:
            MockG.return_value.g = None
            await CardGenerator().generate(blocks[0])

        # 用 CacheRetriever 绕过 Graphiti，验证查询路径
        sent: list[tuple] = []

        async def fake_send(chat_id, text):
            sent.append((chat_id, text))

        query_msg = _make_feishu_message("@机器人 之前为什么不做企业级记忆", is_at_bot=True)
        handler = RealtimeQueryHandler(retriever=CacheRetriever(), send_text=fake_send)
        trace = await handler.handle_query_message(query_msg)

        self.assertEqual(trace.retrieved_count, 1, "应召回 1 条 MemoryCard")
        self.assertEqual(len(sent), 1, "应向群聊发送回复")
        reply_text = sent[0][1]
        self.assertIn("企业级记忆", reply_text, "回复中应包含决策内容")

    async def test_4b_at_bot_triggers_query_via_dispatcher(self):
        """@机器人 消息通过 dispatcher 正确路由到 query 通道。"""
        query_msg = _make_feishu_message("@机器人 为什么不做企业级记忆", is_at_bot=True)
        sent = []

        async def fake_send(chat_id, text):
            sent.append(text)

        handler = RealtimeQueryHandler(retriever=CacheRetriever(), send_text=fake_send)
        trace = await dispatch_message(query_msg, query_handler=handler)

        self.assertEqual(trace.action, "query")
        self.assertFalse(trace.delegated_to_legacy)
        self.assertEqual(len(sent), 1)

    # [5] 用户追问"依据是什么"，系统能给到来源 ──────────────────────────────

    async def test_5_expand_evidence_returns_original_messages(self):
        """根据 MemoryCard.source_block_ids 展开原始 EvidenceBlock。"""
        # 写入 EvidenceBlock
        batch = _make_fetch_batch()
        blocks = segment(batch)
        with patch("memory.evidence_store.GraphitiClient") as MockG:
            MockG.return_value.g = None
            await EvidenceStore().save(blocks[0])

        # 写入 MemoryCard（source_block_ids 指向该块）
        with patch.object(CardGenerator, "_call_llm", new=AsyncMock(return_value=LLM_CARD_RESPONSE)), \
             patch("memory.card_generator.GraphitiClient") as MockG:
            MockG.return_value.g = None
            card = await CardGenerator().generate(blocks[0])

        self.assertIsNotNone(card)

        # 展开来源
        retriever = MemoryRetriever()
        expanded = await retriever.expand_evidence(card.source_block_ids[0])

        self.assertIsNotNone(expanded, "应能展开 EvidenceBlock")
        self.assertEqual(expanded.chat_id, CHAT_ID)
        texts = [m.text for m in expanded.messages]
        self.assertTrue(
            any("企业级记忆" in t for t in texts),
            "展开的原始消息应包含决策讨论内容",
        )

    async def test_5b_full_p0_criteria_in_sequence(self):
        """
        一次性验证全部 5 条 P0 标准，模拟完整用户旅程：
        群聊讨论 → 批处理沉淀 → @机器人提问 → 召回 → 追问来源 → 展开原文。
        """
        # === 准备：批处理写入 ===
        bp_module._active_chats[CHAT_ID] = ChatMemorySpace(chat_id=CHAT_ID)
        fetched = _make_evidence_messages()

        with patch("memory.batch_processor.FeishuAPIClient") as MockClient, \
             patch.object(CardGenerator, "_call_llm", new=AsyncMock(return_value=LLM_CARD_RESPONSE)), \
             patch("memory.evidence_store.GraphitiClient") as MockEG, \
             patch("memory.card_generator.GraphitiClient") as MockCG:

            MockClient.return_value.fetch_messages = AsyncMock(return_value=(fetched, fetched[-1].timestamp))
            MockEG.return_value.g = None
            MockCG.return_value.g = None

            await BatchProcessor()._process_chat(CHAT_ID)

        # 断言 [2] 有证据
        self.assertGreater(len(store_module._block_cache), 0)
        # 断言 [3] 有记忆卡片
        self.assertGreater(len(gen_module._card_cache), 0)

        card = list(gen_module._card_cache.values())[0]

        # 断言 [4] 可召回
        cards = await CacheRetriever().retrieve(CHAT_ID, "企业级记忆")
        self.assertEqual(len(cards), 1)
        self.assertIn("企业级记忆", cards[0].decision)

        # 断言 [5] 可展开来源
        block = await MemoryRetriever().expand_evidence(card.source_block_ids[0])
        self.assertIsNotNone(block)
        all_texts = " ".join(m.text for m in block.messages)
        self.assertIn("企业级记忆", all_texts)


if __name__ == "__main__":
    unittest.main(verbosity=2)
