"""
TopicSummary 生成层（三层记忆结构 高粒度层）。

P1 策略：
- 数据源：同群 Active + 非 PROGRESS 的 MemoryCard（优先走内存缓存，冷启动读 SQLite）
- 聚合：不做复杂聚类，直接把卡片列表交给 LLM，让 LLM 自行归并为 2-6 个主题
- 存储：只写 SQLite（P1 约定，不写 Graphiti）
- 触发：由 batch_processor 在本轮新增 Active+非PROGRESS 卡片时调用，失败不阻断主流程
"""
import json
import logging
import os
from typing import List, Optional

import httpx

from memory import store
from memory.schemas import CardStatus, MemoryType, TopicSummary

logger = logging.getLogger(__name__)

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
TOPIC_MODEL = os.getenv("LOCAL_MODEL", "qwen2.5:7b")
MAX_CARDS_PER_REBUILD = int(os.getenv("TOPIC_MAX_CARDS", "20"))

_TOPIC_PROMPT = """\
你是一个群聊决策记忆摘要助手。以下是一个飞书群聊中已沉淀的决策记忆卡片（JSON 数组）。

请将这些卡片归并为若干主题（2 至 6 个），每个主题生成一条摘要。

要求：
- 只合并真正相关的卡片，不要强行把无关决策归为同一主题
- topic：2-4 个词的主题标签，如"MVP产品边界"
- summary：当前生效状态的简洁描述（1-3 句话，只说现在怎么定的，不说"讨论了"）
- covered_memory_ids：该主题包含的 memory_id 列表（从输入中取，不要捏造）

卡片列表：
{cards_json}

【输出规则】只返回 JSON 数组，不要其他内容，不要包裹在对象里：
[
  {{
    "topic": "主题标签",
    "summary": "当前状态描述",
    "covered_memory_ids": ["memory_id_1", "memory_id_2"]
  }}
]
"""


class TopicManager:
    """高粒度主题摘要层：Active+非PROGRESS卡片 → LLM归并 → SQLite。"""

    async def get_topics(self, chat_id: str) -> List[TopicSummary]:
        """返回当前群的全部 TopicSummary，按更新时间倒序。"""
        return store.load_topics_by_chat(chat_id)

    async def upsert_topic(self, chat_id: str, topic: TopicSummary) -> None:
        """写入或更新单条 TopicSummary。"""
        store.save_topic_summary(topic)
        logger.info("TopicSummary upserted | chat=%s topic=%s", chat_id, topic.topic)

    async def rebuild_topics(self, chat_id: str) -> List[TopicSummary]:
        """
        重建当前群的全部 TopicSummary。
        流程：取 Active+非PROGRESS 卡片 → LLM 归并 → 清除旧摘要 → 写入新摘要。
        """
        cards = self._get_eligible_cards(chat_id)
        if len(cards) < 2:
            logger.info("TopicSummary rebuild skipped | chat=%s cards=%d (< 2)",
                        chat_id, len(cards))
            return []

        logger.info("TopicSummary rebuilding | chat=%s eligible_cards=%d",
                    chat_id, len(cards))

        raw = await self._call_llm(cards)
        if not raw:
            logger.warning("TopicSummary LLM returned empty | chat=%s", chat_id)
            return []

        summaries: List[TopicSummary] = []
        valid_ids_set = {c.memory_id for c in cards}

        for item in raw:
            topic_text = (item.get("topic") or "").strip()
            summary_text = (item.get("summary") or "").strip()
            covered = [mid for mid in item.get("covered_memory_ids", [])
                       if mid in valid_ids_set]
            if not topic_text or not summary_text or not covered:
                continue
            summaries.append(TopicSummary(
                chat_id=chat_id,
                topic=topic_text,
                summary=summary_text,
                covered_memory_ids=covered,
            ))

        if not summaries:
            logger.warning("TopicSummary rebuild produced 0 valid topics | chat=%s", chat_id)
            return []

        store.delete_topics_by_chat(chat_id)
        for t in summaries:
            store.save_topic_summary(t)
        logger.info("TopicSummary rebuild done | chat=%s topics=%d", chat_id, len(summaries))
        return summaries

    # ── 内部辅助 ──────────────────────────────────────────────────────────────

    def _get_eligible_cards(self, chat_id: str) -> list:
        """取同群 Active+非PROGRESS 卡片，直接读 SQLite（真相源）。
        不走 _card_cache：cache 索引是给 SUPERSEDE 查找设计的，以 decision_object_key
        为键，重复写入同一议题时会覆盖旧值，不适合做全量枚举。
        """
        all_cards = store.get_cards_for_chat(chat_id)
        return [
            c for c in all_cards
            if c.status == CardStatus.ACTIVE
            and c.memory_type != MemoryType.PROGRESS
        ][:MAX_CARDS_PER_REBUILD]

    async def _call_llm(self, cards: list) -> Optional[list]:
        cards_json = json.dumps(
            [{"memory_id": c.memory_id, "decision_object": c.decision_object,
              "title": c.title, "decision": c.decision, "reason": c.reason}
             for c in cards],
            ensure_ascii=False, indent=2,
        )
        prompt = _TOPIC_PROMPT.format(cards_json=cards_json)
        provider = os.getenv("MODEL_PROVIDER", "ollama").strip().lower()
        if provider == "openai" or os.getenv("OPENAI_API_KEY"):
            return await self._call_openai_compatible(prompt)
        return await self._call_ollama(prompt)

    def _parse_response(self, parsed) -> Optional[list]:
        """兼容 LLM 把数组包进 {"topics":[...]} 等对象的情况。"""
        if isinstance(parsed, list):
            return parsed
        for key in ("topics", "summaries", "result", "data"):
            if isinstance(parsed.get(key), list):
                return parsed[key]
        return None

    async def _call_openai_compatible(self, prompt: str) -> Optional[list]:
        api_key = os.getenv("OPENAI_API_KEY", "")
        base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        model = os.getenv("OPENAI_MODEL", TOPIC_MODEL)
        if not api_key:
            logger.error("TopicManager: OPENAI_API_KEY 未配置")
            return None
        try:
            async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
                resp = await client.post(
                    f"{base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={"model": model,
                          "messages": [{"role": "user", "content": prompt}],
                          "response_format": {"type": "json_object"}},
                )
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]
                return self._parse_response(json.loads(content))
        except Exception as e:
            logger.error("TopicManager 云端 LLM 调用失败: %s", e)
            return None

    async def _call_ollama(self, prompt: str) -> Optional[list]:
        try:
            async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
                resp = await client.post(
                    f"{OLLAMA_URL}/api/generate",
                    json={"model": TOPIC_MODEL, "prompt": prompt,
                          "stream": False, "format": "json"},
                )
                resp.raise_for_status()
                return self._parse_response(json.loads(resp.json().get("response", "{}")))
        except Exception as e:
            logger.error("TopicManager Ollama 调用失败: %s", e)
            return None
