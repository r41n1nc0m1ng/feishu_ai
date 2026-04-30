"""
MemoryCard 生成层（对应需求文档 4.6 多粒度记忆生成层）。

流程：EvidenceBlock → LLM → CardOperation 判断 → 写入/更新 MemoryCard
支持四种操作：ADD / NOOP / PROGRESS / SUPERSEDE
"""
import json
import logging
import os
import re
from datetime import timezone
from typing import Optional

import httpx
from graphiti_core.nodes import EpisodeType

from memory.graphiti_client import GraphitiClient
from memory.schemas import (
    CardOperation,
    CardStatus,
    EvidenceBlock,
    MemoryCard,
    MemoryRelation,
    MemoryRelationType,
    MemoryType,
)
from memory import store

logger = logging.getLogger(__name__)

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
CARD_MODEL = os.getenv("LOCAL_MODEL", "qwen2.5:7b")

# 内存缓存：memory_id → MemoryCard
_card_cache: dict[str, MemoryCard] = {}
# 按 decision_object_key（归一化主键）索引，用于 SUPERSEDE 查找和 Topic 聚合
_cards_by_object: dict[str, MemoryCard] = {}


def _normalize_decision_key(text: str) -> str:
    """将 decision_object 归一化为稳定的业务主键，去除空白与非中英文字符，截断至 48 字。"""
    key = re.sub(r'[\s　]+', '_', text.strip())
    key = re.sub(r'[^\w一-鿿]', '', key)
    return key[:48].lower()


def _restore_cache() -> None:
    """启动时从 SQLite 恢复内存缓存。"""
    cards = store.load_all_memory_cards()
    for card in cards:
        _card_cache[card.memory_id] = card
        key = card.decision_object_key or _normalize_decision_key(card.decision_object)
        _cards_by_object[key] = card
    if cards:
        logger.info("MemoryCard 缓存已从 SQLite 恢复 | 共 %d 条", len(cards))

_CARD_PROMPT = """\
你是一个群聊决策记忆提炼助手。根据以下群聊消息片段，判断是否需要生成或更新记忆卡片。

消息片段：
{messages}

已有相关记忆（如有）：
{existing}

【输出规则】只返回 JSON，不要其他内容。

【必须输出 NOOP 的情况】以下内容不具备记忆价值，直接忽略：
- 纯粹的提问或疑问句（如"为什么不做X""之前怎么定的""X是什么"）
- 向机器人发起的查询（含 @机器人 的询问）
- 闲聊、表情包、单纯的"好的""收到""可以"
- 日程安排、待办事项

操作类型说明：
- ADD：新决策，之前没有相关记忆
- PROGRESS：讨论有价值但尚未形成一致决策
- SUPERSEDE：新内容覆盖了旧决策（decision_object 与已有记忆一致）
- NOOP：无记忆价值，忽略

输出格式（operation 为 NOOP 时只需返回 {{"operation": "NOOP"}}）：
{{
  "operation": "ADD" | "PROGRESS" | "SUPERSEDE" | "NOOP",
  "decision_object": "该决策所属的议题，一句话",
  "title": "一句话标题",
  "decision": "决策内容",
  "reason": "决策理由",
  "memory_type": "decision / tradeoff / rule / constraint / version_update / risk / progress"
}}
"""


