"""
事件切分模块（对应需求文档 4.5 Event Segmentation 层）。

策略（SEGMENTER_STRATEGY 环境变量控制）：
  time     P0 默认：时间窗口 + 消息数量双阈值，不依赖外部服务。
  semantic P1-6：Embedding 余弦相似度切分，失败回退 time。
  llm      P1-6+：LLM 理解话题边界切分，失败回退 semantic，再回退 time。
"""
import json
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
SEMANTIC_THRESHOLD = float(os.getenv("SEMANTIC_THRESHOLD", "0.60"))
# 新消息与最近 MIN_BLOCK_MESSAGES 条消息质心的余弦相似度低于此值视为话题切换。
# 越高越容易切分：0.60 对"同项目不同议题"（语言选型 vs 评审规则 vs 截止日期）通常有效。
# 若同话题消息被误切可适当降低；若不同话题仍合并可适当升高（0.65~0.70）。
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
      time     → 与 segment() 行为完全一致
      semantic → Embedding 余弦相似度切分，失败回退 time
      llm      → LLM 话题理解切分，失败回退 semantic，再回退 time
    """
    strategy = os.getenv("SEGMENTER_STRATEGY", "time").strip().lower()
    if strategy == "llm":
        try:
            return await _segment_llm(batch)
        except Exception:
            logger.exception("LLM 切分异常，回退到语义切分 | chat=%s", batch.chat_id)
            try:
                return await _segment_semantic(batch)
            except Exception:
                logger.exception("语义切分异常，回退到 P0 时间切分 | chat=%s", batch.chat_id)
    elif strategy == "semantic":
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
    First-anchor 语义切分：以每个 block 第一条消息的 embedding 作为话题锚点，
    新消息与锚点余弦相似度低于 SEMANTIC_THRESHOLD 时切块。

    为什么不用质心（centroid）：
      短确认句（"好""定了""就这样"）与多种话题的相似度都很高（nomic-embed-text 实测 0.67-0.80），
      一旦混入质心就会使质心漂移到通用空间，导致后续所有消息都无法触发切分。
      锚点固定在 block 第一条消息，不受后续消息污染。

    流程：
    1. 为每条消息获取 embedding（失败则置 None）
    2. 每个 block 记录第一条消息的 embedding 作为话题锚点
    3. 当新消息与锚点相似度 < SEMANTIC_THRESHOLD 且已积累 >= MIN_BLOCK_MESSAGES 条时切块
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
    anchor_emb: Optional[List[float]] = None   # 当前 block 第一条消息的 embedding

    for msg, emb in zip(messages, embeddings):
        if not current_msgs:
            current_msgs.append(msg)
            anchor_emb = emb   # 锚点 = block 第一条消息
            continue

        gap = (msg.timestamp - current_msgs[-1].timestamp).total_seconds()
        force_cut = gap > BLOCK_GAP_SECONDS or len(current_msgs) >= MAX_BLOCK_MESSAGES

        semantic_cut = False
        if (
            not force_cut
            and emb is not None
            and anchor_emb is not None
            and len(current_msgs) >= MIN_BLOCK_MESSAGES
        ):
            sim = _cosine(emb, anchor_emb)
            semantic_cut = sim < SEMANTIC_THRESHOLD
            if semantic_cut:
                logger.debug(
                    "语义边界(first-anchor) | chat=%s sim=%.3f threshold=%.2f text=%s",
                    batch.chat_id, sim, SEMANTIC_THRESHOLD, msg.text[:40],
                )

        if force_cut or semantic_cut:
            blocks.append(_make_block(batch.chat_id, current_msgs))
            current_msgs = [msg]
            anchor_emb = emb   # 新 block 的锚点
        else:
            current_msgs.append(msg)

    if current_msgs:
        blocks.append(_make_block(batch.chat_id, current_msgs))

    logger.info(
        "语义切分完成 | chat=%s 消息数=%d 块数=%d（时间切分会得到 %d 块）",
        batch.chat_id, len(messages), len(blocks),
        len(_segment_time(batch)),
    )
    return blocks


# ── LLM 切分（异步）─────────────────────────────────────────────────────────

_LLM_SEGMENT_PROMPT = """\
你是一个群聊话题切分助手。以下是一批按时间排列的群聊消息，请识别话题边界并分组。

【消息列表】
{messages}

