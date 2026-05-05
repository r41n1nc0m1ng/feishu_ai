from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Optional
import logging
import os

from realtime.action_handler import RealtimeActionHandler
from realtime.query_handler import QueryTrace, RealtimeQueryHandler
from realtime.triggers import classify_realtime_action
from openclaw_bridge.client import OpenClawClient
from openclaw_bridge.context_builder import ContextBuilder
from memory.zep_session import ZepSessionManager

logger = logging.getLogger(__name__)


@dataclass
class DispatchTrace:
    action: str
    handled: bool
    delegated_to_legacy: bool
    note: str


async def dispatch_message(
    message,
    *,
    query_handler: Optional[RealtimeQueryHandler] = None,
    action_handler: Optional[RealtimeActionHandler] = None,
    legacy_ingest: Optional[Callable[[object], Awaitable[None]]] = None,
) -> DispatchTrace:
    action = classify_realtime_action(message)
    logger.info(
        "Realtime dispatch | chat=%s message_id=%s action=%s text=%s",
        getattr(message, "chat_id", ""),
        getattr(message, "message_id", ""),
        action,
        getattr(message, "text", "")[:120],
    )

    if action == "query":
        handler = query_handler or RealtimeQueryHandler()
        await handler.handle_query_message(message)
        return DispatchTrace(action="query", handled=True, delegated_to_legacy=False, note="realtime query")

    if action == "schedule":
        handler = action_handler or RealtimeActionHandler()
        await handler.handle_schedule_message(message)
        return DispatchTrace(action="schedule", handled=True, delegated_to_legacy=False, note="schedule hint")

    if action == "task":
        handler = action_handler or RealtimeActionHandler()
        await handler.handle_task_message(message)
        return DispatchTrace(action="task", handled=True, delegated_to_legacy=False, note="task hint")

    if action == "noop" and os.getenv("REALTIME_OPENCLAW_FALLBACK", "").strip().lower() in {"1", "true", "yes", "on"}:
        zep = ZepSessionManager()
        await zep.ensure_session(message.chat_id)
        await zep.add_message(message)
        context = await ContextBuilder().build(message, zep)
        extracted = await OpenClawClient().extract_memory(context)
        if extracted:
            handler = query_handler or RealtimeQueryHandler()
            await handler.handle_query_message(message)
            return DispatchTrace(action="query", handled=True, delegated_to_legacy=False, note="openclaw fallback")

    if legacy_ingest:
        await legacy_ingest(message)
        return DispatchTrace(action="noop", handled=True, delegated_to_legacy=True, note="legacy ingest")

    return DispatchTrace(action="noop", handled=False, delegated_to_legacy=False, note="no handler")
