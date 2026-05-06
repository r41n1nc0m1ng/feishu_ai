import json
import logging
import os
from typing import List, Optional

import httpx
import numpy as np

from memory.graphiti_client import GraphitiClient
from memory.llm_runtime import apply_thinking_payload
from memory.schemas import CardStatus, EvidenceBlock, MemoryCard, TopicSummary

logger = logging.getLogger(__name__)

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
RERANK_MODEL = os.getenv("LOCAL_MODEL", "qwen2.5:7b")


def _rerank_enabled() -> bool:
    """RERANK_ENABLED=true 才会触发两段式 LLM rerank。"""
    return os.getenv("RERANK_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


_RERANK_RECALL_K = int(os.getenv("RERANK_RECALL_K", "10"))   # 第一阶段 hybrid 召回数量上限

_RERANK_PROMPT = """\
你是一个群聊记忆检索助手。下面是用户提出的问题，以及若干候选记忆卡片。
请基于"用户真正在问什么"挑出与问题最贴合的卡片，并按相关度从高到低排序。

【用户问题】
{query}

【候选卡片】（共 {n} 张，编号 0 起）
{cards_block}

【排序规则】
- 优先看卡片的 decision_object 与 decision 是否回答了用户问题的核心点
- reason 仅作辅助，不要被无关 reason 中的关键词带偏
- 不相关的卡片不要写进结果
- 如果所有候选都不相关，返回空数组

【输出 JSON】只返回 JSON，不要其他内容：
{{"ranked_indices": [候选下标, ...], "reason": "一句话依据"}}
"""


class MemoryRetriever:
    """
    记忆检索服务接口（写入侧实现，查询侧调用）。

    retrieve() 流程：
      1. Graphiti 语义搜索 → 获取相关 fact 列表（决定排序和召回范围）
      2. 对每条 fact，从内存缓存中匹配真实 MemoryCard（含 source_block_ids）
      3. 缓存未命中时回退到临时 MemoryCard（兼容旧数据）
    """

    async def retrieve(
        self, chat_id: str, query: str, limit: int = 5
    ) -> List[MemoryCard]:
        """
        两段式检索（仅返回 Active 状态卡片）：
          Stage 1 — hybrid recall：embedding cosine + 分层 bigram 字面命中
                    召回 RERANK_RECALL_K（默认 10）张候选
          Stage 2 — LLM rerank（仅当 RERANK_ENABLED=true 时启用）：
                    LLM 读 query + 候选卡片，重新排序输出 top-limit
                    LLM 不可用 → 兜底返回 hybrid 原排序的 top-limit

        当 hybrid 召回不到任何卡（如 embedding 缓存为空）→ 走 Graphiti 图搜索降级。
        """
        recall_k = max(limit, _RERANK_RECALL_K) if _rerank_enabled() else limit
        candidates = await self._hybrid_topk(chat_id, query, k=recall_k)

        if not candidates:
            return await self._graphiti_fallback(chat_id, query, limit)

        if _rerank_enabled() and len(candidates) > 1:
            ranked = await self._llm_rerank(query, candidates, limit=limit)
            if ranked is not None:
                return ranked

        return candidates[:limit]

    async def _hybrid_topk(
        self, chat_id: str, query: str, k: int
    ) -> List[MemoryCard]:
        """Stage 1：hybrid 打分召回 top-k 候选。embedding 缓存为空时返回 []，由调用方触发降级。"""
        from memory.card_generator import _card_cache, _card_embeddings

        active_cards = [
            c for c in _card_cache.values()
            if c.chat_id == chat_id and c.decision and c.status != CardStatus.DEPRECATED
        ]
        cards_with_emb = [
            (c, _card_embeddings[c.memory_id])
            for c in active_cards
            if c.memory_id in _card_embeddings
        ]
        if not cards_with_emb:
            return []

        query_vec = await self._embed_text(query)
        if query_vec is None:
            return []

        scored = sorted(
            [(self._hybrid_score(query, query_vec, emb, c), c)
             for c, emb in cards_with_emb],
            reverse=True,
            key=lambda x: x[0],
        )
        cards = [c for _, c in scored[:k]]
        logger.info(
            "Memory retrieve (hybrid topK=%d) | chat=%s query=%s top3=%s",
            k, chat_id, query[:40],
            [(round(s, 3), c.decision_object) for s, c in scored[:3]],
        )
        return cards

    async def _graphiti_fallback(
        self, chat_id: str, query: str, limit: int
    ) -> List[MemoryCard]:
        """embedding 不可用时走 Graphiti 图搜索 + fact→card 匹配。"""
        raw_results = await self.search_active(chat_id, query, limit=limit)
        logger.info(
            "Memory retrieve (Graphiti) | chat=%s query=%s raw_hits=%d",
            chat_id, query, len(raw_results),
        )
        cards_out: List[MemoryCard] = []
        seen_ids: set[str] = set()
        for raw in raw_results:
            fact = raw.get("fact", "")
            fact_vec = await self._embed_text(fact) if fact else None
            card = self._find_card_for_fact(chat_id, fact, fact_vec)
            if card and card.memory_id not in seen_ids:
                seen_ids.add(card.memory_id)
                cards_out.append(card)
            elif not card:
                cards_out.append(self._to_memory_card(chat_id, query, raw))
        return cards_out[:limit]

    async def retrieve_all(
        self, chat_id: str, query: str, limit: int = 5
    ) -> List[MemoryCard]:
        """同 retrieve()，但同时返回 Deprecated 状态的旧版本（用于版本链展示）。"""
        raw_results = await self.search(chat_id, query, limit=limit)
        return [self._to_memory_card(chat_id, query, raw) for raw in raw_results]

    async def retrieve_topic_summary(
        self, chat_id: str, query: str, limit: int = 3
    ) -> List[TopicSummary]:
        """
        读取当前群的 TopicSummary，并按轻量字符重叠排序。
        P1 约定：TopicSummary 直接读取 SQLite 真相源，不复用 Graphiti fact 映射链路。
        """
        from memory.topic_manager import TopicManager

        summaries = await TopicManager().get_topics(chat_id)
        if not summaries:
            return []

        query_chars = set((query or "").strip())
        scored: list[tuple[float, TopicSummary]] = []
        for summary in summaries:
            haystack = f"{summary.topic} {summary.summary}"
            haystack_chars = set(haystack)
            inter = len(query_chars & haystack_chars)
            union = len(query_chars | haystack_chars) or 1
            score = inter / union if query_chars else 0.0
            scored.append((score, summary))

        scored.sort(key=lambda item: item[0], reverse=True)
        top = [summary for score, summary in scored if score > 0][:limit]
        return top if top else summaries[:limit]

    async def expand_evidence(self, block_id: str) -> Optional[EvidenceBlock]:
        """
        根据 block_id 展开对应的 EvidenceBlock 原始消息列表。
        优先走内存缓存，缓存未命中时从 SQLite 查询（重启后仍可用）。
        """
        from memory.evidence_store import EvidenceStore
        block = await EvidenceStore().get(block_id)
        if not block:
            logger.warning("expand_evidence: block_id 未命中 | block_id=%s", block_id)
        else:
            logger.info(
                "expand_evidence hit | block_id=%s chat=%s messages=%d",
                block_id,
                block.chat_id,
                len(block.messages),
            )
        return block

    async def get_version_chain(self, memory_id: str) -> List[MemoryCard]:
        """
        从 memory_id 出发，沿 supersedes_memory_ids 向上 BFS 追溯完整版本祖先。
        返回 [当前卡, 直接父辈, 更上一层, ...]；REFINE/PROGRESS_* 合并产生多父辈时按 BFS 深度排列。
        """
        chain: List[MemoryCard] = []
        seen: set[str] = set()
        queue: List[str] = [memory_id]

        while queue:
            cid = queue.pop(0)
            if cid in seen:
                continue
            seen.add(cid)
            card = await self.get_card_by_id(cid)
            if not card:
                continue
            chain.append(card)
            queue.extend(card.supersedes_memory_ids or [])

        return chain

    async def get_card_by_id(self, memory_id: str) -> Optional[MemoryCard]:
        """根据 memory_id 精确查询单张 MemoryCard（缓存或 SQLite）。"""
        from memory.card_generator import get_card
        from memory import store
        card = get_card(memory_id)
        if not card:
            card = store.load_memory_card(memory_id)
            if card:
                logger.debug("get_card_by_id: SQLite 命中 | memory_id=%s", memory_id)
        return card

    async def search(self, chat_id: str, query: str, limit: int = 5) -> List[dict]:
        """兼容旧链路：直接返回 Graphiti 搜索结果 dict。"""
        try:
            return await GraphitiClient().search_memories(chat_id, query, limit=limit)
        except Exception as e:
            logger.error("Memory retrieval failed: %s", e)
            return []

    async def search_active(self, chat_id: str, query: str, limit: int = 5) -> List[dict]:
        """同 search()，过滤 Deprecated 条目。
        注意：Graphiti fact 本身不携带 status 字段，此处过滤依赖后续
        _find_card_for_fact() 从本地缓存匹配真实 MemoryCard 后再检查 status；
        Graphiti 返回值中的 status 不作为状态真相源。
        """
        results = await self.search(chat_id, query, limit=limit * 2)
        active = [
            r for r in results
            if r.get("status", CardStatus.ACTIVE) != CardStatus.DEPRECATED
        ]
        return active[:limit]

    # ── 内部辅助 ──────────────────────────────────────────────────────────────

    # 混合检索权重：cosine 主导 + 关键词重合补正
    _HYBRID_COSINE_WEIGHT  = 0.5
    _HYBRID_KEYWORD_WEIGHT = 0.5
    _HYBRID_NGRAM_N        = 2   # 中文 bigram，对短词更敏感
    # decision 是卡片主体；reason 是辅助；PROGRESS 字段是补充内容
    _FIELD_WEIGHT_DECISION = 0.7
    _FIELD_WEIGHT_REASON   = 0.2
    _FIELD_WEIGHT_EXTRA    = 0.1

    @staticmethod
    def _ngrams(text: str, n: int) -> set[str]:
        text = (text or "").strip()
        return {text[i:i+n] for i in range(len(text) - n + 1)} if len(text) >= n else set()

    def _keyword_score(self, q_bigrams: set[str], card: MemoryCard) -> float:
        """分层 bigram 命中率：decision 命中权重最高，reason 次之，PROGRESS 补充字段最低。

        避免"reason 中提及关键词"被错当成"卡片主体讨论这件事"——
        如 wrong card 的 reason 含'不自动淘汰原则一致'，但 decision 是 UI 设计。
        """
        if not q_bigrams:
            return 0.0

        decision_text = f"{card.decision_object} {card.decision}"
        reason_text   = card.reason if (card.reason and card.reason != "无") else ""
        extra_parts: list[str] = []
        if card.tentative_consensus: extra_parts.extend(card.tentative_consensus)
        if card.open_questions:      extra_parts.extend(card.open_questions)
        extra_text = " ".join(extra_parts)

        n = self._HYBRID_NGRAM_N
        d_hits = len(q_bigrams & self._ngrams(decision_text, n)) / len(q_bigrams)
        r_hits = len(q_bigrams & self._ngrams(reason_text,   n)) / len(q_bigrams) if reason_text else 0.0
        e_hits = len(q_bigrams & self._ngrams(extra_text,    n)) / len(q_bigrams) if extra_text   else 0.0

        return (self._FIELD_WEIGHT_DECISION * d_hits +
                self._FIELD_WEIGHT_REASON   * r_hits +
                self._FIELD_WEIGHT_EXTRA    * e_hits)

    def _hybrid_score(
        self,
        query: str,
        query_vec: np.ndarray,
        card_vec: np.ndarray,
        card: MemoryCard,
    ) -> float:
        """embedding 余弦 + 分层 bigram 字面重合度的加权混合分。"""
        cosine = float(np.dot(query_vec, card_vec))
        q_bigrams = self._ngrams(query, self._HYBRID_NGRAM_N)
        if not q_bigrams:
            return cosine
        keyword = self._keyword_score(q_bigrams, card)
        return self._HYBRID_COSINE_WEIGHT * cosine + self._HYBRID_KEYWORD_WEIGHT * keyword

    async def _embed_text(self, text: str) -> Optional[np.ndarray]:
        """获取文本 embedding，复用 card_generator 的 _get_embedding。"""
        from memory.card_generator import _get_embedding
        return await _get_embedding(text)

    # ── Stage 2：LLM rerank ───────────────────────────────────────────────────

    async def _llm_rerank(
        self,
        query: str,
        candidates: List[MemoryCard],
        limit: int,
    ) -> Optional[List[MemoryCard]]:
        """LLM 读 query + 候选卡，重新排序。失败/格式异常返回 None 由调用方退化。"""
        if not candidates:
            return []

        cards_block = "\n".join(
            f"[{i}] decision_object={c.decision_object}\n    decision={c.decision}\n    reason={c.reason or '无'}"
            for i, c in enumerate(candidates)
        )
        prompt = _RERANK_PROMPT.format(
            query=query, n=len(candidates), cards_block=cards_block,
        )

        try:
            raw = await self._call_rerank_llm(prompt)
        except Exception as e:
            logger.warning("rerank LLM 调用异常 | err=%s", e)
            return None
        if not raw:
            return None

        ranked_indices = raw.get("ranked_indices") or []
        if not isinstance(ranked_indices, list):
            logger.warning("rerank 返回 ranked_indices 类型错误: %r", ranked_indices)
            return None

        # 把整数下标映射回 MemoryCard，过滤越界 / 重复
        seen: set[int] = set()
        ordered: List[MemoryCard] = []
        for raw_idx in ranked_indices:
            try:
                idx = int(raw_idx)
            except (TypeError, ValueError):
                continue
            if idx < 0 or idx >= len(candidates) or idx in seen:
                continue
            seen.add(idx)
            ordered.append(candidates[idx])
            if len(ordered) >= limit:
                break

        if not ordered:
            logger.info("rerank 返回空命中 (LLM 认为候选都不相关) | query=%s", query[:40])
            return []

        logger.info(
            "Memory retrieve (rerank) | query=%s top3=%s reason=%s",
            query[:40],
            [c.decision_object for c in ordered[:3]],
            (raw.get("reason") or "")[:80],
        )
        return ordered

    async def _call_rerank_llm(self, prompt: str) -> Optional[dict]:
        """优先级：DeepSeek → OpenAI → Ollama，与其他模块一致。"""
        if os.getenv("DEEPSEEK_API_KEY"):
            return await self._call_deepseek_rerank(prompt)
        provider = os.getenv("MODEL_PROVIDER", "ollama").strip().lower()
        if provider == "openai" or os.getenv("OPENAI_API_KEY"):
            return await self._call_openai_rerank(prompt)
        return await self._call_ollama_rerank(prompt)

    async def _call_deepseek_rerank(self, prompt: str) -> Optional[dict]:
        api_key  = os.getenv("DEEPSEEK_API_KEY", "")
        base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
        model    = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
        seed     = int(os.getenv("LLM_SEED", "42"))
        async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json=apply_thinking_payload({
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "response_format": {"type": "json_object"},
                    "temperature": 0,
                    "top_p": 1,
                    "seed": seed,
                }),
            )
            resp.raise_for_status()
            return json.loads(resp.json()["choices"][0]["message"]["content"])

    async def _call_openai_rerank(self, prompt: str) -> Optional[dict]:
        api_key  = os.getenv("OPENAI_API_KEY", "")
        base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        model    = os.getenv("OPENAI_MODEL", RERANK_MODEL)
        seed     = int(os.getenv("LLM_SEED", "42"))
        if not api_key:
            return None
        async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json=apply_thinking_payload({
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "response_format": {"type": "json_object"},
                    "temperature": 0,
                    "top_p": 1,
                    "seed": seed,
                }),
            )
            resp.raise_for_status()
            return json.loads(resp.json()["choices"][0]["message"]["content"])

    async def _call_ollama_rerank(self, prompt: str) -> Optional[dict]:
        seed = int(os.getenv("LLM_SEED", "42"))
        async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
            resp = await client.post(
                f"{OLLAMA_URL}/api/generate",
                json={"model": RERANK_MODEL, "prompt": prompt,
                      "stream": False, "format": "json",
                      "options": {"temperature": 0, "seed": seed}},
            )
            resp.raise_for_status()
            return json.loads(resp.json().get("response", "{}"))

    def _find_card_for_fact(
        self,
        chat_id: str,
        fact: str,
        fact_vec: Optional[np.ndarray] = None,
    ) -> Optional[MemoryCard]:
        """
        从内存缓存中找到与 Graphiti fact 最匹配的 MemoryCard。
        优先使用 embedding 余弦相似度（fact_vec 由调用方预先计算）；
        fact_vec 为 None 时降级为字符级 Jaccard。
        """
        from memory.card_generator import _card_cache, _card_embeddings

        if not fact or len(fact) < 4:
            return None

        active_cards = [
            c for c in _card_cache.values()
            if c.chat_id == chat_id and c.decision and c.status != CardStatus.DEPRECATED
        ]
        if not active_cards:
            return None

        # ── embedding 路径 ────────────────────────────────────────────────────
        if fact_vec is not None:
            cards_with_emb = [(c, _card_embeddings[c.memory_id])
                              for c in active_cards if c.memory_id in _card_embeddings]
            if cards_with_emb:
                best_card, best_score = None, -1.0
                for card, card_vec in cards_with_emb:
                    score = float(np.dot(fact_vec, card_vec))
                    if score > best_score:
                        best_score, best_card = score, card
                if best_card and best_score >= 0.5:
                    logger.debug("fact→card embedding match | score=%.3f card=%s",
                                 best_score, best_card.memory_id)
                    return best_card

        # ── Jaccard 降级路径（embedding 缓存未命中或向量不可用）──────────────
        fact_chars = set(fact)
        best_card, best_score = None, 0.0
        for card in active_cards:
            decision_chars = set(card.decision)
            inter = len(decision_chars & fact_chars)
            union = len(decision_chars | fact_chars)
            score = inter / union if union else 0.0
            if score > best_score:
                best_score, best_card = score, card

        return best_card if best_score >= 0.35 else None

    def _to_memory_card(self, chat_id: str, query: str, raw: dict) -> MemoryCard:
        """将 Graphiti raw fact 转为临时 MemoryCard（source_block_ids 为空）。"""
        fact = (raw.get("fact") or "").strip()
        title = fact.splitlines()[0][:80] if fact else query[:80]
        return MemoryCard(
            chat_id=chat_id,
            decision_object=query,
            title=title or "检索结果",
            decision=fact or query,
            reason="",
            status=CardStatus.ACTIVE,
            source_block_ids=[],
        )
