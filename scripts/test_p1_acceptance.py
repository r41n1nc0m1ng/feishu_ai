"""
P1 验收测试 AC1~AC3

背景：模拟一个产品团队在群里讨论"是否做企业级记忆"的决策对话，
验收以下三条标准：

  AC1  用户问"之前怎么定的 / 为什么这么定"  → 返回对应 MemoryCard
  AC2  用户问"原话在哪 / 谁说的 / 依据是什么" → 展开对应 EvidenceBlock
  AC3  用户问"当前整体方案 / 总结一下"        → 返回 TopicSummary

运行：
    conda run -n feishu python scripts/test_p1_acceptance.py
"""
import asyncio
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory.schemas import (
    CardStatus, EvidenceBlock, EvidenceMessage, FeishuMessage,
    MemoryCard, MemoryType, TopicSummary,
)
from realtime.triggers import is_source_query, is_summary_query
from realtime.query_handler import RealtimeQueryHandler

CHAT = "accept_test_chat"
BLOCK_ID = str(uuid.uuid4())


# ── 测试数据（模拟真实群聊决策场景）──────────────────────────────────────────

def make_evidence_block() -> EvidenceBlock:
    """
    原始群聊记录：三条消息构成"不做企业级记忆"这一决策的来源。
    """
    return EvidenceBlock(
        block_id=BLOCK_ID,
        chat_id=CHAT,
        start_time=datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 5, 1, 10, 15, tzinfo=timezone.utc),
        messages=[
            EvidenceMessage(
                message_id="m1", sender_id="u_zhangsan", sender_name="张三",
                timestamp=datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc),
                text="我想做企业级记忆存储，支持跨群和私聊共享",
            ),
            EvidenceMessage(
                message_id="m2", sender_id="u_lisi", sender_name="李四",
                timestamp=datetime(2026, 5, 1, 10, 5, tzinfo=timezone.utc),
                text="耗时费工夫，也不方便在比赛现场展示",
            ),
            EvidenceMessage(
                message_id="m3", sender_id="u_zhangsan", sender_name="张三",
                timestamp=datetime(2026, 5, 1, 10, 8, tzinfo=timezone.utc),
                text="那就先做群聊记忆，企业级留到 P2",
            ),
        ],
    )


def make_card(title, decision, reason, memory_type=MemoryType.DECISION) -> MemoryCard:
    return MemoryCard(
        memory_id=str(uuid.uuid4()),
        chat_id=CHAT,
        decision_object=title,
        title=title,
        decision=decision,
        reason=reason,
        memory_type=memory_type,
        status=CardStatus.ACTIVE,
        source_block_ids=[BLOCK_ID],
    )


CARD_SCOPE = make_card(
    title="记忆存储范围",
    decision="P1 只做群聊内记忆，不做企业级存储和跨群共享",
    reason="企业级耗时费工夫，比赛阶段不适合展示",
)

CARD_ARCH = make_card(
    title="存储技术方案",
    decision="采用 SQLite（结构化真相源）+ Graphiti（语义召回）+ LLM（抽取与总结）三层架构",
    reason="分层职责清晰，避免 Graphiti 承担主状态管理",
)

CARD_ROUTE = make_card(
    title="查询路由优先级",
    decision="查询路由优先级固定为：来源类 > 整体类 > 决策类",
    reason="不同问法需要返回不同粒度的记忆对象",
)

TOPIC = TopicSummary(
    summary_id=str(uuid.uuid4()),
    chat_id=CHAT,
    topic="产品边界与技术架构",
    summary=(
        "P1 范围限定为群聊内记忆，不做企业级或跨群共享；"
        "技术上采用 SQLite+Graphiti+LLM 三层架构；"
        "查询路由按来源 > 整体 > 决策优先级分流。"
    ),
    covered_memory_ids=[CARD_SCOPE.memory_id, CARD_ARCH.memory_id, CARD_ROUTE.memory_id],
)


