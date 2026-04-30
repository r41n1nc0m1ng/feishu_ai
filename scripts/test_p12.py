"""P1-2 集成测试：TopicSummary 生成、持久化、重建"""
import asyncio
import sqlite3
import uuid
from unittest.mock import AsyncMock, patch

from memory import store
from memory.schemas import CardStatus, MemoryCard, MemoryType, TopicSummary
from memory.topic_manager import TopicManager

CHAT = "p12_test"


def cleanup():
    conn = sqlite3.connect(store.DB_PATH)
    conn.execute("DELETE FROM memory_cards WHERE chat_id=?", (CHAT,))
    conn.execute("DELETE FROM topic_summaries WHERE chat_id=?", (CHAT,))
    conn.commit()
    conn.close()


def make_card(title: str, decision_object: str, memory_type=MemoryType.DECISION) -> MemoryCard:
    return MemoryCard(
        memory_id=str(uuid.uuid4()),
        chat_id=CHAT,
        decision_object=decision_object,
        title=title,
        decision=f"{title}的决策内容",
        reason=f"{title}的决策理由",
        memory_type=memory_type,
        status=CardStatus.ACTIVE,
    )


def test_save_and_load():
    t = TopicSummary(
        chat_id=CHAT, topic="MVP边界",
        summary="当前MVP只做群聊决策记忆",
        covered_memory_ids=["mem_1", "mem_2"],
    )
    store.save_topic_summary(t)
    loaded = store.load_topics_by_chat(CHAT)
    assert len(loaded) == 1
    assert loaded[0].topic == "MVP边界"
    assert loaded[0].covered_memory_ids == ["mem_1", "mem_2"]
    print("TEST 1 OK: save_topic_summary + load_topics_by_chat")


def test_delete():
    store.delete_topics_by_chat(CHAT)
    assert store.load_topics_by_chat(CHAT) == []
    print("TEST 2 OK: delete_topics_by_chat")


async def test_rebuild_skip_when_few_cards():
    """卡片数 < 2 时跳过重建。"""
    cards = [make_card("单张卡", "议题A")]
    store.save_memory_card(cards[0])
    tm = TopicManager()
    result = await tm.rebuild_topics(CHAT)
    assert result == []
    print("TEST 3 OK: rebuild skipped when cards < 2")


async def test_rebuild_with_mock_llm():
    """mock LLM，验证 rebuild_topics 完整流程。"""
    cards = [
        make_card("不做企业级记忆", "企业级记忆边界"),
        make_card("聚焦群聊记忆", "MVP产品范围"),
        make_card("使用SQLite存储", "存储架构"),
    ]
    for c in cards:
        store.save_memory_card(c)

    mock_llm_response = [
        {
            "topic": "MVP产品边界",
            "summary": "当前MVP聚焦群聊决策记忆，不做企业级记忆。",
            "covered_memory_ids": [cards[0].memory_id, cards[1].memory_id],
        },
        {
            "topic": "技术架构",
            "summary": "使用SQLite作为结构化真相源。",
            "covered_memory_ids": [cards[2].memory_id],
        },
    ]

    tm = TopicManager()
    # mock _call_llm 使测试不依赖真实 LLM
    with patch.object(tm, "_call_llm", new=AsyncMock(return_value=mock_llm_response)):
        summaries = await tm.rebuild_topics(CHAT)

    assert len(summaries) == 2, f"expected 2, got {len(summaries)}"
    topics = {s.topic for s in summaries}
    assert "MVP产品边界" in topics
    assert "技术架构" in topics

    # 验证持久化
    persisted = store.load_topics_by_chat(CHAT)
    assert len(persisted) == 2
    print(f"TEST 4 OK: rebuild_topics with mock LLM → {[s.topic for s in summaries]}")


async def test_get_topics():
    topics = await TopicManager().get_topics(CHAT)
    assert len(topics) == 2
    print("TEST 5 OK: get_topics returns persisted summaries")


async def test_rebuild_replaces_old():
    """第二次 rebuild 应替换旧 TopicSummary，不累积。"""
    cards = [make_card(f"卡片{i}", f"议题{i}") for i in range(3)]
    for c in cards:
        store.save_memory_card(c)

    new_response = [{"topic": "新主题", "summary": "新摘要",
                     "covered_memory_ids": [cards[0].memory_id]}]
    tm = TopicManager()
    with patch.object(tm, "_call_llm", new=AsyncMock(return_value=new_response)):
        summaries = await tm.rebuild_topics(CHAT)

    persisted = store.load_topics_by_chat(CHAT)
    assert len(persisted) == 1
    assert persisted[0].topic == "新主题"
    print("TEST 6 OK: rebuild replaces old summaries")


if __name__ == "__main__":
    cleanup()
    try:
        test_save_and_load()
        test_delete()
        asyncio.run(test_rebuild_skip_when_few_cards())
        cleanup()
        asyncio.run(test_rebuild_with_mock_llm())
        asyncio.run(test_get_topics())
        cleanup()
        asyncio.run(test_rebuild_replaces_old())
        print("\nALL P1-2 TESTS PASSED")
    finally:
        cleanup()
