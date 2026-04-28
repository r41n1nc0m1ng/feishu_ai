"""
EvidenceBlock 存储层。

三写策略（优先级依次递减）：
1. 内存缓存（block_id → EvidenceBlock）：O(1) 查询，进程内最快
2. SQLite（memory_store.db）：持久化，重启后恢复内存缓存
3. Graphiti episode：语义索引，供"谁说的/原话在哪"的语义检索
"""
import logging
from datetime import timezone
from typing import Optional

from graphiti_core.nodes import EpisodeType

from memory.graphiti_client import GraphitiClient
from memory.schemas import EvidenceBlock
from memory import store

logger = logging.getLogger(__name__)

# 内存缓存：block_id → EvidenceBlock
_block_cache: dict[str, EvidenceBlock] = {}


def _restore_cache() -> None:
    """启动时从 SQLite 恢复内存缓存。"""
    blocks = store.load_all_evidence_blocks()
    for block in blocks:
        _block_cache[block.block_id] = block
    if blocks:
        logger.info("EvidenceBlock 缓存已从 SQLite 恢复 | 共 %d 条", len(blocks))


class EvidenceStore:

    async def save(self, block: EvidenceBlock) -> None:
        """将 EvidenceBlock 写入内存缓存、SQLite 和 Graphiti。"""
        # 1. 内存缓存
        _block_cache[block.block_id] = block

        # 2. SQLite 持久化（重启后可恢复）
        try:
            store.save_evidence_block(block)
        except Exception:
            logger.exception("EvidenceBlock 写入 SQLite 失败 | block_id=%s", block.block_id)

        # 3. Graphiti 语义索引
        g = GraphitiClient()
        if not g.g:
            logger.warning("Graphiti 未初始化，EvidenceBlock 仅写入本地存储")
            return

        lines = [
            f"{m.sender_name or m.sender_id}  {m.timestamp.strftime('%H:%M')}：{m.text}"
            for m in block.messages
        ]
        episode_body = "\n".join(lines)

        ref_time = block.end_time
        if ref_time.tzinfo is None:
            ref_time = ref_time.astimezone(timezone.utc)

        try:
            await g.g.add_episode(
                name=f"evidence::{block.block_id}",
                episode_body=episode_body,
                source=EpisodeType.message,
                source_description=f"EvidenceBlock {block.block_id} | 群聊 {block.chat_id}",
                reference_time=ref_time,
                group_id=block.chat_id,
            )
            logger.info(
                "EvidenceBlock 已保存 | block_id=%s chat=%s 消息数=%d",
                block.block_id, block.chat_id, len(block.messages),
            )
        except Exception:
            logger.exception("EvidenceBlock 写入 Graphiti 失败 | block_id=%s", block.block_id)

    async def get(self, block_id: str) -> Optional[EvidenceBlock]:
        """按 block_id 精确查询：内存命中直接返回，否则从 SQLite 查并回填缓存。"""
        if block_id in _block_cache:
            return _block_cache[block_id]
        block = store.load_evidence_block(block_id)
        if block:
            _block_cache[block.block_id] = block
        return block

    def all_block_ids(self) -> list[str]:
        return list(_block_cache.keys())


# 模块加载时从 SQLite 恢复缓存
_restore_cache()
