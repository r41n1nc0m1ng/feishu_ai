import logging
from typing import Optional

from memory.retriever import MemoryRetriever
from memory.schemas import ExtractedMemory

logger = logging.getLogger(__name__)


class ConflictDetector:
    """
    Detects when a newly extracted memory conflicts with an existing Active memory
    on the same topic within the same chat.
    """

    async def find_conflict(
        self, chat_id: str, new_memory: ExtractedMemory
    ) -> Optional[dict]:
        """
        Returns the conflicting existing memory fact dict if found, else None.
        """
        retriever = MemoryRetriever()
        candidates = await retriever.search_active(chat_id, new_memory.title, limit=3)
        for candidate in candidates:
            if self._is_conflict(new_memory, candidate):
                logger.info(
                    "Conflict detected: new='%s' vs existing='%s'",
                    new_memory.title,
                    candidate.get("fact", "")[:60],
                )
                return candidate
        return None

    def _is_conflict(self, new_memory: ExtractedMemory, existing: dict) -> bool:
        # TODO: replace with LLM-based judgment for higher accuracy.
        # Heuristic: overlapping title or decision text suggests same topic.
        existing_fact = existing.get("fact", "").lower()
        return (
            new_memory.decision.lower() in existing_fact
            or new_memory.title.lower() in existing_fact
        )
