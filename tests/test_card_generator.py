"""
card_generator 单元测试 — mock LLM 调用和 Graphiti，只测卡片生成逻辑。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import memory.card_generator as gen_module
from memory.card_generator import CardGenerator
from memory.schemas import CardStatus, EvidenceBlock, EvidenceMessage, MemoryType

BASE = datetime(2026, 4, 28, 10, 0, tzinfo=timezone.utc)


def _make_block(chat_id: str = "oc_test") -> EvidenceBlock:
    msgs = [
        EvidenceMessage(message_id="m1", sender_id="u1", sender_name="A", timestamp=BASE, text="我们决定不做企业级记忆"),
        EvidenceMessage(message_id="m2", sender_id="u2", sender_name="B", timestamp=BASE, text="同意，太复杂了"),
    ]
    return EvidenceBlock(chat_id=chat_id, start_time=BASE, end_time=BASE, messages=msgs)


def _patch_llm(response: dict):
    """返回固定 LLM 响应的 patch context manager。"""
    return patch.object(CardGenerator, "_call_llm", new=AsyncMock(return_value=response))


def _patch_graphiti():
    return patch("memory.card_generator.GraphitiClient", **{"return_value.g": None})


class CardGeneratorTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        gen_module._card_cache.clear()
        gen_module._cards_by_object.clear()

    async def test_add_operation_creates_card(self):
        block = _make_block()
        llm_resp = {
            "operation": "ADD",
            "decision_object": "企业级记忆范围",
            "title": "不做企业级记忆",
            "decision": "MVP 不做企业级记忆",
            "reason": "太复杂",
            "memory_type": "decision",
        }
        with _patch_llm(llm_resp), _patch_graphiti():
            card = await CardGenerator().generate(block)

        self.assertIsNotNone(card)
        self.assertEqual(card.decision_object, "企业级记忆范围")
        self.assertEqual(card.status, CardStatus.ACTIVE)
        self.assertIn(block.block_id, card.source_block_ids)
        self.assertIn(card.memory_id, gen_module._card_cache)

    async def test_noop_returns_none(self):
        block = _make_block()
        with _patch_llm({"operation": "NOOP"}), _patch_graphiti():
            card = await CardGenerator().generate(block)
        self.assertIsNone(card)
        self.assertEqual(len(gen_module._card_cache), 0)

    async def test_progress_operation_creates_card_with_progress_type(self):
        block = _make_block()
        llm_resp = {
            "operation": "PROGRESS",
            "decision_object": "企业级记忆是否保留",
            "title": "讨论进行中",
            "decision": "倾向不做，但未确认",
            "reason": "还有疑问",
            "memory_type": "progress",
        }
        with _patch_llm(llm_resp), _patch_graphiti():
            card = await CardGenerator().generate(block)

        self.assertIsNotNone(card)
        self.assertEqual(card.memory_type, MemoryType.PROGRESS)

    async def test_supersede_marks_old_card_deprecated(self):
        # 先写入旧卡片
        from memory.schemas import MemoryCard
        old_card = MemoryCard(
            chat_id="oc_test",
            decision_object="个人入口策略",
            title="不做个人入口",
            decision="完全不做个人入口",
            reason="太复杂",
        )
        gen_module._card_cache[old_card.memory_id] = old_card
        gen_module._cards_by_object["个人入口策略"] = old_card

        block = _make_block()
        llm_resp = {
            "operation": "SUPERSEDE",
            "decision_object": "个人入口策略",
            "title": "保留私聊查询入口",
            "decision": "保留私聊入口，仅用于查询",
            "reason": "用户需要",
            "memory_type": "version_update",
        }
        with _patch_llm(llm_resp), _patch_graphiti():
            new_card = await CardGenerator().generate(block)

        self.assertIsNotNone(new_card)
        self.assertEqual(new_card.supersedes_memory_id, old_card.memory_id)
        self.assertEqual(gen_module._card_cache[old_card.memory_id].status, CardStatus.DEPRECATED)

    async def test_invalid_memory_type_falls_back_to_decision(self):
        block = _make_block()
        llm_resp = {
            "operation": "ADD",
            "decision_object": "测试议题",
            "title": "测试标题",
            "decision": "测试决策",
            "reason": "测试理由",
            "memory_type": "invalid_type_xyz",   # 无效值
        }
        with _patch_llm(llm_resp), _patch_graphiti():
            card = await CardGenerator().generate(block)

        self.assertIsNotNone(card)
        self.assertEqual(card.memory_type, MemoryType.DECISION)

    async def test_llm_failure_returns_none(self):
        block = _make_block()
        with patch.object(CardGenerator, "_call_llm", new=AsyncMock(return_value=None)), _patch_graphiti():
            card = await CardGenerator().generate(block)
        self.assertIsNone(card)


if __name__ == "__main__":
    unittest.main()
