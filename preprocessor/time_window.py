import asyncio
import logging
from collections import defaultdict
from datetime import datetime
from typing import Awaitable, Callable, List

from memory.schemas import EventBlock, FeishuMessage

logger = logging.getLogger(__name__)

WINDOW_SECONDS = 180        # flush after 3 min of inactivity
MAX_WINDOW_MESSAGES = 20    # or flush when this many messages accumulate


class TimeWindowAccumulator:
    """
    Accumulates messages per chat_id and flushes them as an EventBlock
    when the time window expires or message count threshold is reached.

    Usage:
        acc = TimeWindowAccumulator(flush_callback=process_block)
        await acc.add_message(message)
    """

    def __init__(self, flush_callback: Callable[[EventBlock], Awaitable[None]]):
        self._flush_callback = flush_callback
        self._buffers: dict[str, List[FeishuMessage]] = defaultdict(list)
        self._window_starts: dict[str, datetime] = {}
        self._timers: dict[str, asyncio.TimerHandle] = {}
        self._lock = asyncio.Lock()

    async def add_message(self, message: FeishuMessage) -> None:
        async with self._lock:
            chat_id = message.chat_id
            if chat_id not in self._window_starts:
                self._window_starts[chat_id] = message.timestamp
            self._buffers[chat_id].append(message)
            self._reschedule_timer(chat_id)
            if len(self._buffers[chat_id]) >= MAX_WINDOW_MESSAGES:
                await self._flush(chat_id)

    def _reschedule_timer(self, chat_id: str) -> None:
        if chat_id in self._timers:
            self._timers[chat_id].cancel()
        loop = asyncio.get_event_loop()
        self._timers[chat_id] = loop.call_later(
            WINDOW_SECONDS,
            lambda: asyncio.ensure_future(self._flush_locked(chat_id)),
        )

    async def _flush_locked(self, chat_id: str) -> None:
        async with self._lock:
            await self._flush(chat_id)

    async def _flush(self, chat_id: str) -> None:
        messages = self._buffers.pop(chat_id, [])
        window_start = self._window_starts.pop(chat_id, datetime.utcnow())
        if chat_id in self._timers:
            self._timers.pop(chat_id).cancel()
        if not messages:
            return
        block = EventBlock(
            chat_id=chat_id,
            messages=messages,
            window_start=window_start,
            window_end=datetime.utcnow(),
        )
        logger.info("Flushing EventBlock for %s: %d messages", chat_id, len(messages))
        await self._flush_callback(block)