def make_msg(text: str) -> FeishuMessage:
    return FeishuMessage(
        message_id=str(uuid.uuid4()),
        sender_id="u_tester",
        chat_id=CHAT,
        chat_type="group",
        text=text,
        timestamp=datetime.now(timezone.utc),
        is_at_bot=False,
    )


def make_handler(cards, block=None, summaries=None) -> RealtimeQueryHandler:
    retriever = MagicMock()
    retriever.retrieve = AsyncMock(return_value=cards)
    retriever.expand_evidence = AsyncMock(return_value=block)
    retriever.retrieve_topic_summary = AsyncMock(return_value=summaries or [])
    return RealtimeQueryHandler(retriever=retriever, send_text=None)


# ── AC1：决策类问题 → 返回 MemoryCard ────────────────────────────────────────
#
# 模拟用户在群里问"当时怎么定的"类问题。
# 期望：action = "query"，回答包含 MemoryCard 的决策内容。

DECISION_QUERIES = [
    "之前怎么定的记忆存储方案？",          # 标准"之前怎么定的"
    "为什么不做企业级的",                    # 问理由
    "当时为什么这么设计存储架构",            # 问架构决策理由
    "查询路由是怎么定的来着？",              # "来着"触发词
    "存储方案当时是怎么选的",               # 无明确触发词，靠问号或上层 @bot 路由
]


async def test_ac1_returns_memory_card():
    print("\n=== AC1: 决策类问题 → MemoryCard ===")
    handler = make_handler(cards=[CARD_SCOPE])

    for q in DECISION_QUERIES:
        trace = await handler.handle_query_message(make_msg(q))
        assert trace.action == "query", (
            f"[FAIL] 期望 action=query，实际={trace.action!r}\n"
            f"       消息：{q!r}\n"
            f"       回答：{trace.reply_preview!r}"
        )
        assert "P1 只做群聊内记忆" in trace.reply_preview, (
            f"[FAIL] 回答应含 MemoryCard 决策内容\n"
            f"       消息：{q!r}\n"
            f"       实际回答：{trace.reply_preview!r}"
        )
        print(f"  PASS  [{trace.action}] {q!r}")
        print(f"        → {trace.reply_preview[:80]}")

    print("AC1 PASSED\n")


# ── AC2：来源类问题 → 展开 EvidenceBlock ─────────────────────────────────────
#
# 模拟用户追问"原话在哪 / 谁说的"类问题。
# 期望：action = "source"，回答展开原始聊天记录，包含发言者姓名。

SOURCE_QUERIES = [
    "原话在哪说的？",                        # 最直接的来源追问
    "谁说的不做企业级",                      # 追问发言者
    "依据是什么",                            # 追问依据
    "当时聊天记录在哪",                      # 明确要看聊天记录
    "有没有来源",                            # 泛化来源追问
    "当时怎么说的",                          # "当时.*怎么说"
]


async def test_ac2_expands_evidence():
    print("=== AC2: 来源类问题 → EvidenceBlock ===")
    block = make_evidence_block()
    handler = make_handler(cards=[CARD_SCOPE], block=block)

    for q in SOURCE_QUERIES:
        trace = await handler.handle_query_message(make_msg(q))
        assert trace.action == "source", (
            f"[FAIL] 期望 action=source，实际={trace.action!r}\n"
            f"       消息：{q!r}\n"
            f"       回答：{trace.reply_preview!r}"
        )
        has_sender = "张三" in trace.reply_preview or "李四" in trace.reply_preview
        assert has_sender, (
            f"[FAIL] 回答应展开聊天记录并含发言者姓名\n"
            f"       消息：{q!r}\n"
            f"       实际回答：{trace.reply_preview!r}"
        )
        print(f"  PASS  [{trace.action}] {q!r}")
        print(f"        → {trace.reply_preview[:80]}")

    print("AC2 PASSED\n")


