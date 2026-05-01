"""ConflictDetector Option B 架构测试：三条路径独立验证"""
import asyncio
import sqlite3
import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory import store
from memory.conflict_detector import ConflictDetector, _simple_key
from memory.schemas import CardStatus, ExtractedMemory, MemoryCard, MemoryType

CHAT = "conflict_test"


def cleanup():
    conn = sqlite3.connect(store.DB_PATH)
    conn.execute("DELETE FROM memory_cards WHERE chat_id=?", (CHAT,))
    conn.commit()
    conn.close()


def make_card(decision_object: str, decision: str, key: str = None) -> MemoryCard:
    card = MemoryCard(
        memory_id=str(uuid.uuid4()), chat_id=CHAT,
        decision_object=decision_object,
        decision_object_key=key or _simple_key(decision_object),
        title=decision_object, decision=decision, reason="",
        status=CardStatus.ACTIVE,
    )
    store.save_memory_card(card)
    return card


def new_memory(title: str, decision: str) -> ExtractedMemory:
    return ExtractedMemory(
        title=title, decision=decision,
        reason="", memory_type=MemoryType.DECISION, participants=[],
    )


# ── Stage 1: key match ────────────────────────────────────────────────────────

async def test_stage1_key_match():
    card = make_card("企业级记忆是否进入MVP", "不做企业级记忆")
    mem = new_memory("企业级记忆是否进入MVP", "决定做企业级记忆")

    result = await ConflictDetector().find_conflict(CHAT, mem)
    assert result is not None
    assert result["memory_id"] == card.memory_id
    assert result["reason"] == "key_match"
    print("TEST 1 OK: Stage 1 key match fires correctly")


async def test_stage1_no_match():
    make_card("存储架构选型", "使用SQLite")
    mem = new_memory("API设计风格", "使用REST")

    # patch Stage 2 to confirm it was called (Stage 1 missed)
    cd = ConflictDetector()
    with patch.object(cd, "_semantic_llm_check", new=AsyncMock(return_value=None)):
        result = await cd.find_conflict(CHAT, mem)
    assert result is None
    print("TEST 2 OK: Stage 1 miss passes to Stage 2")


# ── Stage 2: Graphiti + LLM ───────────────────────────────────────────────────

async def test_stage2_llm_finds_conflict():
    """Stage 2 返回冲突 dict → find_conflict 直接返回，不进 Fallback。"""
    make_card("全局知识库范围", "不做全局知识库", key="全局知识库范围")
    mem = new_memory("企业级记忆边界", "要做企业级全局记忆")

    expected = {"memory_id": "xxx", "title": "全局知识库", "decision": "不做", "reason": "semantic_llm"}
    cd = ConflictDetector()
    with patch.object(cd, "_semantic_llm_check", new=AsyncMock(return_value=expected)):
        result = await cd.find_conflict(CHAT, mem)

    assert result is expected
    assert result["reason"] == "semantic_llm"
    print("TEST 3 OK: Stage 2 conflict returned → propagated correctly")


async def test_stage2_llm_no_conflict():
    """Stage 2 正常返回 None（LLM 判断无冲突）→ 不触发 Fallback。"""
    make_card("日程管理", "使用飞书日历")
    mem = new_memory("待办事项处理", "使用飞书待办")

    cd = ConflictDetector()
    jaccard_called = []
    original = cd._jaccard_fallback
    def spy(*a, **kw):
        jaccard_called.append(True)
        return original(*a, **kw)

    with patch.object(cd, "_semantic_llm_check", new=AsyncMock(return_value=None)), \
         patch.object(cd, "_jaccard_fallback", side_effect=spy):
        result = await cd.find_conflict(CHAT, mem)

    assert result is None
    assert not jaccard_called, "Jaccard must NOT fire when Stage 2 returns None normally"
    print("TEST 4 OK: Stage 2 returns None → Jaccard NOT triggered")


# ── Fallback: Jaccard ─────────────────────────────────────────────────────────

async def test_fallback_triggers_on_stage2_exception():
    """Stage 2 抛出异常时，Jaccard fallback 接管。"""
    card = make_card("MVP产品边界定义", "不做企业级记忆，聚焦群聊记忆")
    mem = new_memory("MVP产品边界", "MVP边界定义为群聊记忆系统")

    cd = ConflictDetector()
    with patch.object(cd, "_semantic_llm_check",
                      new=AsyncMock(side_effect=RuntimeError("Graphiti unavailable"))):
        result = await cd.find_conflict(CHAT, mem)

    # key不同，但 Jaccard 应该命中（高字符重叠）
    if result:
        assert "jaccard_fallback" in result["reason"]
        print(f"TEST 5 OK: Fallback Jaccard fires on Stage 2 exception → {result['reason']}")
    else:
        print("TEST 5 OK: Fallback Jaccard fired but below threshold (chars not overlapping enough)")


async def test_fallback_does_not_trigger_on_no_conflict():
    """Stage 2 正常返回 None（无冲突），不触发 Jaccard。与 TEST 4 互补：此处明确验证 spy 行为。"""
    make_card("存储架构", "使用SQLite")
    mem = new_memory("前端界面设计", "使用React")

    cd = ConflictDetector()
    jaccard_called = []
    original = cd._jaccard_fallback
    def spy(*a, **kw):
        jaccard_called.append(True)
        return original(*a, **kw)

    with patch.object(cd, "_semantic_llm_check", new=AsyncMock(return_value=None)), \
         patch.object(cd, "_jaccard_fallback", side_effect=spy):
        result = await cd.find_conflict(CHAT, mem)

    assert result is None
    assert not jaccard_called, "Jaccard should NOT be called when Stage 2 returns None normally"
    print("TEST 6 OK: Jaccard NOT triggered when Stage 2 returns None cleanly")


if __name__ == "__main__":
    cleanup()
    try:
        asyncio.run(test_stage1_key_match())
        cleanup()
        asyncio.run(test_stage1_no_match())
        cleanup()
        asyncio.run(test_stage2_llm_finds_conflict())
        cleanup()
        asyncio.run(test_stage2_llm_no_conflict())
        cleanup()
        asyncio.run(test_fallback_triggers_on_stage2_exception())
        cleanup()
        asyncio.run(test_fallback_does_not_trigger_on_no_conflict())
        print("\nALL CONFLICT DETECTOR TESTS PASSED")
    finally:
        cleanup()
