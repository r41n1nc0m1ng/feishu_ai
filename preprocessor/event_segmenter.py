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
from memory.topics import flat_list as _topic_flat_list, format_for_prompt as _topics_for_prompt

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

_LLM_SEGMENT_PROMPT_old = """\
你是一个群聊决策切分助手。以下是一批按时间排列的群聊消息，请按【决策点】边界分组，并为每组标注议题。

【消息列表】
{messages}

【核心原则：每个独立的决策点单独成一组】
即使前后讨论的都是同一个项目，只要决策主题不同就必须切分。
示例（错误）：把"要不要做完整ATS"和"MVP不输出淘汰结论"放在同一组
示例（正确）：两者是两个不同的决策点，必须分成两组


【切分规则】
- 当讨论从一个决策问题（A）转向另一个决策问题（B）时，立即新建一组，哪怕两者属于同一项目
- 同一决策问题的提问、讨论、确认、收尾放同一组。
- 尽快在前后文联系不大的情况下切分，尽量多切。
- 纯打招呼、离题闲聊、"好的"/"收到"等无实质内容的消息，归入前一组, 如果闲聊内容大于2条，则单独归一组。
- 时间间隔超过 {gap_seconds} 秒时强制分组
- 每组最少 {min_block} 条

【输出规则】只返回 JSON，不要其他内容：
{{"groups": [[0,1,2,3], [4,5,6], ...], "topics": ["大方向-小方向", "大方向-小方向", ...]}}
- groups：每个数组是一组消息的下标（从 0 开始），必须覆盖全部 {total} 条消息，不得遗漏
- topics：与 groups 等长，每项对应该组的议题，从上方列表原文选取
"""

# sy自定义的prompt
_LLM_SEGMENT_PROMPT = """\
你是群聊消息切分助手。你的任务是将一批群聊消息切分成若干"决策块"，每个决策块对应一个独立的讨论主题/决策单元，供下游提炼成记忆卡片。

═══════════════════════════════════════
输入
═══════════════════════════════════════
消息列表（每条带编号）：
{messages}

格式示例：
[1] 张三 14:32: 我先说下范围...
[2] 李四 14:33: 同意，完整 ATS...
...

═══════════════════════════════════════
切分原则
═══════════════════════════════════════

【核心原则】
按"决策完成度"切分，而非按"话题相似度"切分。
同一个大项目下可能有多个子决策（范围/UI/字段/命名），它们必须切开。
判断标准：每个块应能独立提炼出一个可一句话描述的结论或讨论焦点。

【块的硬约束】
- 每块消息数：3-10 条（连续短发言可合并计数）
- 单条系统消息（飞书卡片/直播推送/机器人消息）单独成块，标记为 noise
- 元对话单独成块（如"刚才那个忽略"、"我继续看X"），标记为 noise

═══════════════════════════════════════
切分信号（按强弱排序）
═══════════════════════════════════════

【强信号 A：收口语（在该消息之后切】
出现以下表达，意味着前面的讨论已收口，应在此消息**之后**切分：
- 收口词："那定一下"、"那MVP就是"、"那就这样"、"先按X做"、"统一叫X"、"OK就这么定"
- 任务接手："我就按X设计/做"、"我记一下"、"我去X"、"后端/前端我来X"
- 决议复述：用"不X，只Y"句式重复总结的句子（如"MVP不输出自动淘汰，只输出辅助判断"）

【强信号 B：议题开启句（在该消息之前切】
出现以下表达，意味着新议题开启，应在此消息**之前**切分：
- 询问开启："X先定哪些？"、"X怎么办？"、"X要不要Y？"
- 列举开启："还有X问题"、"另外X"、"X方面呢"
- 维度切换关键词："UI 上"、"后端"、"还有"、"另外"+ 新名词

【中信号 C：维度切换】
即使没有显式开启句，若话题从一个维度跳到另一个维度，也应切分：
- 范围/边界 → 具体交付物
- 后端实现 → 前端展示
- 功能讨论 → 非功能讨论（隐私/安全/性能）
- 字段定义 → UI呈现

【强信号 D：系统消息/元对话】
- 飞书卡片、直播提醒、链接预览等结构化推送：单独成 noise 块
- "刚才那个忽略"、"回到XX"等元对话：单独成 noise 块

═══════════════════════════════════════
切分判定流程
═══════════════════════════════════════

对每相邻两条消息 [n] 和 [n+1]，按以下顺序检查是否切分：

1. [n] 或 [n+1] 是否为系统消息/元对话？→ 切，单独成块
2. [n] 是否含强信号 A（收口语）？→ 切
3. [n+1] 是否含强信号 B（议题开启）？→ 切
4. [n] 和 [n+1] 是否触发信号 C（维度切换）？→ 切
5. 当前块是否已达 10 条上限？→ 强制切，找最近的弱边界

无任何信号 → 不切，归入同一块

═══════════════════════════════════════
PROGRESS 块的特殊处理
═══════════════════════════════════════
若一个块内出现"下一轮继续"、"先不当结论"、"再讨论"，
不要把它和后续讨论合并——它是一个独立的 PROGRESS 块，单独成块。

═══════════════════════════════════════
输出格式
═══════════════════════════════════════
只返回 JSON，不要其他内容。

{{
  "chunks": [
    {{
      "chunk_id": 1,
      "message_ids": [1, 2, 3, 4, 5],
      "block_type": "decision" | "progress" | "noise",
      "boundary_signal": "触发本块结束的信号，简述（如：收口语-那定一下；议题开启-还有隐私问题）",
      "one_line_summary": "用一句话概括本块讨论焦点，写不出来则说明粒度过大需重切"
    }},
    ...
  ]
}}

═══════════════════════════════════════
自检要求（输出前内部检查，不要输出检查过程）
═══════════════════════════════════════
1. 每个块是否能写出 one_line_summary？写不出 → 拆分
2. 相邻两块的 one_line_summary 是否高度相似？相似 → 合并
3. 块内消息数是否在 3-10 条范围？超出 → 重新切
4. 系统消息和元对话是否被正确隔离为 noise？

═══════════════════════════════════════
完整示例（学习这个例子的切分粒度）
═══════════════════════════════════════
输入消息（节选）：
[1] A: 我先说下范围，AI自动晒简历这个项目别做成完整招聘系统
[2] B: 同意，完整ATS要做职位发布、候选人池、面试流转
[3] A: 我们先把招聘官最痛的一步拿出来：JD和简历对不上
[4] C: 那MVP就是JD-简历匹配分析，对吧？
[5] A: 对，先不做完整ATS，只做初筛分析
[6] B: 这个边界我觉得可以定，Demo就是上传JD和简历，AI出报告
[7] A: 页面也别复杂，一个JD输入区，一个简历上传区
[8] C: 结果里最好不要写'淘汰'，太敏感
[9] B: 同意，可以写'建议人工复核'
[10] A: 那定一下：MVP不输出自动淘汰结论，只输出辅助判断

正确切分：
{{
  "chunks": [
    {{
      "chunk_id": 1,
      "message_ids": [1,2,3,4,5],
      "block_type": "decision",
      "boundary_signal": "收口语-对，先不做X，只做Y",
      "one_line_summary": "MVP范围限定为JD-简历匹配，不做完整ATS"
    }},
    {{
      "chunk_id": 2,
      "message_ids": [6,7,8,9,10],
      "block_type": "decision",
      "boundary_signal": "收口语-那定一下",
      "one_line_summary": "MVP输出边界：不输出自动淘汰，只做辅助判断"
    }}
  ]
}}

注意：消息[6][7]虽然属于Demo形态讨论，[8][9][10]属于输出敏感性讨论，但它们共同构成"如何呈现结果"这个统一焦点，且都在最终的"那定一下"收口下完成。所以合并为一个块。
═══════════════════════════════════════

现在开始切分。
"""




