"""
P1-6 事件切分测试

1. 原有时间切分测试（通过 segment() 同步接口）—— 行为不变
2. 语义切分逻辑测试（mock embedding，验证相似度判断）
3. 降级测试（embedding 全失败时回退到时间切分）
4. 向量工具单元测试

运行：conda run -n feishu python scripts/test_p16.py
"""
import asyncio
import os
import sys
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory.schemas import EvidenceMessage, FetchBatch
from preprocessor.event_segmenter import (
    BLOCK_GAP_SECONDS,
    MAX_BLOCK_MESSAGES,
    MIN_BLOCK_MESSAGES,
    SEMANTIC_THRESHOLD,
    _centroid,
    _cosine,
    _segment_time,
    segment,
    segment_async,
)

BASE = datetime(2026, 5, 1, 10, 0, 0, tzinfo=timezone.utc)
CHAT = "p16_test"


def _msg(offset_s: int, text: str = "内容") -> EvidenceMessage:
    return EvidenceMessage(
        message_id=f"msg_{offset_s}",
        sender_id="u1",
        sender_name="A",
        timestamp=BASE + timedelta(seconds=offset_s),
        text=text,
    )


def _batch(messages: list) -> FetchBatch:
    return FetchBatch(
        chat_id=CHAT,
        fetch_start=messages[0].timestamp if messages else BASE,
        fetch_end=messages[-1].timestamp if messages else BASE,
        messages=messages,
    )


# ── 1. 原有时间切分接口回归 ────────────────────────────────────────────────────
# segment() 和 _segment_time() 行为完全一致，与旧版本无差异

def test_time_empty():
    assert segment(FetchBatch(chat_id=CHAT, fetch_start=BASE, fetch_end=BASE, messages=[])) == []
    print("PASS  time: 空批次返回空列表")


def test_time_single_message():
    blocks = segment(_batch([_msg(0)]))
    assert len(blocks) == 1 and len(blocks[0].messages) == 1
    print("PASS  time: 单条消息 → 1 块")


def test_time_within_gap():
    blocks = segment(_batch([_msg(0), _msg(60), _msg(120)]))
    assert len(blocks) == 1
    print("PASS  time: 2 分钟内 → 1 块")


def test_time_gap_split():
    msgs = [_msg(0, "第一段"), _msg(BLOCK_GAP_SECONDS + 1, "第二段")]
    blocks = segment(_batch(msgs))
    assert len(blocks) == 2
    assert blocks[0].messages[0].text == "第一段"
    assert blocks[1].messages[0].text == "第二段"
    print("PASS  time: 超时间阈值 → 2 块")


def test_time_max_messages():
    msgs = [_msg(i * 10) for i in range(MAX_BLOCK_MESSAGES + 1)]
    blocks = segment(_batch(msgs))
    assert len(blocks) > 1
    for b in blocks:
        assert len(b.messages) <= MAX_BLOCK_MESSAGES
    print(f"PASS  time: 超过 {MAX_BLOCK_MESSAGES} 条消息强制截断")


def test_time_timestamps():
    msgs = [_msg(0), _msg(100), _msg(200)]
    b = segment(_batch(msgs))[0]
    assert b.start_time == msgs[0].timestamp
    assert b.end_time == msgs[-1].timestamp
    print("PASS  time: block 时间戳正确")


def test_time_sort():
    msgs = [_msg(200), _msg(0), _msg(100)]
    blocks = segment(_batch(msgs))
    assert len(blocks) == 1
    assert blocks[0].messages[0].timestamp == BASE
    print("PASS  time: 乱序输入按时间排序")


# ── 2. 语义切分逻辑（mock embedding）────────────────────────────────────────

def _make_emb(topic_vec: list[float]) -> list[float]:
    """构造一个简化的 embedding（前几维代表 topic，其余补零到 dim=8）。"""
    dim = 8
    result = list(topic_vec) + [0.0] * (dim - len(topic_vec))
    return result[:dim]


