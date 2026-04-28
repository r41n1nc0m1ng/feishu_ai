"""
离线事件切分模块（对应需求文档 4.5 Event Segmentation 层）。

P0 策略：时间窗口 + 消息数量双阈值切分，不依赖 LLM。
后续可替换为 embedding 相似度或 LLM 边界判断，接口不变。
"""
from typing import List

from memory.schemas import EvidenceBlock, EvidenceMessage, FetchBatch

# 相邻消息时间间隔超过此值视为新事件块的开始
BLOCK_GAP_SECONDS = 300     # 5 分钟
# 单块最大消息数，超过则强制截断
MAX_BLOCK_MESSAGES = 30


def segment(batch: FetchBatch) -> List[EvidenceBlock]:
    """
    将 FetchBatch 中的消息按事件边界切分为若干 EvidenceBlock。

    规则（P0）：
    - 相邻消息时间间隔 > BLOCK_GAP_SECONDS → 关闭当前块，开启新块
    - 当前块消息数达到 MAX_BLOCK_MESSAGES → 强制关闭，开启新块
    - 批次结束时关闭最后一个块
    """
    if not batch.messages:
        return []

    messages = sorted(batch.messages, key=lambda m: m.timestamp)
    blocks: List[EvidenceBlock] = []
    current: List[EvidenceMessage] = []

    for msg in messages:
        if current:
            gap = (msg.timestamp - current[-1].timestamp).total_seconds()
            if gap > BLOCK_GAP_SECONDS or len(current) >= MAX_BLOCK_MESSAGES:
                blocks.append(_make_block(batch.chat_id, current))
                current = []
        current.append(msg)

    if current:
        blocks.append(_make_block(batch.chat_id, current))

    return blocks


def _make_block(chat_id: str, messages: List[EvidenceMessage]) -> EvidenceBlock:
    return EvidenceBlock(
        chat_id=chat_id,
        start_time=messages[0].timestamp,
        end_time=messages[-1].timestamp,
        messages=messages,
    )
