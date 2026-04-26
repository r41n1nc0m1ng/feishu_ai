import logging
from typing import List

from memory.graphiti_client import GraphitiClient
from memory.schemas import MemoryStatus

logger = logging.getLogger(__name__)


class MemoryRetriever:
    """
    Hybrid retrieval over Graphiti: vector semantic search + status filter.
    Priority: new version > old, high similarity > low, complete records first.
    """

    async def search(self, chat_id: str, query: str, limit: int = 5) -> List[dict]:
        """Returns top-k relevant memory fact dicts for the given chat and query."""
        try:
            return await GraphitiClient().search_memories(chat_id, query, limit=limit)
        except Exception as e:
            logger.error("Memory retrieval failed: %s", e)
            return []

    async def search_active(self, chat_id: str, query: str, limit: int = 5) -> List[dict]:
        """Same as search() but filters out Deprecated entries."""
        results = await self.search(chat_id, query, limit=limit * 2)
        active = [
            r for r in results
            if r.get("status", MemoryStatus.ACTIVE) != MemoryStatus.DEPRECATED
        ]
        return active[:limit]
