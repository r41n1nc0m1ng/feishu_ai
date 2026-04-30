"""P1-1 集成测试：MemoryRelation 持久化 + get_version_chain"""
import asyncio
import sqlite3
import uuid

from memory import store
from memory.schemas import (
    CardStatus, MemoryCard, MemoryRelation, MemoryRelationType
)

CHAT = "p11_test"


def cleanup():
    conn = sqlite3.connect(store.DB_PATH)
    conn.execute("DELETE FROM memory_cards WHERE chat_id=?", (CHAT,))
    conn.execute("DELETE FROM memory_relations WHERE chat_id=?", (CHAT,))
    conn.commit()
    conn.close()


def test_save_and_load_relation():
    r = MemoryRelation(
        chat_id=CHAT,
        source_id=str(uuid.uuid4()),
        target_id=str(uuid.uuid4()),
        relation_type=MemoryRelationType.SUPERSEDES,
    )
    store.save_relation(r)

    by_card = store.load_relations_by_card(r.source_id)
    assert len(by_card) == 1 and by_card[0].relation_id == r.relation_id
    print("TEST 1 OK: save_relation + load_relations_by_card")

    by_chat = store.load_relations_by_chat(CHAT)
    assert any(x.relation_id == r.relation_id for x in by_chat)
    print("TEST 2 OK: load_relations_by_chat")


async def test_version_chain():
    old_id = str(uuid.uuid4())
    new_id = str(uuid.uuid4())

    store.save_memory_card(MemoryCard(
        memory_id=old_id, chat_id=CHAT,
        decision_object="议题X", title="旧版本",
        decision="旧决策", reason="",
        status=CardStatus.DEPRECATED,
    ))
    store.save_memory_card(MemoryCard(
        memory_id=new_id, chat_id=CHAT,
        decision_object="议题X", title="新版本",
        decision="新决策", reason="",
        status=CardStatus.ACTIVE,
        supersedes_memory_id=old_id,
    ))

    from memory.retriever import MemoryRetriever
    chain = await MemoryRetriever().get_version_chain(new_id)

    assert len(chain) == 2, f"expected 2, got {len(chain)}"
    assert chain[0].memory_id == new_id
    assert chain[1].memory_id == old_id
    print(f"TEST 3 OK: get_version_chain → {[c.title for c in chain]}")


if __name__ == "__main__":
    cleanup()
    try:
        test_save_and_load_relation()
        asyncio.run(test_version_chain())
        print("\nALL P1-1 TESTS PASSED")
    finally:
        cleanup()