【切分规则】
- 同一议题的讨论（包括提问、回应、确认）放在同一组
- 明显切换到新讨论（人员/时间/技术方向变化）时要新建一组
- 同一议题下的细节讨论也需要新建一组（从实现细节a切换到实现细节b）
- 每组最少 {min_block} 条，最多 {max_block} 条
- 时间间隔超过 {gap_seconds} 秒的消息强制分组

【输出规则】只返回 JSON，不要其他内容：
{{"groups": [[0,1,2,3], [4,5,6], ...]}}
每个数组是一组消息的下标（从 0 开始）。必须覆盖全部消息，不得遗漏。
"""


async def _segment_llm(batch: FetchBatch) -> List[EvidenceBlock]:
    """
    LLM 话题理解切分：把消息列表交给 LLM，由其判断话题边界并分组。
    优点：理解上下文和语义转折，不受短确认句干扰。
    失败时由调用方回退到 _segment_semantic。
    """
    if not batch.messages:
        return []

    messages = sorted(batch.messages, key=lambda m: m.timestamp)

    lines = [
        f"[{i}] {m.sender_name or m.sender_id} {m.timestamp.strftime('%H:%M')}：{m.text}"
        for i, m in enumerate(messages)
    ]
    prompt = _LLM_SEGMENT_PROMPT.format(
        messages="\n".join(lines),
        min_block=MIN_BLOCK_MESSAGES,
        max_block=MAX_BLOCK_MESSAGES,
        gap_seconds=BLOCK_GAP_SECONDS,
    )

    raw = await _call_llm_segment(prompt)
    if not raw:
        logger.warning("LLM 切分无有效返回，回退语义切分 | chat=%s", batch.chat_id)
        return await _segment_semantic(batch)

    groups = raw.get("groups")
    if not isinstance(groups, list) or not groups:
        logger.warning("LLM 切分 groups 字段无效，回退语义切分 | chat=%s raw=%s", batch.chat_id, raw)
        return await _segment_semantic(batch)

    # 验证索引完备性：所有消息必须被覆盖且不重复
    all_indices = [idx for g in groups for idx in (g if isinstance(g, list) else [])]
    n = len(messages)
    if sorted(all_indices) != list(range(n)):
        logger.warning(
            "LLM 切分索引不完备（期望 0-%d，实际 %s），回退语义切分 | chat=%s",
            n - 1, sorted(set(all_indices)), batch.chat_id,
        )
        return await _segment_semantic(batch)

    blocks: List[EvidenceBlock] = []
    for group in groups:
        if not isinstance(group, list) or not group:
            continue
        group_msgs = [messages[i] for i in group if 0 <= i < n]
        if group_msgs:
            blocks.append(_make_block(batch.chat_id, group_msgs))

    logger.info(
        "LLM 切分完成 | chat=%s 消息数=%d 块数=%d",
        batch.chat_id, n, len(blocks),
    )
    return blocks if blocks else await _segment_semantic(batch)


async def _call_llm_segment(prompt: str) -> Optional[dict]:
    """调用 LLM 获取切分结果，优先级：DeepSeek → OpenAI → Ollama。"""
    try:
        if os.getenv("DEEPSEEK_API_KEY"):
            return await _call_openai_compat(
                prompt,
                api_key=os.getenv("DEEPSEEK_API_KEY", ""),
                base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
                model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
            )
        if os.getenv("OPENAI_API_KEY") or os.getenv("MODEL_PROVIDER", "").lower() == "openai":
            return await _call_openai_compat(
                prompt,
                api_key=os.getenv("OPENAI_API_KEY", ""),
                base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
                model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            )
        return await _call_ollama_segment(prompt)
    except Exception as e:
        logger.error("LLM 切分调用失败: %s", e)
        return None


async def _call_openai_compat(prompt: str, api_key: str, base_url: str, model: str) -> Optional[dict]:
    async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
        resp = await client.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
            },
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        return json.loads(content)


async def _call_ollama_segment(prompt: str) -> Optional[dict]:
    ollama_url = os.getenv("OLLAMA_URL", "http://localhost:11434")
    model = os.getenv("LOCAL_MODEL", "qwen2.5:7b")
    async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
        resp = await client.post(
            f"{ollama_url}/api/generate",
            json={"model": model, "prompt": prompt, "stream": False, "format": "json"},
        )
        resp.raise_for_status()
        return json.loads(resp.json().get("response", "{}"))


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