# embedding 方案：
#   话题 A（讨论范围）：[1, 0, 0, 0, ...]
#   话题 B（讨论架构）：[0, 1, 0, 0, ...]
# 这两个向量余弦相似度 = 0.0 < SEMANTIC_THRESHOLD，必定触发切分
EMB_TOPIC_A = _make_emb([1.0, 0.0])
EMB_TOPIC_B = _make_emb([0.0, 1.0])
# 同一话题内的消息略有随机性，但仍高度相似
EMB_TOPIC_A2 = _make_emb([0.95, 0.05])
EMB_TOPIC_B2 = _make_emb([0.05, 0.95])


async def test_semantic_splits_different_topics():
    """不同话题（相似度≈0）在积累足够消息后应被切分。"""
    msgs = (
        [_msg(i * 10, "记忆范围讨论") for i in range(4)]
        + [_msg(40 + i * 10, "存储架构讨论") for i in range(4)]
    )
    emb_sequence = [EMB_TOPIC_A, EMB_TOPIC_A2, EMB_TOPIC_A, EMB_TOPIC_A2,
                    EMB_TOPIC_B, EMB_TOPIC_B2, EMB_TOPIC_B, EMB_TOPIC_B2]

    with patch.dict(os.environ, {"SEGMENTER_STRATEGY": "semantic",
                                  "SEMANTIC_THRESHOLD": "0.5",
                                  "MIN_BLOCK_MESSAGES": "3"}):
        with patch("preprocessor.event_segmenter._embed_safe",
                   new=AsyncMock(side_effect=emb_sequence)):
            blocks = await segment_async(_batch(msgs))

    assert len(blocks) >= 2, f"期望 >=2 块，实际 {len(blocks)} 块"
    assert all("范围" in m.text for m in blocks[0].messages)
    print(f"PASS  semantic: 不同话题切成 {len(blocks)} 块")


async def test_semantic_keeps_same_topic_together():
    """同一话题内的消息（高相似度）不应被切分。"""
    msgs = [_msg(i * 10, "记忆范围讨论") for i in range(6)]
    emb_sequence = [EMB_TOPIC_A, EMB_TOPIC_A2, EMB_TOPIC_A,
                    EMB_TOPIC_A2, EMB_TOPIC_A, EMB_TOPIC_A2]

    with patch.dict(os.environ, {"SEGMENTER_STRATEGY": "semantic",
                                  "SEMANTIC_THRESHOLD": "0.5",
                                  "MIN_BLOCK_MESSAGES": "3"}):
        with patch("preprocessor.event_segmenter._embed_safe",
                   new=AsyncMock(side_effect=emb_sequence)):
            blocks = await segment_async(_batch(msgs))

    assert len(blocks) == 1, f"同话题期望 1 块，实际 {len(blocks)} 块"
    print("PASS  semantic: 同话题消息聚合在 1 块")


async def test_semantic_min_block_guard():
    """少于 MIN_BLOCK_MESSAGES 条消息时不触发语义切分。"""
    msgs = [_msg(0, "话题A"), _msg(10, "话题B")]
    emb_sequence = [EMB_TOPIC_A, EMB_TOPIC_B]

    with patch.dict(os.environ, {"SEGMENTER_STRATEGY": "semantic",
                                  "SEMANTIC_THRESHOLD": "0.5",
                                  "MIN_BLOCK_MESSAGES": "3"}):
        with patch("preprocessor.event_segmenter._embed_safe",
                   new=AsyncMock(side_effect=emb_sequence)):
            blocks = await segment_async(_batch(msgs))

    assert len(blocks) == 1, f"消息数 < MIN_BLOCK_MESSAGES 时不应切分，实际 {len(blocks)} 块"
    print("PASS  semantic: MIN_BLOCK_MESSAGES 保护生效")


async def test_semantic_time_gap_still_cuts():
    """即使语义相似，时间间隔超阈值仍应强制切块。"""
    msgs = [_msg(0, "话题A"), _msg(BLOCK_GAP_SECONDS + 5, "话题A延续")]
    emb_sequence = [EMB_TOPIC_A, EMB_TOPIC_A]

    with patch.dict(os.environ, {"SEGMENTER_STRATEGY": "semantic",
                                  "SEMANTIC_THRESHOLD": "0.5",
                                  "MIN_BLOCK_MESSAGES": "1"}):
        with patch("preprocessor.event_segmenter._embed_safe",
                   new=AsyncMock(side_effect=emb_sequence)):
            blocks = await segment_async(_batch(msgs))

    assert len(blocks) == 2, f"时间 gap 应强制切块，实际 {len(blocks)} 块"
    print("PASS  semantic: 时间 gap 硬切块保留")


