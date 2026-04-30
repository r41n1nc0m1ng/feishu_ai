from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from memory.schemas import EvidenceMessage, FeishuMessage, FetchBatch
from preprocessor.event_segmenter import segment
from realtime.action_handler import RealtimeActionHandler
from realtime.dispatcher import dispatch_message
from realtime.query_handler import RealtimeQueryHandler


@dataclass
class ReplayResult:
    channel: str
    ok: bool
    skipped: bool = False
    message_id: str = ""
    batch_id: str = ""
    action: str = ""
    result_count: int = 0
    input_count: int = 0
    ignored_message_ids: list[str] = field(default_factory=list)
    error: str = ""
    payload: Any = None


class _EmptyRetriever:
    async def retrieve(self, chat_id: str, query: str, limit: int = 3):
        return []

    async def expand_evidence(self, block_id: str):
        return None


class _ReplyCollector:
    def __init__(self):
        self.replies: list[tuple[str, str]] = []

    async def send_text(self, chat_id: str, text: str) -> None:
        self.replies.append((chat_id, text))


RealtimeEntry = Callable[[FeishuMessage], Awaitable[Any]]
WriteEntry = Callable[[FetchBatch], Any]


class DualChannelReplayAdapter:
    """
    Owns all fixture-to-runtime adaptation and concrete entry calls.

    If realtime or write-side entry signatures change later, update this file.
    The runner should continue to pass raw fixture messages and raw batches.
    """

    def __init__(
        self,
        *,
        realtime_entry: RealtimeEntry | None = None,
        write_entry: WriteEntry | None = None,
    ):
        self.reply_collector = _ReplyCollector()
        self.realtime_entry = realtime_entry or self._default_realtime_entry
        self.write_entry = write_entry or self._default_write_entry

    async def send_realtime_message(
        self,
        raw_msg: dict[str, Any],
        *,
        case: dict[str, Any],
        batch: dict[str, Any],
    ) -> ReplayResult:
        message_id = self.message_id(raw_msg)
        if not self.should_send_to_realtime(raw_msg):
            return ReplayResult(
                channel="realtime",
                ok=True,
                skipped=True,
                message_id=message_id,
                batch_id=str(batch.get("batch_id", "")),
            )

        try:
            message = self.to_realtime_message(raw_msg, self.batch_chat_id(case, batch))
            trace = await self.realtime_entry(message)
            return ReplayResult(
                channel="realtime",
                ok=True,
                message_id=message.message_id,
                batch_id=str(batch.get("batch_id", "")),
                action=getattr(trace, "action", ""),
                payload=trace,
            )
        except Exception as exc:
            return ReplayResult(
                channel="realtime",
                ok=False,
                message_id=message_id,
                batch_id=str(batch.get("batch_id", "")),
                error=str(exc),
            )

    async def send_write_batch(
        self,
        batch: dict[str, Any],
        *,
        case: dict[str, Any],
    ) -> ReplayResult:
        batch_id = str(batch.get("batch_id", ""))
        try:
            fetch_batch = self.to_fetch_batch(batch.get("messages") or [], self.batch_chat_id(case, batch))
            result = self.write_entry(fetch_batch)
            if hasattr(result, "__await__"):
                result = await result
            result_count = len(result) if hasattr(result, "__len__") else int(result is not None)
            return ReplayResult(
                channel="write",
                ok=True,
                batch_id=batch_id,
                result_count=result_count,
                input_count=len(fetch_batch.messages),
                ignored_message_ids=[
                    self.message_id(msg)
                    for msg in (batch.get("messages") or [])
                    if not self.should_send_to_write_layer(msg)
                ],
                payload=result,
            )
        except Exception as exc:
            return ReplayResult(
                channel="write",
                ok=False,
                batch_id=batch_id,
                error=str(exc),
            )

    async def _default_realtime_entry(self, message: FeishuMessage):
        return await dispatch_message(
            message,
            query_handler=RealtimeQueryHandler(
                retriever=_EmptyRetriever(),
                send_text=self.reply_collector.send_text,
            ),
            action_handler=RealtimeActionHandler(send_text=self.reply_collector.send_text),
        )

    def _default_write_entry(self, fetch_batch: FetchBatch):
        return segment(fetch_batch)

    def batch_chat_id(self, case: dict[str, Any], batch: dict[str, Any]) -> str:
        return str(batch.get("chat_id") or case.get("chat_id") or "")

    def parse_content_text(self, raw_msg: dict[str, Any]) -> str:
        raw_content = raw_msg.get("content")
        if raw_content is None:
            raw_content = (raw_msg.get("body") or {}).get("content")
        if raw_content is None:
            raw_content = raw_msg.get("text", "")

        if isinstance(raw_content, dict):
            return str(raw_content.get("text", "")).strip()
        if not isinstance(raw_content, str):
            return str(raw_content or "").strip()

        try:
            parsed = json.loads(raw_content or "{}")
        except json.JSONDecodeError:
            return raw_content.strip()

        if isinstance(parsed, dict):
            return str(parsed.get("text", raw_content)).strip()
        return raw_content.strip()

    def parse_timestamp(self, value: Any) -> datetime:
        if isinstance(value, datetime):
            return value
        if value is None or value == "":
            raise ValueError("message timestamp is required")

        if isinstance(value, (int, float)):
            timestamp = float(value)
        else:
            text = str(value).strip()
            if text.isdigit():
                timestamp = float(text)
            else:
                try:
                    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                except ValueError:
                    parsed = None
                    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                        try:
                            parsed = datetime.strptime(text, fmt)
                            break
                        except ValueError:
                            pass
                    if parsed is None:
                        raise
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

        if timestamp > 10_000_000_000:
            timestamp = timestamp / 1000
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)

    def to_realtime_message(self, raw_msg: dict[str, Any], chat_id: str = "") -> FeishuMessage:
        mentions = self.mentions(raw_msg)
        return FeishuMessage(
            message_id=self.message_id(raw_msg),
            sender_id=self.sender_id(raw_msg),
            chat_id=self.chat_id(raw_msg, chat_id),
            chat_type=str(raw_msg.get("chat_type") or "group"),
            text=self.parse_content_text(raw_msg),
            timestamp=self.parse_timestamp(self.raw_timestamp(raw_msg)),
            mentions=mentions,
            is_at_bot=self.is_at_bot(raw_msg, mentions),
        )

    def to_evidence_message(self, raw_msg: dict[str, Any]) -> EvidenceMessage:
        return EvidenceMessage(
            message_id=self.message_id(raw_msg),
            sender_id=self.sender_id(raw_msg),
            sender_name=self.sender_name(raw_msg),
            timestamp=self.parse_timestamp(self.raw_timestamp(raw_msg)),
            text=self.parse_content_text(raw_msg),
        )

    def to_fetch_batch(self, raw_messages: list[dict[str, Any]], chat_id: str = "") -> FetchBatch:
        if not raw_messages:
            raise ValueError("empty batch cannot be converted to FetchBatch")

        batch_chat_id = self.chat_id(raw_messages[0], chat_id)
        writable_messages = [msg for msg in raw_messages if self.should_send_to_write_layer(msg)]
        all_timestamps = sorted(self.parse_timestamp(self.raw_timestamp(msg)) for msg in raw_messages)

        if not writable_messages:
            return FetchBatch(
                chat_id=batch_chat_id,
                fetch_start=all_timestamps[0],
                fetch_end=all_timestamps[-1],
                messages=[],
            )

        messages = sorted(
            [self.to_evidence_message(msg) for msg in writable_messages],
            key=lambda msg: msg.timestamp,
        )
        return FetchBatch(
            chat_id=batch_chat_id,
            fetch_start=all_timestamps[0],
            fetch_end=all_timestamps[-1],
            messages=messages,
        )

    def should_send_to_realtime(self, raw_msg: dict[str, Any]) -> bool:
        return self.is_text_message(raw_msg) and bool(self.parse_content_text(raw_msg))

    def should_send_to_write_layer(self, raw_msg: dict[str, Any]) -> bool:
        if not self.should_send_to_realtime(raw_msg):
            return False
        if self.sender_type(raw_msg) == "app":
            return False
        if self.is_at_bot(raw_msg, self.mentions(raw_msg)):
            return False
        return True

    def is_text_message(self, raw_msg: dict[str, Any]) -> bool:
        return str(raw_msg.get("msg_type") or "text") == "text"

    def raw_timestamp(self, raw_msg: dict[str, Any]) -> Any:
        return (
            raw_msg.get("create_time")
            or raw_msg.get("timestamp")
            or raw_msg.get("time")
            or raw_msg.get("date")
        )

    def message_id(self, raw_msg: dict[str, Any]) -> str:
        return str(raw_msg.get("message_id") or raw_msg.get("msg_id") or raw_msg.get("id") or "")

    def chat_id(self, raw_msg: dict[str, Any], default_chat_id: str = "") -> str:
        return str(raw_msg.get("chat_id") or raw_msg.get("group_id") or default_chat_id)

    def sender(self, raw_msg: dict[str, Any]) -> dict[str, Any]:
        sender = raw_msg.get("sender") or {}
        return sender if isinstance(sender, dict) else {}

    def sender_type(self, raw_msg: dict[str, Any]) -> str:
        return str(self.sender(raw_msg).get("sender_type") or raw_msg.get("sender_type") or "")

    def sender_id(self, raw_msg: dict[str, Any]) -> str:
        sender = self.sender(raw_msg)
        sender_id = sender.get("sender_id")
        if isinstance(sender_id, dict):
            return str(sender_id.get("open_id") or sender_id.get("user_id") or "")
        if isinstance(sender_id, str):
            return sender_id

        sender_open_id = sender.get("id")
        if isinstance(sender_open_id, dict):
            return str(sender_open_id.get("open_id") or sender_open_id.get("user_id") or "")
        return str(
            sender_open_id
            or sender.get("open_id")
            or raw_msg.get("sender_id")
            or raw_msg.get("user_id")
            or ""
        )

    def sender_name(self, raw_msg: dict[str, Any]) -> str:
        sender = self.sender(raw_msg)
        return str(raw_msg.get("sender_name") or sender.get("name") or sender.get("sender_name") or "")

    def mentions(self, raw_msg: dict[str, Any]) -> list[str]:
        content = raw_msg.get("content")
        parsed_content: dict[str, Any] = {}
        if isinstance(content, str):
            try:
                maybe_content = json.loads(content or "{}")
                if isinstance(maybe_content, dict):
                    parsed_content = maybe_content
            except json.JSONDecodeError:
                parsed_content = {}
        elif isinstance(content, dict):
            parsed_content = content

        raw_mentions = raw_msg.get("mentions") or parsed_content.get("mentions") or []
        return [open_id for item in raw_mentions if (open_id := self.extract_open_id(item))]

    def extract_open_id(self, mention: Any) -> str:
        if isinstance(mention, str):
            return mention
        if not isinstance(mention, dict):
            return ""

        id_field = mention.get("id")
        if isinstance(id_field, dict):
            return str(id_field.get("open_id") or id_field.get("user_id") or "")
        return str(id_field or mention.get("open_id") or mention.get("user_id") or "")

    def is_at_bot(self, raw_msg: dict[str, Any], mentions: list[str]) -> bool:
        if "is_at_bot" in raw_msg:
            return bool(raw_msg["is_at_bot"])
        bot_open_id = os.getenv("FEISHU_BOT_OPEN_ID", "").strip()
        if bot_open_id:
            return bot_open_id in mentions
        return bool(mentions)
