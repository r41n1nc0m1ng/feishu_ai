import logging
from typing import List, Optional

import numpy as np

from memory.graphiti_client import GraphitiClient
from memory.schemas import CardStatus, EvidenceBlock, MemoryCard, TopicSummary

logger = logging.getLogger(__name__)


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
        语义检索当前群聊中与 query 相关的 MemoryCard，仅返回 Active 状态。

        主路径：query embedding 直接对比所有卡片 embedding（余弦排序），
        绕开 Graphiti 图遍历，避免同语义域卡片被错误路由。
        降级路径（_card_embeddings 为空时）：Graphiti 图搜索 + fact→card 匹配。
        """
        from memory.card_generator import _card_cache, _card_embeddings

        # ── 主路径：embedding 余弦排序 ────────────────────────────────────────
        active_cards = [
            c for c in _card_cache.values()
            if c.chat_id == chat_id and c.decision and c.status != CardStatus.DEPRECATED
        ]
        cards_with_emb = [
            (c, _card_embeddings[c.memory_id])
            for c in active_cards
            if c.memory_id in _card_embeddings
        ]
        if cards_with_emb:
            query_vec = await self._embed_text(query)
            if query_vec is not None:
                # 混合检索：embedding cosine + 字面字符重合度
                # 字面命中能挽回 embedding 把"自动淘汰"和"需人工确认项"语义混淆的情况
                scored = sorted(
                    [(self._hybrid_score(query, query_vec, emb, c), c)
                     for c, emb in cards_with_emb],
                    reverse=True,
                    key=lambda x: x[0],
                )
                cards = [c for _, c in scored[:limit]]
                logger.info(
                    "Memory retrieve (hybrid) | chat=%s query=%s top=%s",
                    chat_id, query[:40],
                    [(round(s, 3), c.decision_object) for s, c in scored[:3]],
                )
                return cards

        # ── 降级路径：Graphiti 图搜索 + fact→card 匹配 ────────────────────────
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
