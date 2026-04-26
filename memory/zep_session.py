import logging
from collections import defaultdict, deque
from typing import List

from memory.schemas import FeishuMessage

logger = logging.getLogger(__name__)

# In-memory session store: chat_id → deque of message dicts
# Replaces Zep CE for the demo. Swap back to ZepSessionManager when
# a self-hostable Zep image becomes reliably accessible.
_sessions: dict[str, deque] = defaultdict(lambda: deque(maxlen=30))

_SESSION_WINDOW = 10   # messages surfaced to OpenClaw context


class ZepSessionManager:
    """
    Lightweight in-memory substitute for Zep CE.
    Provides the same interface as the Zep-backed version so the rest of
    the codebase requires no changes when switching back to Zep.
    """

    async def ensure_session(self, chat_id: str):
        # Session is created lazily on first add_message; nothing to do here.
        pass

    async def add_message(self, message: FeishuMessage):
        _sessions[message.chat_id].append(
            {
                "sender": message.sender_id,
                "text": message.text,
                "message_id": message.message_id,
                "timestamp": message.timestamp.isoformat(),
            }
        )
        logger.debug("Session %s now has %d messages", message.chat_id, len(_sessions[message.chat_id]))

    async def get_recent_messages(self, chat_id: str, limit: int = _SESSION_WINDOW) -> List[dict]:
        msgs = list(_sessions[chat_id])
        return [{"sender": m["sender"], "text": m["text"]} for m in msgs[-limit:]]

    async def get_memory_facts(self, chat_id: str) -> List[str]:
        # Zep CE would return NLP-extracted facts here.
        # For the demo, return an empty list; Graphiti search covers long-term recall.
        return []