async def _segment_llm(batch: FetchBatch) -> List[EvidenceBlock]:
    """
    LLM 话题理解切分：把消息列表交给 LLM，由其判断话题边界并分组。
    优点：理解上下文和语义转折，不受短确认句干扰。
    失败时由调用方回退到 _segment_semantic。
    """
    if not batch.messages:
        return []

    messages = sorted(batch.messages, key=lambda m: m.message_id)

    lines = [
        f"[{i}] {m.sender_id} {m.timestamp.strftime('%H:%M')}：{m.text}"
        for i, m in enumerate(messages)
    ]
    prompt = _LLM_SEGMENT_PROMPT.format(
        messages="\n".join(lines),
        total=len(messages),
        max_block=MAX_BLOCK_MESSAGES,
        min_block=MIN_BLOCK_MESSAGES,
        gap_seconds=BLOCK_GAP_SECONDS,
        topics=_topics_for_prompt(),
    )

    raw = await _call_llm_segment(prompt)
    if not raw:
        logger.warning("LLM 切分无有效返回，回退语义切分 | chat=%s", batch.chat_id)
        return await _segment_semantic(batch)

    chunks = raw.get("chunks")
    if not isinstance(chunks, list) or not chunks:
        logger.warning("LLM 切分 chunks 字段无效，回退语义切分 | chat=%s raw=%s", batch.chat_id, raw)
        return await _segment_semantic(batch)

    n = len(messages)
    valid_topics = set(_topic_flat_list())
    blocks: List[EvidenceBlock] = []

    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        msg_ids: list = chunk.get("message_ids", [])
        if not isinstance(msg_ids, list) or not msg_ids:
            continue
        group_msgs = [messages[idx] for idx in msg_ids if 0 <= idx < n]
        if not group_msgs:
            continue

        block = _make_block(batch.chat_id, group_msgs)
        block.block_type      = chunk.get("block_type") or "decision"
        block.boundary_signal = chunk.get("boundary_signal")
        block.one_line_summary = chunk.get("one_line_summary")

        raw_topic = chunk.get("topic")
        if raw_topic and raw_topic in valid_topics:
            block.topic = raw_topic
        elif raw_topic:
            logger.warning("LLM 返回了未知 topic=%s，忽略", raw_topic)

        blocks.append(block)

    noise  = sum(1 for b in blocks if b.block_type == "noise")
    logger.info(
        "LLM 切分完成 | chat=%s 消息数=%d 块数=%d noise=%d",
        batch.chat_id, n, len(blocks), noise,
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
    seed = int(os.getenv("LLM_SEED", "42"))
    async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
        resp = await client.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
                "temperature": 0,
                "top_p": 1,
                "seed": seed,
            },
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        return json.loads(content)


async def _call_ollama_segment(prompt: str) -> Optional[dict]:
    ollama_url = os.getenv("OLLAMA_URL", "http://localhost:11434")
    model = os.getenv("LOCAL_MODEL", "qwen2.5:7b")
    seed = int(os.getenv("LLM_SEED", "42"))
    async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
        resp = await client.post(
            f"{ollama_url}/api/generate",
            json={"model": model, "prompt": prompt, "stream": False, "format": "json",
                  "options": {"temperature": 0, "seed": seed}},
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
