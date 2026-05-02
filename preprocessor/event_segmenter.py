"""
事件切分模块（对应需求文档 4.5 Event Segmentation 层）。

P0 策略（默认）：时间窗口 + 消息数量双阈值切分，不依赖外部服务。
P1-6 策略（可选）：Embedding 余弦相似度切分，正确识别话题边界。

切换方式：设置环境变量 SEGMENTER_STRATEGY=semantic（默认 time）。
语义切分任意步骤失败时自动回退到 P0 行为，不影响主链。
"""
import logging
import math
import os
from typing import List, Optional

import httpx

from memory.schemas import EvidenceBlock, EvidenceMessage, FetchBatch

logger = logging.getLogger(__name__)

# ── P0 阈值（两种策略均适用的硬上限，可通过环境变量覆盖）──────────────────────
BLOCK_GAP_SECONDS = int(os.getenv("BLOCK_GAP_SECONDS", "300"))
# 相邻消息时间间隔超过此值强制切块（默认 300s）。
# 测试时发言间隔较短可设为 90 或 60。
MAX_BLOCK_MESSAGES = int(os.getenv("MAX_BLOCK_MESSAGES", "30"))
# 单块消息数上限，超过则强制截断。

# ── P1-6 语义切分参数（可通过环境变量覆盖）────────────────────────────────────
SEMANTIC_THRESHOLD = float(os.getenv("SEMANTIC_THRESHOLD", "0.50"))
# 新消息与当前 block 中心向量余弦相似度低于此值视为话题切换。
# 0.50 对同项目不同议题（语言选型 vs 评审规则）更有效；
# 若误切过多可调高至 0.60。
MIN_BLOCK_MESSAGES = int(os.getenv("MIN_BLOCK_MESSAGES", "3"))
# 至少积累此数量的消息后才允许语义切分，避免首条消息误触发。


# ── 公共入口 ──────────────────────────────────────────────────────────────────

def segment(batch: FetchBatch) -> List[EvidenceBlock]:
    """
    P0 同步入口：时间窗口 + 消息数量双阈值切分。
    行为与原实现完全一致，不依赖外部服务，现有测试零改动。
    """
    return _segment_time(batch)


async def segment_async(batch: FetchBatch) -> List[EvidenceBlock]:
    """
    P1-6 异步入口：按 SEGMENTER_STRATEGY 分发。
      time（默认）→ 与 segment() 行为完全一致
      semantic    → Embedding 语义相似度切分，失败自动回退到 time
    """
    strategy = os.getenv("SEGMENTER_STRATEGY", "time").strip().lower()
    if strategy == "semantic":
        try:
            return await _segment_semantic(batch)
        except Exception:
            logger.exception("语义切分异常，回退到 P0 时间切分 | chat=%s", batch.chat_id)
    return _segment_time(batch)


# ── P0 时间切分（同步）────────────────────────────────────────────────────────

def _segment_time(batch: FetchBatch) -> List[EvidenceBlock]:
    """P0 双阈值切分：与原 segment() 逻辑完全一致。"""
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


# ── P1-6 语义切分（异步）────────────────────────────────────────────────────

