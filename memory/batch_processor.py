"""
批处理主调度器（对应需求文档 4.4 批量消息获取层）。

职责：
- register_chat()：作为 dispatcher 的 legacy_ingest 钩子，发现并注册活跃群聊
- run()：asyncio 周期任务，每 POLL_INTERVAL 秒对所有已注册群执行一次完整批处理流水线

流水线：拉取增量消息 → 事件切分 → 存储 EvidenceBlock → 生成 MemoryCard
"""
import asyncio
import logging
import os

# Per-chat processing locks: prevents duplicate runs when process_now and run()
# overlap (e.g. bot-join fires while the periodic loop is also awake).
_process_locks: dict[str, asyncio.Lock] = {}

from feishu.api_client import FeishuAPIClient, InvalidChatError
from memory.card_generator import CardGenerator
from memory.evidence_store import EvidenceStore
from memory.schemas import CardStatus, ChatMemorySpace, FeishuMessage, FetchBatch, MemoryType
from memory import store
from preprocessor.event_segmenter import segment_async
from realtime.triggers import has_explicit_bot_mention


def _should_skip_message(text: str, sender_id: str) -> bool:
    """
    Returns True if this message should be excluded from batch memory extraction.

    Active filters:
      - Bot's own replies (sender_id == FEISHU_BOT_OPEN_ID)
      - Messages that @mention the bot (already handled by realtime handler)

    Commented out (pattern-based query detection — disable until realtime
    trigger is re-enabled for query patterns):
      # from realtime.triggers import is_explicit_query, is_source_query, is_summary_query, is_version_query
      # is_source_query(text) or is_summary_query(text)
      # or is_version_query(text) or is_explicit_query(text)
    """
    bot_id = os.getenv("FEISHU_BOT_OPEN_ID", "")
    if bot_id and sender_id == bot_id:
        return True
    if has_explicit_bot_mention(text):
        return True
    return False


logger = logging.getLogger(__name__)

POLL_INTERVAL = int(os.getenv("BATCH_POLL_INTERVAL", "60"))  # 轮询间隔（秒），默认 60 秒
MIN_MESSAGES = 1        # 增量消息数低于此值则跳过本轮处理
LOOKBACK_MESSAGES = int(os.getenv("FETCH_LOOKBACK_MESSAGES", "5"))

# 活跃群聊注册表：chat_id → ChatMemorySpace
_active_chats: dict[str, ChatMemorySpace] = {}
_cache_restored = False


def _restore_active_chats() -> None:
    """从 SQLite 恢复活跃群聊注册表，确保重启后仍会轮询已知群聊。只执行一次。"""
    global _cache_restored
    if _cache_restored:
        return
    _cache_restored = True
    spaces = store.load_all_chat_spaces()
    for space in spaces:
        _active_chats[space.chat_id] = space
    if spaces:
        logger.info("活跃群聊已从 SQLite 恢复 | 共 %d 个", len(spaces))


