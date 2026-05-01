"""
MemoryCard 生命周期管理层（P1 实现）。

基于 SQLite 状态更新实现生命周期，不依赖 Graphiti node-update API
（Graphiti 未暴露直接的节点状态修改接口）。

职责：
- deprecate()：将指定卡片标记为 Deprecated，同步更新缓存
- expire_chat_memories()：批量废弃某群某时间点前的 Active 卡片
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from memory import store
from memory.schemas import CardStatus

logger = logging.getLogger(__name__)


class MemoryLifecycle:

    async def deprecate(self, chat_id: str, memory_id: str) -> bool:
        """
        将指定 MemoryCard 标记为 Deprecated。
        返回 True 表示找到并更新；False 表示未找到或 chat_id 不匹配。
        """
        card = store.load_memory_card(memory_id)
        if not card or card.chat_id != chat_id:
            logger.warning("deprecate: not found | memory_id=%s chat=%s", memory_id, chat_id)
            return False
        if card.status == CardStatus.DEPRECATED:
            return True  # 幂等

        card.status = CardStatus.DEPRECATED
        card.updated_at = datetime.now(timezone.utc)
        store.save_memory_card(card)

        # 同步内存缓存（若存在）
        try:
            from memory.card_generator import _card_cache
            if memory_id in _card_cache:
                _card_cache[memory_id].status = CardStatus.DEPRECATED
        except Exception:
            pass

        logger.info("Memory deprecated | chat=%s memory_id=%s title=%s",
                    chat_id, memory_id, card.title)
        return True

    async def expire_chat_memories(
        self, chat_id: str, cutoff: Optional[datetime] = None
    ) -> int:
        """
        将某群中早于 cutoff 时间的全部 Active 卡片标记为 Deprecated。
        cutoff=None 时废弃该群所有 Active 卡片（用于项目结束后归档）。
        返回实际废弃的数量。
        """
        cards = store.get_cards_for_chat(chat_id)
        now = datetime.now(timezone.utc)
        count = 0

        for card in cards:
            if card.status != CardStatus.ACTIVE:
                continue
            if cutoff:
                card_ts = card.created_at
                if card_ts.tzinfo is None:
                    card_ts = card_ts.replace(tzinfo=timezone.utc)
                if card_ts >= cutoff:
                    continue

            card.status = CardStatus.DEPRECATED
            card.updated_at = now
            store.save_memory_card(card)
            count += 1

        if count:
            logger.info("expire_chat_memories | chat=%s expired=%d cutoff=%s",
                        chat_id, count, cutoff)
        return count
