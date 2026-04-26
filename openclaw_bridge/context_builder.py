from memory.schemas import FeishuMessage
from memory.zep_session import ZepSessionManager


class ContextBuilder:
    """
    Assembles the minimal context view passed to OpenClaw.
    Follows the 代: suggestion: only recent message window + memory hints,
    not full raw session history, to avoid context bloat.
    """

    async def build(self, message: FeishuMessage, zep: ZepSessionManager) -> dict:
        recent = await zep.get_recent_messages(message.chat_id, limit=10)
        memory_hints = await zep.get_memory_facts(message.chat_id)

        return {
            "chat_id": message.chat_id,
            "current_message_id": message.message_id,
            "messages": recent,
            "memory_hints": memory_hints,
        }
