import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


class MemoryLifecycle:
    """
    Manages memory expiry and deprecation.
    Design: project-scoped memories expire when the project ends;
    attach a project_id and deadline to MemoryItem and mark as DEPRECATED on expiry.
    """

    async def deprecate(self, chat_id: str, memory_fact: str) -> bool:
        """
        Marks a specific memory as Deprecated by adding a superseding episode.
        Returns True on success.
        """
        # Graphiti does not yet expose a direct node-update API.
        # Implement by adding a new episode that explicitly supersedes the old fact.
        logger.info("Deprecating memory in chat %s: %s", chat_id, memory_fact[:60])
        # TODO: call GraphitiClient.add_memory_episode with a "deprecated" marker
        return False

    async def expire_chat_memories(
        self, chat_id: str, cutoff: Optional[datetime] = None
    ) -> int:
        """
        Marks all memories older than cutoff as Deprecated.
        If cutoff is None, deprecates all memories in the chat.
        Returns count of deprecated items.
        """
        # TODO: requires Graphiti node iteration API or direct Neo4j query via driver.
        logger.info("Expiring memories for chat %s before %s", chat_id, cutoff)
        return 0
