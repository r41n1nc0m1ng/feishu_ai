import asyncio
import logging
import os
import threading

from dotenv import load_dotenv
load_dotenv()

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

from feishu.event_handler import handle_lark_event
from memory.graphiti_client import GraphitiClient

logging.basicConfig(level=logging.INFO)
logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)
logging.getLogger("neo4j").setLevel(logging.ERROR)
logger = logging.getLogger(__name__)

_loop: asyncio.AbstractEventLoop | None = None


def on_message(data: P2ImMessageReceiveV1) -> None:
    """Sync callback from lark SDK — bridge to async handler."""
    if _loop:
        asyncio.run_coroutine_threadsafe(handle_lark_event(data), _loop)


async def main():
    global _loop
    _loop = asyncio.get_running_loop()

    try:
        await GraphitiClient.initialize()
    except Exception as e:
        logger.error("Graphiti init failed (memory write disabled): %s", e)

    dispatcher = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .build()
    )

    ws_client = lark.ws.Client(
        app_id=os.getenv("FEISHU_APP_ID", ""),
        app_secret=os.getenv("FEISHU_APP_SECRET", ""),
        event_handler=dispatcher,
        log_level=lark.LogLevel.INFO,
    )

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
