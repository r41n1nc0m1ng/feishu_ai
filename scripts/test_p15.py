"""P1-5 集成测试：lifecycle + conflict_detector + version query routing"""
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
from memory.lifecycle import MemoryLifecycle
from memory.schemas import CardStatus, MemoryCard, MemoryType
from realtime.triggers import is_version_query, is_summary_query, is_source_query
from realtime.query_handler import render_version_reply

CHAT = "p15_test"


def cleanup():
    conn = sqlite3.connect(store.DB_PATH)
    conn.execute("DELETE FROM memory_cards WHERE chat_id=?", (CHAT,))
    conn.commit()
    conn.close()


def make_card(title: str, status=CardStatus.ACTIVE) -> MemoryCard:
    return MemoryCard(
        memory_id=str(uuid.uuid4()), chat_id=CHAT,
        decision_object=title, title=title,
        decision=f"{title}的决策", reason="理由",
        status=status,
    )


# ── lifecycle ─────────────────────────────────────────────────────────────────

async def test_deprecate():
    card = make_card("测试卡")
    store.save_memory_card(card)

    lc = MemoryLifecycle()
    ok = await lc.deprecate(CHAT, card.memory_id)
    assert ok, "deprecate should return True"

    loaded = store.load_memory_card(card.memory_id)
    assert loaded.status == CardStatus.DEPRECATED
    print("TEST 1 OK: deprecate marks card as Deprecated")


async def test_deprecate_idempotent():
    card = make_card("幂等卡", status=CardStatus.DEPRECATED)
    store.save_memory_card(card)
    lc = MemoryLifecycle()
    ok = await lc.deprecate(CHAT, card.memory_id)
    assert ok
    print("TEST 2 OK: deprecate is idempotent on already-deprecated card")


async def test_deprecate_wrong_chat():
    card = make_card("跨群卡")
    store.save_memory_card(card)
    lc = MemoryLifecycle()
    ok = await lc.deprecate("other_chat", card.memory_id)
    assert not ok, "should return False for wrong chat_id"
    loaded = store.load_memory_card(card.memory_id)
    assert loaded.status == CardStatus.ACTIVE
    print("TEST 3 OK: deprecate rejects wrong chat_id")


async def test_expire_chat_memories():
    from datetime import datetime, timezone, timedelta
    cards = [make_card(f"过期卡{i}") for i in range(3)]
    for c in cards:
        store.save_memory_card(c)

    lc = MemoryLifecycle()
    cutoff = datetime.now(timezone.utc) + timedelta(seconds=10)
    count = await lc.expire_chat_memories(CHAT, cutoff=cutoff)
    assert count == 3, f"expected 3 expired, got {count}"

    for c in cards:
        loaded = store.load_memory_card(c.memory_id)
        assert loaded.status == CardStatus.DEPRECATED
    print(f"TEST 4 OK: expire_chat_memories expired {count} cards")


# ── version query trigger ─────────────────────────────────────────────────────

def test_is_version_query():
    assert is_version_query("后来改了吗")
    assert is_version_query("这个决定有没有变过")
    assert is_version_query("最新版本是什么")
    assert not is_version_query("为什么不做企业级记忆")
    assert not is_version_query("整体方案是什么")
    print("TEST 5 OK: is_version_query patterns correct")


def test_trigger_priority():
    """来源 > 版本 > 整体 > 决策，互不重叠。"""
    assert is_source_query("原话在哪")
    assert not is_version_query("原话在哪")

    assert is_version_query("后来改了吗")
    assert not is_source_query("后来改了吗")
    assert not is_summary_query("后来改了吗")

    assert is_summary_query("整体方案是什么")
    assert not is_version_query("整体方案是什么")
    print("TEST 6 OK: trigger priorities don't overlap")


# ── render_version_reply ──────────────────────────────────────────────────────

def test_render_version_reply_single():
    card = make_card("唯一版本")
    reply = render_version_reply("测试", [card])
    assert "唯一版本的决策" in reply
    assert "没有发现历史更新记录" in reply
    print("TEST 7 OK: render_version_reply single card")


def test_render_version_reply_chain():
    new = make_card("新版本")
    old = make_card("旧版本")
    reply = render_version_reply("测试", [new, old])
    assert "新版本的决策" in reply
    assert "旧版本的决策" in reply
    assert "已被覆盖" in reply
    print("TEST 8 OK: render_version_reply version chain")


def test_render_version_reply_empty():
    reply = render_version_reply("测试", [])
    assert "没有查到" in reply
    print("TEST 9 OK: render_version_reply empty chain")


if __name__ == "__main__":
    cleanup()
    try:
        asyncio.run(test_deprecate())
        asyncio.run(test_deprecate_idempotent())
        asyncio.run(test_deprecate_wrong_chat())
        cleanup()
        asyncio.run(test_expire_chat_memories())
        cleanup()
        test_is_version_query()
        test_trigger_priority()
        test_render_version_reply_single()
        test_render_version_reply_chain()
        test_render_version_reply_empty()
        print("\nALL P1-5 TESTS PASSED")
    finally:
        cleanup()
