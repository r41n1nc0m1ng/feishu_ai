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
import numpy as np
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
EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text")

# 内存缓存：memory_id → MemoryCard
_card_cache: dict[str, MemoryCard] = {}
# 按 decision_object_key（归一化主键）索引，用于 SUPERSEDE 查找和 Topic 聚合
_cards_by_object: dict[str, MemoryCard] = {}
# embedding 缓存：memory_id → np.ndarray（方案 A：替换 Jaccard 匹配）
_card_embeddings: dict[str, np.ndarray] = {}


def _normalize_decision_key(text: str) -> str:
    """将 decision_object 归一化为稳定的业务主键，去除空白与非中英文字符，截断至 48 字。"""
    key = re.sub(r'[\s　]+', '_', text.strip())
    key = re.sub(r'[^\w一-鿿]', '', key)
    return key[:48].lower()


def _restore_cache() -> None:
    """启动时从 SQLite 恢复内存缓存（含 embedding）。"""
    cards = store.load_all_memory_cards()
    for card in cards:
        _card_cache[card.memory_id] = card
        key = card.decision_object_key or _normalize_decision_key(card.decision_object)
        _cards_by_object[key] = card

    emb_map = store.load_all_card_embeddings()
    restored = 0
    for memory_id, vec_bytes in emb_map.items():
        try:
            vec = np.frombuffer(vec_bytes, dtype=np.float32).copy()
            _card_embeddings[memory_id] = vec
            restored += 1
        except Exception as e:
            logger.warning("embedding 恢复失败 | memory_id=%s err=%s", memory_id, e)

    if cards:
        logger.info("MemoryCard 缓存已从 SQLite 恢复 | 卡片=%d embedding=%d", len(cards), restored)

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

    async def generate(self, block: EvidenceBlock, skip_graphiti: bool = False) -> Optional[MemoryCard]:
        """
        从 EvidenceBlock 生成 MemoryCard，写入缓存和 Graphiti。
        skip_graphiti=True 时跳过 Graphiti 写入，只写 SQLite + 内存缓存。
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

        await self._save(card, block, skip_graphiti=skip_graphiti)
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

    async def _save(self, card: MemoryCard, block: EvidenceBlock, skip_graphiti: bool = False) -> None:
        """写入内存缓存、SQLite，可选写入 Graphiti。"""
        _card_cache[card.memory_id] = card
        key = card.decision_object_key or _normalize_decision_key(card.decision_object)
        _cards_by_object[key] = card
        try:
            store.save_memory_card(card)
        except Exception:
            logger.exception("MemoryCard 写入 SQLite 失败 | memory_id=%s", card.memory_id)

        # 异步计算并缓存 embedding（用于方案 A：替换 Jaccard 匹配）
        await _cache_card_embedding(card)

        if skip_graphiti:
            return

        await _write_card_to_graphiti(card, ref_time=block.end_time)

    def _format_existing(self, chat_id: str) -> str:
        cards = [c for c in _card_cache.values() if c.chat_id == chat_id and c.status == CardStatus.ACTIVE]
        if not cards:
            return "（暂无）"
        return "\n".join(
            f"- [{c.decision_object}] {c.title}：{c.decision[:60]}"
            for c in cards[-5:]
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
                    json={
                        "model": CARD_MODEL,
                        "prompt": prompt,
                        "stream": False,
                        "format": "json",
                        "options": {"temperature": 0},
                    },
                )
                resp.raise_for_status()
                return json.loads(resp.json().get("response", "{}"))
        except Exception as e:
            logger.error("CardGenerator LLM 调用失败: %s", e)
            return None


async def _get_embedding(text: str) -> Optional[np.ndarray]:
    """调用 Ollama embed 接口，返回归一化向量。失败返回 None。"""
    try:
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            resp = await client.post(
                f"{OLLAMA_URL}/api/embed",
                json={"model": EMBED_MODEL, "input": text},
            )
            resp.raise_for_status()
            vec = resp.json().get("embeddings", [[]])[0]
            if not vec:
                return None
            arr = np.array(vec, dtype=np.float32)
            norm = np.linalg.norm(arr)
            return arr / norm if norm > 0 else arr
    except Exception as e:
        logger.warning("embedding 获取失败: %s", e)
        return None


async def _cache_card_embedding(card: MemoryCard) -> None:
    """为卡片计算 embedding，写入内存缓存并持久化到 SQLite。"""
    text = f"{card.title} {card.decision}"
    vec = await _get_embedding(text)
    if vec is not None:
        _card_embeddings[card.memory_id] = vec
        try:
            store.save_card_embedding(card.memory_id, vec.tobytes())
        except Exception as e:
            logger.warning("embedding 持久化失败 | memory_id=%s err=%s", card.memory_id, e)


async def _write_card_to_graphiti(card: MemoryCard, ref_time=None) -> None:
    """将单张 MemoryCard 写入 Graphiti（供批量并发写入复用）。"""
    g = GraphitiClient()
    if not g.g:
        logger.warning("Graphiti 未初始化，跳过写入 | memory_id=%s", card.memory_id)
        return

    episode_body = (
        f"议题：{card.decision_object}\n"
        f"标题：{card.title}\n"
        f"决策：{card.decision}\n"
        f"理由：{card.reason}\n"
        f"类型：{card.memory_type.value}\n"
        f"状态：{card.status.value}"
    )

    if ref_time is None:
        ref_time = card.created_at
    if ref_time.tzinfo is None:
        ref_time = ref_time.astimezone(timezone.utc)

    try:
        await g.g.add_episode(
            name=f"card::{card.memory_id}::{card.title}",
            episode_body=episode_body,
            source=EpisodeType.text,
            source_description=f"MemoryCard | 群聊 {card.chat_id}",
            reference_time=ref_time,
            group_id=card.chat_id,
        )
        logger.info("MemoryCard 已写入 Graphiti | memory_id=%s title=%s",
                    card.memory_id, card.title)
    except Exception:
        logger.exception("MemoryCard 写入 Graphiti 失败 | memory_id=%s", card.memory_id)


def get_card(memory_id: str) -> Optional[MemoryCard]:
    """模块级查询接口，供 retriever.get_card_by_id() 调用。"""
    return _card_cache.get(memory_id)


def clear_cache(chat_id: str) -> None:
    """清除指定群的内存缓存（benchmark 重置用）。SQLite embedding 由 clear_chat_data 负责。"""
    keys = [k for k, v in _card_cache.items() if v.chat_id == chat_id]
    for k in keys:
        del _card_cache[k]
        _card_embeddings.pop(k, None)
    obj_keys = [k for k, v in _cards_by_object.items() if v.chat_id == chat_id]
    for k in obj_keys:
        del _cards_by_object[k]
    logger.info("MemoryCard 缓存已清除 | chat_id=%s 共 %d 条", chat_id, len(keys))


# 模块加载时从 SQLite 恢复缓存
_restore_cache()
