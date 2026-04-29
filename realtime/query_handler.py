from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

from realtime.triggers import build_query_text, is_source_query

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
    if getattr(top, "source_block_ids", None):
        lines.append("如需查看依据，可以继续问“原话在哪”或“依据是什么”。")
    if len(results) > 1:
        lines.append(f"补充命中 {len(results)} 条相关记忆，当前先返回最相关的一条。")
    return "\n".join(lines)


def render_evidence_reply(query: str, card, block) -> str:
    if not block or not getattr(block, "messages", None):
        title = getattr(card, "title", query)
        return f"查到了相关记忆“{title}”，但没有找到可展开的原始聊天记录。"

    title = getattr(card, "title", query)
    lines = [f"“{title}”的来源记录："]
    for message in block.messages[:6]:
        sender = getattr(message, "sender_name", "") or getattr(message, "sender_id", "unknown")
        timestamp = getattr(message, "timestamp", None)
        time_text = timestamp.strftime("%m-%d %H:%M") if timestamp else ""
        text = getattr(message, "text", "")
        lines.append(f"- {sender} {time_text}：{text}")

    remaining = len(block.messages) - 6
    if remaining > 0:
        lines.append(f"还有 {remaining} 条来源消息未展开。")
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
        action = "query"
        reply = render_query_reply(query, results)

        if results and is_source_query(query):
            action = "source"
            top = results[0]
            block = None
            for block_id in getattr(top, "source_block_ids", []):
                logger.info(
                    "Source query expanding evidence | chat=%s memory_id=%s block_id=%s",
                    message.chat_id,
                    getattr(top, "memory_id", ""),
                    block_id,
                )
                block = await self.retriever.expand_evidence(block_id)
                if block:
                    break
            reply = render_evidence_reply(query, top, block)

        if self.send_text:
            await self.send_text(message.chat_id, reply)

        logger.info(
            "Realtime query handled | chat=%s action=%s query=%s hits=%d",
            message.chat_id,
            action,
            query,
            len(results),
        )
        return QueryTrace(
            triggered=True,
            action=action,
            query_text=query,
            retrieved_count=len(results),
            reply_preview=reply[:120],
            reason="is_at_bot or explicit query",
        )
