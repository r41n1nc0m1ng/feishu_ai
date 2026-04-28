"""
批处理主调度器（对应需求文档 4.4 批量消息获取层）。

职责：
- register_chat()：作为 dispatcher 的 legacy_ingest 钩子，发现并注册活跃群聊
- run()：asyncio 周期任务，每 POLL_INTERVAL 秒对所有活跃群执行一次完整批处理流水线

流水线：拉取增量消息 → 事件切分 → 存储 EvidenceBlock → 生成 MemoryCard
"""
import asyncio
import logging

from feishu.api_client import FeishuAPIClient
from memory.card_generator import CardGenerator
from memory.evidence_store import EvidenceStore
from memory.schemas import ChatMemorySpace, FeishuMessage, FetchBatch
from preprocessor.event_segmenter import segment

logger = logging.getLogger(__name__)

POLL_INTERVAL = 600     # 每 10 分钟轮询一次
MIN_MESSAGES = 1        # 增量消息数低于此值则跳过本轮处理

# 活跃群聊注册表：chat_id → ChatMemorySpace
_active_chats: dict[str, ChatMemorySpace] = {}


class BatchProcessor:

    def __init__(self):
        self._feishu = FeishuAPIClient()
        self._evidence_store = EvidenceStore()
        self._card_generator = CardGenerator()

    # ── 注册接口（由 dispatcher 的 legacy_ingest 钩子调用）────────────────────

    async def register_chat(self, message: FeishuMessage) -> None:
        """
        将消息来源的 chat_id 注册到活跃群聊表。
        首次注册时尝试获取群名称；已注册的群聊直接跳过。
        """
        if message.chat_id in _active_chats:
            return

        group_name = ""
        try:
            info = await self._feishu.get_chat_info(message.chat_id)
            group_name = info.get("name", "")
        except Exception:
            logger.warning("获取群名称失败，chat_id=%s", message.chat_id)

        _active_chats[message.chat_id] = ChatMemorySpace(
            chat_id=message.chat_id,
            group_name=group_name,
        )
        logger.info("新群聊已注册 | chat_id=%s name=%s", message.chat_id, group_name)

    # ── 周期任务（asyncio.create_task 启动）──────────────────────────────────

    async def run(self) -> None:
        """主循环：每 POLL_INTERVAL 秒对所有已注册群执行一次批处理。"""
        logger.info("BatchProcessor 已启动，轮询间隔 %ds", POLL_INTERVAL)
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            if not _active_chats:
                continue
            logger.info("开始批处理轮询，共 %d 个群聊", len(_active_chats))
            for chat_id in list(_active_chats.keys()):
                try:
                    await self._process_chat(chat_id)
                except Exception:
                    logger.exception("批处理异常 | chat_id=%s", chat_id)

    # ── 单群处理流水线 ────────────────────────────────────────────────────────

    async def _process_chat(self, chat_id: str) -> None:
        space = _active_chats[chat_id]

        # 1. 拉取增量消息
        messages = await self._feishu.fetch_messages(chat_id, start_time=space.last_fetch_at)
        if len(messages) < MIN_MESSAGES:
            logger.debug("无增量消息，跳过 | chat_id=%s", chat_id)
            return

        # 2. 构造 FetchBatch
        fetch_end = messages[-1].timestamp
        batch = FetchBatch(
            chat_id=chat_id,
            fetch_start=space.last_fetch_at or messages[0].timestamp,
            fetch_end=fetch_end,
            messages=messages,
        )

        # 3. 事件切分 → EvidenceBlock 列表
        blocks = segment(batch)
        logger.info("事件切分完成 | chat_id=%s 消息数=%d 块数=%d", chat_id, len(messages), len(blocks))

        # 4. 逐块存储证据 + 生成记忆卡片
        for block in blocks:
            await self._evidence_store.save(block)
            card = await self._card_generator.generate(block)
            if card:
                logger.info(
                    "MemoryCard 生成 | chat_id=%s title=%s op=%s",
                    chat_id, card.title, card.memory_type.value,
                )

        # 5. 更新游标
        space.last_fetch_at = fetch_end
        logger.info("批处理完成 | chat_id=%s 游标更新至 %s", chat_id, fetch_end)