async def _segment_semantic(batch: FetchBatch) -> List[EvidenceBlock]:
    """
    Embedding 语义相似度切分。

    算法：
    1. 为每条消息获取 embedding（失败则置 None）
    2. 维护当前 block 的 embedding 中心向量
    3. 当新消息与当前中心的余弦相似度 < SEMANTIC_THRESHOLD 且已积累
       足够消息（>= MIN_BLOCK_MESSAGES）时，关闭当前块
    4. P0 时间间隔 / 消息数量阈值始终作为强制切块的硬上限
    5. 全部 embedding 失败时整体回退到 _segment_time
    """
    if not batch.messages:
        return []

    messages = sorted(batch.messages, key=lambda m: m.timestamp)

    embeddings: List[Optional[List[float]]] = []
    for msg in messages:
        embeddings.append(await _embed_safe(msg.text))

    if all(e is None for e in embeddings):
        logger.warning("全部 embedding 失败，回退到 P0 时间切分 | chat=%s", batch.chat_id)
        return _segment_time(batch)

    blocks: List[EvidenceBlock] = []
    current_msgs: List[EvidenceMessage] = []
    current_embs: List[List[float]] = []

    for msg, emb in zip(messages, embeddings):
        if not current_msgs:
            current_msgs.append(msg)
            if emb is not None:
                current_embs.append(emb)
            continue

        gap = (msg.timestamp - current_msgs[-1].timestamp).total_seconds()
        force_cut = gap > BLOCK_GAP_SECONDS or len(current_msgs) >= MAX_BLOCK_MESSAGES

        semantic_cut = False
        if (
            not force_cut
            and emb is not None
            and current_embs
            and len(current_msgs) >= MIN_BLOCK_MESSAGES
        ):
            sim = _cosine(emb, _centroid(current_embs))
            semantic_cut = sim < SEMANTIC_THRESHOLD
            if semantic_cut:
                logger.debug(
                    "语义边界 | chat=%s sim=%.3f threshold=%.2f text=%s",
                    batch.chat_id, sim, SEMANTIC_THRESHOLD, msg.text[:40],
                )

        if force_cut or semantic_cut:
            blocks.append(_make_block(batch.chat_id, current_msgs))
            current_msgs = [msg]
            current_embs = [emb] if emb is not None else []
        else:
            current_msgs.append(msg)
            if emb is not None:
                current_embs.append(emb)

    if current_msgs:
        blocks.append(_make_block(batch.chat_id, current_msgs))

    logger.info(
        "语义切分完成 | chat=%s 消息数=%d 块数=%d（时间切分会得到 %d 块）",
        batch.chat_id, len(messages), len(blocks),
        len(_segment_time(batch)),
    )
    return blocks


# ── Embedding 工具 ────────────────────────────────────────────────────────────

async def _embed_safe(text: str) -> Optional[List[float]]:
    """获取 embedding，失败返回 None，不向上抛出异常。"""
    try:
        return await _embed(text)
    except Exception as e:
        logger.warning("Embedding 失败: %s | text=%.40s", e, text)
        return None


async def _embed(text: str) -> List[float]:
    """
    调用 Ollama 或 OpenAI-compatible 接口获取文本 embedding。
    Ollama: POST /api/embeddings  {"model": ..., "prompt": ...}
    OpenAI: POST /v1/embeddings   {"model": ..., "input": ...}
    """
    provider = os.getenv("MODEL_PROVIDER", "ollama").strip().lower()

    if provider == "openai" or os.getenv("OPENAI_API_KEY"):
        api_key = os.getenv("OPENAI_API_KEY", "sk-placeholder")
        base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        model = os.getenv("OPENAI_EMBED_MODEL", os.getenv("EMBED_MODEL", "text-embedding-ada-002"))
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            resp = await client.post(
                f"{base_url}/embeddings",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": model, "input": text},
            )
            resp.raise_for_status()
            return resp.json()["data"][0]["embedding"]
    else:
        ollama_url = os.getenv("OLLAMA_URL", "http://localhost:11434")
        model = os.getenv("EMBED_MODEL", "nomic-embed-text")
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            resp = await client.post(
                f"{ollama_url}/api/embeddings",
                json={"model": model, "prompt": text},
            )
            resp.raise_for_status()
            return resp.json()["embedding"]


# ── 向量运算 ──────────────────────────────────────────────────────────────────

def _centroid(embeddings: List[List[float]]) -> List[float]:
    """计算 embedding 列表的分量均值（中心向量）。"""
    if not embeddings:
        return []
    dim = len(embeddings[0])
    result = [0.0] * dim
    for emb in embeddings:
        for j, v in enumerate(emb):
            result[j] += v
    n = len(embeddings)
    return [v / n for v in result]


def _cosine(a: List[float], b: List[float]) -> float:
    """余弦相似度，输入为等长非空向量。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if not norm_a or not norm_b:
        return 0.0
    return dot / (norm_a * norm_b)


def _make_block(chat_id: str, messages: List[EvidenceMessage]) -> EvidenceBlock:
    return EvidenceBlock(
        chat_id=chat_id,
        start_time=messages[0].timestamp,
        end_time=messages[-1].timestamp,
        messages=messages,
    )