# ── AC3：整体类问题 → 返回 TopicSummary ──────────────────────────────────────
#
# 模拟用户问"当前整体方案 / 总结"类问题。
# 期望：action = "summary"，回答包含 TopicSummary 的主题和摘要内容。

SUMMARY_QUERIES = [
    "当前整体方案是什么",                    # 标准整体查询
    "总结一下现在的方向",                    # "总结"触发词
    "当前边界怎么定的",                      # "当前.*边界"
    "整体现在怎么定",                        # "整体.*怎么定"
    "现在方向怎么定",                        # "方向.*怎么定"
]


async def test_ac3_returns_topic_summary():
    print("=== AC3: 整体类问题 → TopicSummary ===")
    handler = make_handler(cards=[CARD_SCOPE], summaries=[TOPIC])

    for q in SUMMARY_QUERIES:
        trace = await handler.handle_query_message(make_msg(q))
        assert trace.action == "summary", (
            f"[FAIL] 期望 action=summary，实际={trace.action!r}\n"
            f"       消息：{q!r}\n"
            f"       回答：{trace.reply_preview!r}"
        )
        assert TOPIC.topic in trace.reply_preview, (
            f"[FAIL] 回答应含 TopicSummary 主题名称\n"
            f"       消息：{q!r}\n"
            f"       实际回答：{trace.reply_preview!r}"
        )
        print(f"  PASS  [{trace.action}] {q!r}")
        print(f"        → {trace.reply_preview[:80]}")

    print("AC3 PASSED\n")


# ── 触发词分类自检（前提条件，先于 handler 测试运行）─────────────────────────

def test_trigger_classification():
    print("=== 触发词分类自检 ===")

    for q in DECISION_QUERIES:
        assert not is_source_query(q), f"决策问题不应命中 source: {q!r}"
        assert not is_summary_query(q), f"决策问题不应命中 summary: {q!r}"

    for q in SOURCE_QUERIES:
        assert is_source_query(q), f"来源问题应命中 source: {q!r}"

    for q in SUMMARY_QUERIES:
        assert is_summary_query(q), f"整体问题应命中 summary: {q!r}"
        assert not is_source_query(q), f"整体问题不应命中 source: {q!r}"

    print("触发词分类 PASSED\n")


# ── 空结果边界用例 ─────────────────────────────────────────────────────────────

async def test_empty_results():
    print("=== 边界：空结果时不崩溃 ===")
    handler_empty = make_handler(cards=[], block=None, summaries=[])

    trace1 = await handler_empty.handle_query_message(make_msg("之前怎么定的"))
    assert "没有查到" in trace1.reply_preview, f"空 MemoryCard 回答异常: {trace1.reply_preview!r}"
    print(f"  PASS  空 MemoryCard → {trace1.reply_preview[:60]}")

    trace2 = await handler_empty.handle_query_message(make_msg("原话在哪"))
    assert "没有查到" in trace2.reply_preview, f"空 source 回答异常: {trace2.reply_preview!r}"
    print(f"  PASS  空 source → {trace2.reply_preview[:60]}")

    trace3 = await handler_empty.handle_query_message(make_msg("当前整体方案是什么"))
    assert "没有查到" in trace3.reply_preview, f"空 TopicSummary 回答异常: {trace3.reply_preview!r}"
    print(f"  PASS  空 summary → {trace3.reply_preview[:60]}")

    print("边界用例 PASSED\n")


if __name__ == "__main__":
    try:
        test_trigger_classification()
        asyncio.run(test_ac1_returns_memory_card())
        asyncio.run(test_ac2_expands_evidence())
        asyncio.run(test_ac3_returns_topic_summary())
        asyncio.run(test_empty_results())
        print("=" * 50)
        print("ALL AC1~AC3 ACCEPTANCE TESTS PASSED")
    except AssertionError as e:
        print(f"\nFAIL: {e}")
        raise
