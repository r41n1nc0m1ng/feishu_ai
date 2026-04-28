from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from realtime.action_handler import RealtimeActionHandler
from realtime.query_handler import QueryTrace, RealtimeQueryHandler
from realtime.triggers import classify_realtime_action


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

    if legacy_ingest:
        await legacy_ingest(message)
        return DispatchTrace(action="noop", handled=True, delegated_to_legacy=True, note="legacy ingest")

    return DispatchTrace(action="noop", handled=False, delegated_to_legacy=False, note="no handler")
