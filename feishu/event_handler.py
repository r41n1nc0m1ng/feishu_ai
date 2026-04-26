import json
import logging
from datetime import datetime

from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

from feishu.api_client import FeishuAPIClient
from memory.graphiti_client import GraphitiClient
from memory.schemas import FeishuMessage
from memory.zep_session import ZepSessionManager
from openclaw_bridge.client import OpenClawClient
from openclaw_bridge.context_builder import ContextBuilder

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
    zep = ZepSessionManager()
    await zep.ensure_session(message.chat_id)
    await zep.add_message(message)

    context = await ContextBuilder().build(message, zep)
    extracted = await OpenClawClient().extract_memory(context)
    if not extracted:
        logger.info("No memory value detected in message from %s", message.sender_id)
        return

    await GraphitiClient().add_memory_episode(message.chat_id, extracted, message)

    reply = f"[记忆已记录] {extracted.title}\n决策：{extracted.decision}\n理由：{extracted.reason}"
    await FeishuAPIClient().send_text(message.chat_id, reply)


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

        return FeishuMessage(
            message_id=msg.message_id,
            sender_id=sender.sender_id.open_id,
            chat_id=msg.chat_id,
            text=text,
            timestamp=datetime.fromtimestamp(int(msg.create_time) / 1000),
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

        return FeishuMessage(
            message_id=msg["message_id"],
            sender_id=sender.get("sender_id", {}).get("open_id", "unknown"),
            chat_id=msg["chat_id"],
            text=text,
            timestamp=datetime.fromtimestamp(int(msg.get("create_time", "0")) / 1000),
        )
    except Exception:
        logger.exception("Failed to parse raw message dict")
        return None
