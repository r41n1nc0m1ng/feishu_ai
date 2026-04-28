import logging
from typing import List

from memory.schemas import TopicSummary

logger = logging.getLogger(__name__)


class TopicManager:
    """
    High-granularity topic layer (三层架构 高粒度).
    Aggregates related decision episodes into TopicSummary objects backed by Graphiti community nodes.
    """

    async def get_topics(self, chat_id: str) -> List[TopicSummary]:
        """Returns all topic summaries for the given chat."""
        # TODO: query Graphiti community nodes filtered by group_id == chat_id
        return []

    async def upsert_topic(self, chat_id: str, topic: TopicSummary) -> None:
        """Creates or updates a topic summary in the graph."""
        # TODO: map TopicSummary fields to Graphiti community node schema
        logger.info("Upserting topic '%s' for chat %s", topic.topic, chat_id)

    async def rebuild_topics(self, chat_id: str) -> List[TopicSummary]:
        """
        Re-derives topic structure from existing episodes via semantic clustering.
        Call after a batch of new episodes is written.

        Steps (to implement):
          1. Retrieve all episodes for chat_id from Graphiti
          2. Embed episode summaries
          3. Cluster by cosine similarity (or GMM)
          4. Generate a TopicSummary topic+summary per cluster via LLM
          5. Upsert each TopicSummary
        """
        logger.info("Rebuilding topics for chat %s", chat_id)
        return []
