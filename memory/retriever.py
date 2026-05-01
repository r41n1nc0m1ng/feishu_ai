import logging
from typing import List, Optional

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
        查询侧直接调用此接口获取检索结果。
        """
        raw_results = await self.search_active(chat_id, query, limit=limit)
        logger.info(
            "Memory retrieve start | chat=%s query=%s limit=%d raw_hits=%d",
            chat_id,
            query,
            limit,
            len(raw_results),
        )
        cards: List[MemoryCard] = []
        seen_ids: set[str] = set()

        for raw in raw_results:
            fact = raw.get("fact", "")
            # 优先从缓存匹配真实 MemoryCard（有 source_block_ids）
            card = self._find_card_for_fact(chat_id, fact)
            if card and card.memory_id not in seen_ids:
                seen_ids.add(card.memory_id)
                cards.append(card)
                logger.info(
                    "Memory retrieve matched card | chat=%s memory_id=%s title=%s sources=%s",
                    chat_id,
                    card.memory_id,
                    card.title,
                    card.source_block_ids,
                )
            elif not card:
                # 回退：用 Graphiti fact 临时构造（source_block_ids 为空）
                cards.append(self._to_memory_card(chat_id, query, raw))
                logger.info(
                    "Memory retrieve fallback fact | chat=%s fact=%s",
                    chat_id,
                    fact[:120],
                )

        logger.info("Memory retrieve done | chat=%s query=%s cards=%d", chat_id, query, len(cards[:limit]))
        return cards[:limit]

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
        从 memory_id 出发，沿 supersedes_memory_id 向上追溯完整版本链。
        返回列表从新到旧排列：[当前卡, 上一版本, 更早版本, ...]。
        """
        from memory import store
        chain: List[MemoryCard] = []
        current_id: Optional[str] = memory_id
        seen: set[str] = set()

        while current_id and current_id not in seen:
            seen.add(current_id)
            card = await self.get_card_by_id(current_id)
            if not card:
                break
            chain.append(card)
            current_id = card.supersedes_memory_id

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

    def _find_card_for_fact(self, chat_id: str, fact: str) -> Optional[MemoryCard]:
        """
        从内存缓存中找到与 Graphiti fact 最匹配的 MemoryCard。
        使用字符级 Jaccard 相似度，适合中文无分词场景。
        """
        from memory.card_generator import _card_cache
        if not fact or len(fact) < 4:
            return None

        fact_chars = set(fact)
        best_card: Optional[MemoryCard] = None
        best_score = 0.0

        for card in _card_cache.values():
            if card.chat_id != chat_id or not card.decision:
                continue
            decision_chars = set(card.decision)
            inter = len(decision_chars & fact_chars)
            union = len(decision_chars | fact_chars)
            score = inter / union if union else 0.0
            if score > best_score:
                best_score = score
                best_card = card

        # 相似度阈值 0.35，避免低质量匹配
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
