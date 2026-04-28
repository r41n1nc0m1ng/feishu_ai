import asyncio
import logging
import os
import threading

from dotenv import load_dotenv
load_dotenv()

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImChatMemberBotAddedV1, P2ImMessageReceiveV1

from feishu.event_handler import handle_lark_event
from memory.graphiti_client import GraphitiClient
from memory.batch_processor import BatchProcessor

_processor: BatchProcessor | None = None

logging.basicConfig(level=logging.INFO)
logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)
logging.getLogger("neo4j").setLevel(logging.ERROR)
logger = logging.getLogger(__name__)

_loop: asyncio.AbstractEventLoop | None = None


def on_message(data: P2ImMessageReceiveV1) -> None:
    """Sync callback from lark SDK — bridge to async handler."""
    if _loop:
        asyncio.run_coroutine_threadsafe(handle_lark_event(data), _loop)


async def _on_bot_added_async(chat_id: str, group_name: str) -> None:
    """注册群聊后立即消化历史消息，不等待下一个轮询周期。"""
    await _processor.register_chat_by_id(chat_id, group_name)
    await _processor.process_now(chat_id)


def on_bot_added(data: P2ImChatMemberBotAddedV1) -> None:
    """Bot 被拉入新群时，注册 ChatMemorySpace 并立即批处理历史消息。"""
    if not (_loop and _processor and data.event):
        return
    chat_id = data.event.chat_id or ""
    group_name = data.event.name or ""
    if chat_id:
        asyncio.run_coroutine_threadsafe(
            _on_bot_added_async(chat_id, group_name), _loop
        )


async def main():
    global _loop, _processor
    _loop = asyncio.get_running_loop()
    _processor = BatchProcessor()

    try:
        await GraphitiClient.initialize()
    except Exception as e:
        logger.error("Graphiti init failed (memory write disabled): %s", e)

    # 自动获取机器人 open_id，供 fetch_messages 过滤 @机器人 查询消息
    if not os.getenv("FEISHU_BOT_OPEN_ID"):
        from feishu.api_client import FeishuAPIClient as _FAC
        try:
            bot_open_id = await _FAC().get_bot_open_id()
            if bot_open_id:
                os.environ["FEISHU_BOT_OPEN_ID"] = bot_open_id
                logger.info("Bot open_id 已自动获取: %s", bot_open_id)
        except Exception as e:
            logger.warning("自动获取 bot open_id 失败: %s", e)

    dispatcher = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .register_p2_im_chat_member_bot_added_v1(on_bot_added)
        .build()
    )

    ws_client = lark.ws.Client(
        app_id=os.getenv("FEISHU_APP_ID", ""),
        app_secret=os.getenv("FEISHU_APP_SECRET", ""),
        event_handler=dispatcher,
        log_level=lark.LogLevel.INFO,
    )

    # 批处理通道：后台周期任务，每 10 分钟对所有活跃群执行一次沉淀流水线
    asyncio.create_task(_processor.run())

    logger.info("Connecting to Feishu via WebSocket (no tunnel needed)...")
    ws_thread = threading.Thread(target=ws_client.start, daemon=True)
    ws_thread.start()

    logger.info("Bot is running. Press Ctrl+C to stop.")
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Shutting down.")


if __name__ == "__main__":
    asyncio.run(main())
