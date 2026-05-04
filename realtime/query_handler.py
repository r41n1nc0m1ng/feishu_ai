from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

from realtime.triggers import (
    build_query_text,
    is_source_query,
    is_summary_query,
    is_topic_list_query,
    is_version_query,
)

logger = logging.getLogger(__name__)

_LAST_QUERY_CARD_BY_CHAT: dict[str, object] = {}


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
        return f'查到了相关记忆“{title}”，但没有找到可展开的原始聊天记录。'

    title = getattr(card, "title", query)
    lines = [f'“{title}”的来源记录：']
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


def render_version_reply(query: str, chain: list) -> str:
    if not chain:
        return f'当前没有查到与“{query}”相关的记忆。'
    current = chain[0]
    lines = ["当前生效版本：" + current.decision]
    if current.reason:
        lines.append("理由：" + current.reason)
    if len(chain) == 1:
        lines.append("没有发现历史更新记录。")
    else:
        lines.append(f"\n历史版本（共更新过 {len(chain) - 1} 次）：")
        for i, old in enumerate(chain[1:], 1):
            lines.append(f"  [{i}] {old.decision}（已被覆盖）")
    return "\n".join(lines)


def render_summary_reply(query: str, summaries: list) -> str:
    if not summaries:
        return f'当前没有查到与“{query}”相关的整体摘要。'

    lines = [f"根据当前群整体记忆，共 {len(summaries)} 个主题：\n"]
    for s in summaries:
        topic = getattr(s, "topic", "")
        summary = getattr(s, "summary", "")
        covered = len(getattr(s, "covered_memory_ids", []) or [])
        lines.append(f"【{topic}】{summary}")
        if covered:
            lines.append(f"  覆盖 {covered} 条相关记忆。")
    return "\n".join(lines)


def render_topic_list_reply(summary, index: int) -> str:
    topic = getattr(summary, "topic", "") or f"Topic {index}"
    body = getattr(summary, "summary", "")
    covered = len(getattr(summary, "covered_memory_ids", []) or [])
    lines = [f"【Topic {index}｜{topic}】", body]
    if covered:
        lines.append(f"覆盖 {covered} 条相关记忆。")
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
        results = []
        action = "query"

        if is_source_query(query):
            remembered = _LAST_QUERY_CARD_BY_CHAT.get(message.chat_id)
            if remembered:
                results = [remembered]
            else:
                results = await self.retriever.retrieve(message.chat_id, query, limit=3)
            action = "source"
            reply = render_query_reply(query, results)
            if results:
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
        elif is_version_query(query):
            action = "version"
            results = await self.retriever.retrieve(message.chat_id, query, limit=1)
            if results:
                _LAST_QUERY_CARD_BY_CHAT[message.chat_id] = results[0]
                chain = await self.retriever.get_version_chain(results[0].memory_id)
                reply = render_version_reply(query, chain)
            else:
                reply = f'当前没有查到与“{query}”相关的记忆。'
        elif is_topic_list_query(query):
            action = "topic_list"
            results = await self.retriever.retrieve_topic_summary(message.chat_id, "", limit=50)
            if results:
                reply = f"当前共 {len(results)} 个 TopicSummary，按主题逐条发送。"
                if self.send_text:
                    await self.send_text(message.chat_id, reply)
                    for idx, summary in enumerate(results, 1):
                        await self.send_text(message.chat_id, render_topic_list_reply(summary, idx))
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
            else:
                reply = "当前还没有生成 TopicSummary。"
        elif is_summary_query(query):
            action = "summary"
            results = await self.retriever.retrieve_topic_summary(message.chat_id, query, limit=3)
            if results:
                reply = render_summary_reply(query, results)
            else:
                action = "summary_fallback"
                results = await self.retriever.retrieve(message.chat_id, query, limit=3)
                if results:
                    _LAST_QUERY_CARD_BY_CHAT[message.chat_id] = results[0]
                reply = render_query_reply(query, results)
        else:
            results = await self.retriever.retrieve(message.chat_id, query, limit=3)
            if results:
                _LAST_QUERY_CARD_BY_CHAT[message.chat_id] = results[0]
            reply = render_query_reply(query, results)

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