# ── 3. 降级测试 ───────────────────────────────────────────────────────────────

async def test_fallback_on_all_embed_failure():
    """所有 embedding 失败时，语义切分回退到时间切分，结果与 segment() 一致。"""
    gap = BLOCK_GAP_SECONDS + 10
    msgs = [_msg(0, "A"), _msg(gap, "B"), _msg(gap * 2, "C")]
    expected = segment(_batch(msgs))  # P0 时间切分结果

    with patch.dict(os.environ, {"SEGMENTER_STRATEGY": "semantic"}):
        with patch("preprocessor.event_segmenter._embed_safe",
                   new=AsyncMock(side_effect=[None, None, None])):
            blocks = await segment_async(_batch(msgs))

    assert len(blocks) == len(expected), (
        f"降级后块数应与时间切分一致：期望 {len(expected)}，实际 {len(blocks)}"
    )
    print(f"PASS  fallback: 全失败时回退到时间切分（{len(blocks)} 块）")


async def test_time_strategy_unchanged_via_segment_async():
    """SEGMENTER_STRATEGY=time 时，segment_async 与 segment 结果完全相同。"""
    msgs = [_msg(0), _msg(60), _msg(BLOCK_GAP_SECONDS + 1), _msg(BLOCK_GAP_SECONDS + 60)]
    expected = segment(_batch(msgs))

    with patch.dict(os.environ, {"SEGMENTER_STRATEGY": "time"}):
        actual = await segment_async(_batch(msgs))

    assert len(actual) == len(expected)
    for a, e in zip(actual, expected):
        assert len(a.messages) == len(e.messages)
    print("PASS  fallback: SEGMENTER_STRATEGY=time 时行为与 segment() 完全一致")


# ── 4. 向量工具单元测试 ────────────────────────────────────────────────────────

def test_cosine_identical():
    v = [1.0, 0.0, 0.0]
    assert abs(_cosine(v, v) - 1.0) < 1e-6
    print("PASS  cosine: 同向量相似度=1")


def test_cosine_orthogonal():
    assert abs(_cosine([1, 0], [0, 1])) < 1e-6
    print("PASS  cosine: 正交向量相似度=0")


def test_cosine_opposite():
    assert abs(_cosine([1, 0], [-1, 0]) - (-1.0)) < 1e-6
    print("PASS  cosine: 反向向量相似度=-1")


def test_centroid_single():
    emb = [1.0, 2.0, 3.0]
    c = _centroid([emb])
    assert c == emb
    print("PASS  centroid: 单向量中心 = 自身")


def test_centroid_two():
    c = _centroid([[1.0, 0.0], [0.0, 1.0]])
    assert abs(c[0] - 0.5) < 1e-6 and abs(c[1] - 0.5) < 1e-6
    print("PASS  centroid: 两向量均值正确")


def test_cosine_empty():
    assert _cosine([], []) == 0.0
    assert _cosine([1.0], []) == 0.0
    print("PASS  cosine: 空向量返回 0")


if __name__ == "__main__":
    print("=== P1-6 事件切分测试 ===\n")

    print("--- 1. 时间切分回归 ---")
    test_time_empty()
    test_time_single_message()
    test_time_within_gap()
    test_time_gap_split()
    test_time_max_messages()
    test_time_timestamps()
    test_time_sort()

    print("\n--- 2. 语义切分逻辑 ---")
    asyncio.run(test_semantic_splits_different_topics())
    asyncio.run(test_semantic_keeps_same_topic_together())
    asyncio.run(test_semantic_min_block_guard())
    asyncio.run(test_semantic_time_gap_still_cuts())

    print("\n--- 3. 降级测试 ---")
    asyncio.run(test_fallback_on_all_embed_failure())
    asyncio.run(test_time_strategy_unchanged_via_segment_async())

    print("\n--- 4. 向量工具 ---")
    test_cosine_identical()
    test_cosine_orthogonal()
    test_cosine_opposite()
    test_centroid_single()
    test_centroid_two()
    test_cosine_empty()

    print("\n" + "=" * 50)
    print("ALL P1-6 TESTS PASSED")
