import logging
from typing import List, Optional

from memory.graphiti_client import GraphitiClient
from memory.schemas import CardStatus, EvidenceBlock, MemoryCard

logger = logging.getLogger(__name__)


class MemoryRetriever:
    """
    记忆检索服务接口（写入侧实现，查询侧调用）。

    检索优先级：Active > Deprecated，新版本 > 旧版本，有明确来源 > 来源不完整。
    两个核心接口：
      - retrieve()         检索 MemoryCard（回答"之前怎么定的"）
      - expand_evidence()  展开 EvidenceBlock 来源（回答"谁说的/原话在哪"）
    """

    async def retrieve(
        self, chat_id: str, query: str, limit: int = 5
    ) -> List[MemoryCard]:
        """
        语义检索当前群聊中与 query 相关的 MemoryCard，仅返回 Active 状态。
        查询侧直接调用此接口获取检索结果。
        """
        raw_results = await self.search_active(chat_id, query, limit=limit)
        return [self._to_memory_card(chat_id, query, raw) for raw in raw_results]

    async def retrieve_all(
        self, chat_id: str, query: str, limit: int = 5
    ) -> List[MemoryCard]:
        """
        同 retrieve()，但同时返回 Deprecated 状态的旧版本（用于版本链展示）。
        """
        raw_results = await self.search(chat_id, query, limit=limit)
        return [self._to_memory_card(chat_id, query, raw) for raw in raw_results]

    async def expand_evidence(self, block_id: str) -> Optional[EvidenceBlock]:
        """
        根据 block_id 展开对应的 EvidenceBlock 原始消息列表。
        查询侧在用户追问"谁说的/原话在哪"时调用。
        """
        # TODO: 从存储层按 block_id 查询 EvidenceBlock
        logger.warning("expand_evidence() 尚未实现 | block_id=%s", block_id)
        return None

    async def get_card_by_id(self, memory_id: str) -> Optional[MemoryCard]:
        """根据 memory_id 精确查询单张 MemoryCard。"""
        # TODO: 按主键查询
        logger.warning("get_card_by_id() 尚未实现 | memory_id=%s", memory_id)
        return None

    async def search(self, chat_id: str, query: str, limit: int = 5) -> List[dict]:
        """
        兼容旧链路的原始检索接口。
        直接返回 Graphiti 搜索结果 dict，供旧测试和迁移中模块继续使用。
        """
        try:
            return await GraphitiClient().search_memories(chat_id, query, limit=limit)
        except Exception as e:
            logger.error("Memory retrieval failed: %s", e)
            return []

    async def search_active(self, chat_id: str, query: str, limit: int = 5) -> List[dict]:
        """
        兼容旧链路的 Active 过滤接口。
        当前底层尚未完整落地版本状态，先保留接口并做最佳努力过滤。
        """
        results = await self.search(chat_id, query, limit=limit * 2)
        active = [
            r for r in results
            if r.get("status", CardStatus.ACTIVE) != CardStatus.DEPRECATED
        ]
        return active[:limit]

    def _to_memory_card(self, chat_id: str, query: str, raw: dict) -> MemoryCard:
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