class BatchProcessor:

    def __init__(self):
        self._feishu = FeishuAPIClient()
        self._evidence_store = EvidenceStore()
        self._card_generator = CardGenerator()
        _restore_active_chats()  # 首次实例化时从 SQLite 恢复（幂等）

    # ── 注册接口 ──────────────────────────────────────────────────────────────

    async def register_chat_by_id(self, chat_id: str, group_name: str = "") -> None:
        """
        直接按 chat_id 注册群聊（供 bot 入群事件调用）。
        已注册的群聊跳过；新注册的群聊同步写入 SQLite。
        """
        if chat_id in _active_chats:
            return
        if not group_name:
            try:
                info = await self._feishu.get_chat_info(chat_id)
                group_name = info.get("name", "")
            except Exception:
                logger.warning("获取群名称失败，chat_id=%s", chat_id)
        space = ChatMemorySpace(chat_id=chat_id, group_name=group_name)
        _active_chats[chat_id] = space
        try:
            store.save_chat_space(space)
        except Exception:
            logger.exception("ChatMemorySpace 写入 SQLite 失败 | chat_id=%s", chat_id)
        logger.info("新群聊已注册 | chat_id=%s name=%s", chat_id, group_name)

    async def register_chat(self, message: FeishuMessage) -> None:
        """
        将消息来源的 chat_id 注册到活跃群聊表（由 dispatcher legacy_ingest 钩子调用）。
        """
        await self.register_chat_by_id(message.chat_id)

    async def process_now(self, chat_id: str) -> None:
        """
        立即对指定群聊执行一次完整批处理流水线。
        用于 bot 入群后立即消化历史消息，不等待周期轮询。
        """
        if chat_id not in _active_chats:
            logger.warning("process_now: chat_id 未注册，跳过 | chat_id=%s", chat_id)
            return
        logger.info("Bot 入群触发立即批处理 | chat_id=%s", chat_id)
        try:
            await self._process_chat(chat_id)
        except Exception:
            logger.exception("立即批处理异常 | chat_id=%s", chat_id)

    # ── 周期任务（asyncio.create_task 启动）──────────────────────────────────

    async def run(self) -> None:
        """主循环：每 POLL_INTERVAL 秒对所有已注册群执行一次批处理。"""
        logger.info("BatchProcessor 已启动，轮询间隔 %ds | 已注册群聊 %d 个",
                    POLL_INTERVAL, len(_active_chats))
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            if not _active_chats:
                logger.debug("暂无已注册群聊，跳过本轮轮询")
                continue
            logger.info("开始批处理轮询，共 %d 个群聊", len(_active_chats))
            for chat_id in list(_active_chats.keys()):
                try:
                    await self._process_chat(chat_id)
                except Exception:
                    logger.exception("批处理异常 | chat_id=%s", chat_id)

    # ── 单群处理流水线 ────────────────────────────────────────────────────────

    async def _process_chat(self, chat_id: str) -> None:
        lock = _process_locks.setdefault(chat_id, asyncio.Lock())
        if lock.locked():
            logger.info("_process_chat already running, skipping | chat=%s", chat_id)
            return
        async with lock:
            await self.__process_chat_inner(chat_id)

    async def __process_chat_inner(self, chat_id: str) -> None:
        space = _active_chats[chat_id]
        logger.info("Batch process start | chat=%s last_fetch_at=%s", chat_id, space.last_fetch_at)

        # 1. 拉取增量消息（API 是秒级闭区间，客户端用毫秒精度过滤重复）
        try:
            messages, last_raw_ts = await self._feishu.fetch_messages(
                chat_id, start_time=space.last_fetch_at
            )
        except InvalidChatError:
            _active_chats.pop(chat_id, None)
            try:
                store.delete_chat_space(chat_id)
            except Exception:
                logger.exception("无效群聊清理失败 | chat_id=%s", chat_id)
            logger.warning("Invalid chat unregistered | chat=%s", chat_id)
            return
        if space.last_fetch_at:
            new_messages = [m for m in messages if m.timestamp > space.last_fetch_at]
            context_messages = [m for m in messages if m.timestamp <= space.last_fetch_at]
            if new_messages:
                messages = context_messages[-LOOKBACK_MESSAGES:] + new_messages
                logger.info(
                    "Lookback context applied | chat=%s context=%d new=%d total=%d",
                    chat_id,
                    min(len(context_messages), LOOKBACK_MESSAGES),
                    len(new_messages),
                    len(messages),
                )
            else:
                messages = []

        # 过滤 bot 自身回复和 @bot 消息（不应进入记忆提取管道）
        skipped = [m for m in messages if _should_skip_message(m.text, m.sender_id)]
        if skipped:
            logger.info("Skipped %d bot/@bot messages | chat=%s", len(skipped), chat_id)
        messages = [m for m in messages if not _should_skip_message(m.text, m.sender_id)]

        # 即使消息全部被过滤（机器人回复/查询语句），仍推进游标，避免重复拉取
        if len(messages) < MIN_MESSAGES:
            if last_raw_ts and (not space.last_fetch_at or last_raw_ts > space.last_fetch_at):
                space.last_fetch_at = last_raw_ts
                store.save_chat_space(space)
                logger.info("No effective messages; cursor advanced | chat=%s cursor=%s", chat_id, last_raw_ts)
            return

        # 2. 构造 FetchBatch，游标取所有原始消息的最后时间（含被过滤的）
        fetch_end = last_raw_ts or messages[-1].timestamp
        batch = FetchBatch(
            chat_id=chat_id,
            fetch_start=space.last_fetch_at or messages[0].timestamp,
            fetch_end=fetch_end,
            messages=messages,
        )

        # 3. 事件切分 → EvidenceBlock 列表
        blocks = await segment_async(batch)
        if space.last_fetch_at:
            blocks = [
                block for block in blocks
                if any(msg.timestamp > space.last_fetch_at for msg in getattr(block, "messages", []))
            ]
        logger.info("事件切分完成 | chat_id=%s 消息数=%d 块数=%d",
                    chat_id, len(messages), len(blocks))

        # 4. 逐块存储证据 + 生成记忆卡片
        new_active_cards = 0
        for block in blocks:
            logger.info(
                "Processing EvidenceBlock | chat=%s block_id=%s start=%s end=%s messages=%d",
                chat_id,
                getattr(block, "block_id", ""),
                getattr(block, "start_time", ""),
                getattr(block, "end_time", ""),
                len(getattr(block, "messages", [])),
            )
            await self._evidence_store.save(block)
            card = await self._card_generator.generate(block)
            if card:
                logger.info("MemoryCard 生成 | chat_id=%s title=%s op=%s",
                            chat_id, card.title, card.memory_type.value)
                if card.status == CardStatus.ACTIVE and card.memory_type != MemoryType.PROGRESS:
                    new_active_cards += 1
            else:
                logger.info(
                    "MemoryCard skipped | chat=%s block_id=%s",
                    chat_id,
                    getattr(block, "block_id", ""),
                )

        # 5. 更新游标并持久化到 SQLite
        space.last_fetch_at = fetch_end
        try:
            store.save_chat_space(space)
        except Exception:
            logger.exception("游标写入 SQLite 失败 | chat_id=%s", chat_id)
        logger.info("批处理完成 | chat_id=%s 游标更新至 %s", chat_id, fetch_end)

        # 6. 按需触发 TopicSummary 重建（失败不阻断主流程）
        if new_active_cards > 0:
            try:
                from memory.topic_manager import TopicManager
                await TopicManager().rebuild_topics(chat_id)
            except Exception:
                logger.exception("TopicSummary 重建失败，跳过 | chat_id=%s", chat_id)


# 注：_restore_active_chats() 在 BatchProcessor.__init__() 中调用，不在模块加载时执行
