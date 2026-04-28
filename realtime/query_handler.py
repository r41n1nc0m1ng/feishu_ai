from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

from realtime.triggers import build_query_text

logger = logging.getLogger(__name__)


@dataclass
class QueryTrace:
    triggered: bool
    action: str
    query_text: str
    retrieved_count: int
    reply_preview: str
    reason: str


def render_query_reply(query: str, results: list) -> str:
    if not results:
        return f"当前没有查到与“{query}”相关的群内记忆。"

    top = results[0]
    lines = [f"根据当前群记忆：{top.decision}"]
    if top.reason:
        lines.append(f"理由：{top.reason}")
    if len(results) > 1:
        lines.append(f"补充命中 {len(results)} 条相关记忆，当前先返回最相关的一条。")
    return "\n".join(lines)


class RealtimeQueryHandler:
    def __init__(
        self,
        retriever=None,
        send_text: Optional[Callable[[str, str], object]] = None,
    ):
        if retriever is None:
            from memory.retriever import MemoryRetriever

            retriever = MemoryRetriever()
        self.retriever = retriever
        self.send_text = send_text

    async def handle_query_message(self, message) -> QueryTrace:
        query = build_query_text(message)
        results = await self.retriever.retrieve(message.chat_id, query, limit=3)
        reply = render_query_reply(query, results)

        if self.send_text:
            await self.send_text(message.chat_id, reply)

        logger.info(
            "Realtime query handled | chat=%s query=%s hits=%d",
            message.chat_id,
            query,
            len(results),
        )
        return QueryTrace(
            triggered=True,
            action="query",
            query_text=query,
            retrieved_count=len(results),
            reply_preview=reply[:120],
            reason="is_at_bot or explicit query",
        )