class CardGenerator:

    async def generate(self, block: EvidenceBlock) -> Optional[MemoryCard]:
        """
        从 EvidenceBlock 生成 MemoryCard，写入缓存和 Graphiti。
        返回生成的 MemoryCard，NOOP 时返回 None。
        """
        messages_text = "\n".join(
            f"{m.sender_name or m.sender_id}  {m.timestamp.strftime('%H:%M')}：{m.text}"
            for m in block.messages
        )
        # 注入同 chat 下已有记忆供 LLM 参考
        existing_text = self._format_existing(block.chat_id)
        prompt = _CARD_PROMPT.format(messages=messages_text, existing=existing_text)

        raw = await self._call_llm(prompt)
        if not raw:
            return None

        operation_str = raw.get("operation", "NOOP").upper()
        try:
            operation = CardOperation(operation_str.lower())
        except ValueError:
            operation = CardOperation.NOOP

        logger.info(
            "CardGenerator result | chat=%s block_id=%s operation=%s object=%s title=%s",
            block.chat_id,
            block.block_id,
            operation.value,
            raw.get("decision_object", ""),
            raw.get("title", ""),
        )

        if operation == CardOperation.NOOP:
            logger.info("CardGenerator: NOOP | block=%s", block.block_id)
            return None

        # 构建新 MemoryCard
        raw_type = raw.get("memory_type", "decision")
        if raw_type not in MemoryType._value2member_map_:
            raw_type = "decision"

        decision_object = raw.get("decision_object", "未知议题")
        card = MemoryCard(
            chat_id=block.chat_id,
            decision_object=decision_object,
            decision_object_key=_normalize_decision_key(decision_object),
            title=raw.get("title", ""),
            decision=raw.get("decision", ""),
            reason=raw.get("reason", ""),
            memory_type=MemoryType(raw_type),
            status=CardStatus.ACTIVE,
            source_block_ids=[block.block_id],
        )

        if operation == CardOperation.SUPERSEDE:
            card = await self._handle_supersede(card)

        await self._save(card, block)
        return card

    async def _handle_supersede(self, new_card: MemoryCard) -> MemoryCard:
        """将旧卡片标记为 Deprecated，并建立 supersedes 关系。"""
        lookup_key = new_card.decision_object_key or _normalize_decision_key(new_card.decision_object)
        old = _cards_by_object.get(lookup_key)
        if not old:
            logger.info("SUPERSEDE 未找到旧卡片，按 ADD 处理 | object=%s", new_card.decision_object)
            return new_card

        old.status = CardStatus.DEPRECATED
        _card_cache[old.memory_id] = old
        try:
            store.save_memory_card(old)
        except Exception:
            logger.exception("SQLite 更新旧卡片状态失败 | memory_id=%s", old.memory_id)

        new_card.supersedes_memory_id = old.memory_id

        relation = MemoryRelation(
            chat_id=new_card.chat_id,
            source_id=new_card.memory_id,
            target_id=old.memory_id,
            relation_type=MemoryRelationType.SUPERSEDES,
        )
        try:
            store.save_relation(relation)
        except Exception:
            logger.exception("MemoryRelation 写入 SQLite 失败 | source=%s target=%s",
                             new_card.memory_id, old.memory_id)
        logger.info(
            "SUPERSEDE | 新卡片=%s 覆盖旧卡片=%s | object=%s",
            new_card.memory_id, old.memory_id, new_card.decision_object,
        )
        return new_card

    async def _save(self, card: MemoryCard, block: EvidenceBlock) -> None:
        """写入内存缓存、SQLite 并持久化到 Graphiti。"""
        _card_cache[card.memory_id] = card
        key = card.decision_object_key or _normalize_decision_key(card.decision_object)
        _cards_by_object[key] = card
        try:
            store.save_memory_card(card)
        except Exception:
            logger.exception("MemoryCard 写入 SQLite 失败 | memory_id=%s", card.memory_id)

        g = GraphitiClient()
        if not g.g:
            logger.warning("Graphiti 未初始化，MemoryCard 仅写入内存缓存")
            return

        episode_body = (
            f"议题：{card.decision_object}\n"
            f"标题：{card.title}\n"
            f"决策：{card.decision}\n"
            f"理由：{card.reason}\n"
            f"类型：{card.memory_type.value}\n"
            f"状态：{card.status.value}\n"
            f"来源块：{', '.join(card.source_block_ids)}"
        )

        ref_time = block.end_time
        if ref_time.tzinfo is None:
            ref_time = ref_time.astimezone(timezone.utc)

        try:
            await g.g.add_episode(
                name=f"card::{card.memory_id}::{card.decision_object}",
                episode_body=episode_body,
                source=EpisodeType.text,
                source_description=f"MemoryCard | 群聊 {card.chat_id}",
                reference_time=ref_time,
                group_id=card.chat_id,
            )
            logger.info(
                "MemoryCard 已保存 | memory_id=%s op=%s title=%s",
                card.memory_id, card.memory_type.value, card.title,
            )
        except Exception:
            logger.exception("MemoryCard 写入 Graphiti 失败 | memory_id=%s", card.memory_id)

    def _format_existing(self, chat_id: str) -> str:
        cards = [c for c in _card_cache.values() if c.chat_id == chat_id and c.status == CardStatus.ACTIVE]
        if not cards:
            return "（暂无）"
        return "\n".join(
            f"- [{c.decision_object}] {c.title}：{c.decision[:60]}"
            for c in cards[-5:]  # 最近 5 条，避免 context 过长
        )

    async def _call_llm(self, prompt: str) -> Optional[dict]:
        provider = os.getenv("MODEL_PROVIDER", "ollama").strip().lower()
        if provider == "openai" or os.getenv("OPENAI_API_KEY"):
            return await self._call_openai_compatible(prompt)
        return await self._call_ollama(prompt)

    async def _call_openai_compatible(self, prompt: str) -> Optional[dict]:
        api_key = os.getenv("OPENAI_API_KEY", "")
        base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        model = os.getenv("OPENAI_MODEL", CARD_MODEL)
        if not api_key:
            logger.error("CardGenerator 云端 LLM 调用失败: OPENAI_API_KEY 未配置")
            return None
        try:
            async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
                resp = await client.post(
                    f"{base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": prompt}],
                        "response_format": {"type": "json_object"},
                    },
                )
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]
                return json.loads(content)
        except Exception as e:
            logger.error("CardGenerator 云端 LLM 调用失败: %s", e)
            return None

    async def _call_ollama(self, prompt: str) -> Optional[dict]:
        try:
            async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
                resp = await client.post(
                    f"{OLLAMA_URL}/api/generate",
                    json={"model": CARD_MODEL, "prompt": prompt, "stream": False, "format": "json"},
                )
                resp.raise_for_status()
                return json.loads(resp.json().get("response", "{}"))
        except Exception as e:
            logger.error("CardGenerator LLM 调用失败: %s", e)
            return None


def get_card(memory_id: str) -> Optional[MemoryCard]:
    """模块级查询接口，供 retriever.get_card_by_id() 调用。"""
    return _card_cache.get(memory_id)


# 模块加载时从 SQLite 恢复缓存
_restore_cache()
