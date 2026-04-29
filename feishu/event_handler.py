import json
import logging
import os
from datetime import datetime

try:
    from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
except ModuleNotFoundError:  # pragma: no cover - enables local non-SDK testing
    class P2ImMessageReceiveV1:  # type: ignore[no-redef]
        pass

from feishu.api_client import FeishuAPIClient, extract_open_id
from memory.schemas import FeishuMessage
from realtime.action_handler import RealtimeActionHandler
from realtime.dispatcher import dispatch_message
from realtime.query_handler import RealtimeQueryHandler

logger = logging.getLogger(__name__)


async def handle_lark_event(data: P2ImMessageReceiveV1):
    """Entry point for production: receives structured SDK objects."""
    try:
        message = _parse_lark_message(data)
        if message:
            await _process(message)
    except Exception:
        logger.exception("Unhandled error in handle_lark_event")


async def handle_raw_event(raw: dict):
    """Entry point for local tests: accepts raw dict (same shape as Feishu webhook payload)."""
    try:
        message = _parse_raw_message(raw)
        if message:
            await _process(message)
    except Exception:
        logger.exception("Unhandled error in handle_raw_event")


async def _process(message: FeishuMessage):
    logger.info(
        "Message received | chat=%s sender=%s at_bot=%s mentions=%d text=%s",
        message.chat_id,
        message.sender_id,
        message.is_at_bot,
        len(message.mentions),
        message.text[:120],
    )
    await dispatch_message(
        message,
        query_handler=RealtimeQueryHandler(send_text=FeishuAPIClient().send_text),
        action_handler=RealtimeActionHandler(send_text=FeishuAPIClient().send_text),
        legacy_ingest=handle_legacy_ingest,
    )


async def handle_realtime_query(message: FeishuMessage):
    handler = RealtimeQueryHandler(send_text=FeishuAPIClient().send_text)
    await handler.handle_query_message(message)


async def handle_legacy_ingest(message: FeishuMessage):
    # 将 chat_id 注册到批处理器的活跃群聊表
    from memory.batch_processor import BatchProcessor
    logger.info("Legacy ingest registration | chat=%s message_id=%s", message.chat_id, message.message_id)
    await BatchProcessor().register_chat(message)


def _extract_mentions_from_content(content: dict) -> list[str]:
    return [
        open_id for mention in content.get("mentions", [])
        if (open_id := extract_open_id(mention))
    ]


def _extract_mentions_from_sdk_message(msg) -> list[str]:
    return [
        open_id for mention in (getattr(msg, "mentions", []) or [])
        if (open_id := extract_open_id(mention))
    ]


def _is_at_bot(mentions: list[str]) -> bool:
    bot_open_id = os.getenv("FEISHU_BOT_OPEN_ID", "").strip()
    if bot_open_id:
        return bot_open_id in mentions
    return bool(mentions)


def _parse_lark_message(data: P2ImMessageReceiveV1):
    try:
        msg = data.event.message
        sender = data.event.sender

        if msg.chat_type not in ("group", "p2p"):
            return None

        content = json.loads(msg.content or "{}")
        text = content.get("text", "").strip()
        if not text:
            return None

        mentions = _extract_mentions_from_content(content) or _extract_mentions_from_sdk_message(msg)

        return FeishuMessage(
            message_id=msg.message_id,
            sender_id=sender.sender_id.open_id,
            chat_id=msg.chat_id,
            chat_type=msg.chat_type,
            text=text,
            timestamp=datetime.fromtimestamp(int(msg.create_time) / 1000),
            mentions=mentions,
            is_at_bot=_is_at_bot(mentions),
        )
    except Exception:
        logger.exception("Failed to parse lark SDK message")
        return None


def _parse_raw_message(raw: dict):
    try:
        evt = raw.get("event", {})
        msg = evt.get("message", {})
        sender = evt.get("sender", {})

        if msg.get("chat_type") not in ("group", "p2p"):
            return None

        content = json.loads(msg.get("content", "{}"))
        text = content.get("text", "").strip()
        if not text:
            return None

        mentions = _extract_mentions_from_content(content)
        chat_type = msg.get("chat_type", "group")

        return FeishuMessage(
            message_id=msg["message_id"],
            sender_id=sender.get("sender_id", {}).get("open_id", "unknown"),
            chat_id=msg["chat_id"],
            chat_type=chat_type,
            text=text,
            timestamp=datetime.fromtimestamp(int(msg.get("create_time", "0")) / 1000),
            mentions=mentions,
            is_at_bot=_is_at_bot(mentions),
        )
    except Exception:
        logger.exception("Failed to parse raw message dict")
        return None
