"""
EvidenceBlock 存储层。

双写策略：
- 内存缓存（block_id → EvidenceBlock）：供 retriever.expand_evidence() O(1) 查询
- Graphiti episode：供语义搜索（"谁说的/原话在哪"）
进程重启后内存缓存清空，Graphiti 数据持久保留，P0 阶段可接受。
"""
import json
import logging
from datetime import timezone
from typing import Optional

from graphiti_core.nodes import EpisodeType

from memory.graphiti_client import GraphitiClient
from memory.schemas import EvidenceBlock

logger = logging.getLogger(__name__)

# 内存缓存：block_id → EvidenceBlock
_block_cache: dict[str, EvidenceBlock] = {}


class EvidenceStore:

    async def save(self, block: EvidenceBlock) -> None:
        """将 EvidenceBlock 写入内存缓存并持久化到 Graphiti。"""
        _block_cache[block.block_id] = block

        g = GraphitiClient()
        if not g.g:
            logger.warning("Graphiti 未初始化，EvidenceBlock 仅写入内存缓存")
            return

        # 将消息列表序列化为可读文本，保真存储
        lines = [
            f"{m.sender_name or m.sender_id}  {m.timestamp.strftime('%H:%M')}：{m.text}"
            for m in block.messages
        ]
        episode_body = "\n".join(lines)

        ref_time = block.end_time
        if ref_time.tzinfo is None:
            ref_time = ref_time.replace(tzinfo=timezone.utc)

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
        """按 block_id 精确查询，优先走内存缓存。"""
        return _block_cache.get(block_id)

    def all_block_ids(self) -> list[str]:
        return list(_block_cache.keys())
