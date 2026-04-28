from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class ActionTrace:
    triggered: bool
    action: str
    reply_preview: str
    reason: str


def render_schedule_reply(text: str) -> str:
    return f"检测到这条消息可能是在约日程：\n{text}\n\n如需我继续，下一步可以接创建日程确认流程。"


def render_task_reply(text: str) -> str:
    return f"检测到这条消息可能包含待办事项：\n{text}\n\n如需我继续，下一步可以接创建待办确认流程。"


class RealtimeActionHandler:
    def __init__(self, send_text: Optional[Callable[[str, str], object]] = None):
        self.send_text = send_text

    async def handle_schedule_message(self, message) -> ActionTrace:
        reply = render_schedule_reply(message.text)
        if self.send_text:
            await self.send_text(message.chat_id, reply)
        logger.info("Realtime schedule hint sent | chat=%s", message.chat_id)
        return ActionTrace(
            triggered=True,
            action="schedule",
            reply_preview=reply[:120],
            reason="schedule-like text",
        )

    async def handle_task_message(self, message) -> ActionTrace:
        reply = render_task_reply(message.text)
        if self.send_text:
            await self.send_text(message.chat_id, reply)
        logger.info("Realtime task hint sent | chat=%s", message.chat_id)
        return ActionTrace(
            triggered=True,
            action="task",
            reply_preview=reply[:120],
            reason="task-like text",
        )
