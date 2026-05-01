"""
冲突检测层（Option B 架构）。

三段式，触发条件互斥：
  Stage 1 — 规则：decision_object_key 精确匹配（无外部依赖，始终可用）
  Stage 2 — 语义：Graphiti 召回候选 → LLM 判断是否同议题
  Fallback — 降级：Jaccard 直扫 SQLite（仅当 Stage 2 服务不可用时触发）

Jaccard 不在 LLM 判断"否"之后运行，只在 Graphiti/LLM 抛出异常时接管。
"""
import json
import logging
import os
import re
from typing import Optional

import httpx

from memory.retriever import MemoryRetriever
from memory.schemas import CardStatus, ExtractedMemory

logger = logging.getLogger(__name__)

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
CONFLICT_MODEL = os.getenv("LOCAL_MODEL", "qwen2.5:7b")

_CONFLICT_PROMPT = """\
你是一个决策记忆管理助手。

【任务】
判断"新记忆"与"已有记忆"的关系，决定是否需要将已有记忆标记为废弃。

新记忆：
  议题：{new_object}
  决策：{new_decision}

已有记忆：
  议题：{existing_object}
  决策：{existing_decision}

【判断步骤】
1. 这两条记忆是否在讨论同一个决策问题？（忽略措辞差异，看本质）
2. 如果是同一问题，新记忆与已有记忆的立场/结论是否相悖或矛盾？

【关系类型】
- same_and_conflict：同一问题，立场相悖 → 已有记忆应被废弃
- same_no_conflict：同一问题，立场一致（补充或重复）→ 不废弃
- different：不同问题 → 不废弃

【输出规则】只返回 JSON，不要其他内容：
{{"relation": "same_and_conflict | same_no_conflict | different", "reason": "一句话说明判断依据"}}
"""


def _simple_key(text: str) -> str:
    key = re.sub(r'[\s　]+', '_', text.strip())
    key = re.sub(r'[^\w一-鿿_]', '', key)
    return key[:48].lower()


class ConflictDetector:
    """
    检测新提取的记忆是否与同群已有 Active 记忆冲突，
    用于辅助 card_generator 判断是否触发 SUPERSEDE。
    """

    async def find_conflict(
        self, chat_id: str, new_memory: ExtractedMemory
    ) -> Optional[dict]:
        """
        返回冲突的已有记忆 dict（含 memory_id / title / decision / reason），
        未发现冲突返回 None。
        """
        # ── Stage 1：规则 — key 精确匹配 ─────────────────────────────────────
        result = self._key_match(chat_id, new_memory)
        if result:
            return result

        # ── Stage 2：语义 — Graphiti 召回 + LLM 判断 ─────────────────────────
        try:
            result = await self._semantic_llm_check(chat_id, new_memory)
            return result  # None 表示"确认无冲突"，dict 表示"发现冲突"
        except Exception as e:
            logger.warning(
                "Stage 2 unavailable, falling back to Jaccard | reason=%s", e
            )

        # ── Fallback：Jaccard — 仅在 Stage 2 服务不可用时触发 ─────────────────
        return self._jaccard_fallback(chat_id, new_memory)

    # ── Stage 1 ───────────────────────────────────────────────────────────────

    def _key_match(self, chat_id: str, new_memory: ExtractedMemory) -> Optional[dict]:
        from memory import store
        new_key = _simple_key(new_memory.title)
        active = [c for c in store.get_cards_for_chat(chat_id)
                  if c.status == CardStatus.ACTIVE]
        for card in active:
            card_key = card.decision_object_key or _simple_key(card.decision_object)
            if card_key and card_key == new_key:
                logger.info("Conflict key-match | new='%s' existing='%s'",
                            new_memory.title, card.title)
                return {"memory_id": card.memory_id, "title": card.title,
                        "decision": card.decision, "reason": "key_match"}
        return None

    # ── Stage 2 ───────────────────────────────────────────────────────────────

    async def _semantic_llm_check(
        self, chat_id: str, new_memory: ExtractedMemory
    ) -> Optional[dict]:
        """
        Graphiti 召回候选，取 top-1 让 LLM 判断是否同议题。
        Graphiti 未初始化或检索失败时抛出异常，由调用方触发 Fallback。
        """
        from memory.graphiti_client import GraphitiClient
        gc = GraphitiClient()
        if not gc.g:
            raise RuntimeError("Graphiti not initialized")

        candidates = await MemoryRetriever().retrieve(chat_id, new_memory.title, limit=3)
        if not candidates:
            return None  # 语义上无相关候选，判定无冲突

        top = candidates[0]
        is_conflict = await self._llm_judge(new_memory, top)
        if is_conflict:
            logger.info("Conflict semantic+LLM | new='%s' existing='%s'",
                        new_memory.title, top.title)
            return {"memory_id": top.memory_id, "title": top.title,
                    "decision": top.decision, "reason": "semantic_llm"}
        return None

    async def _llm_judge(self, new_memory: ExtractedMemory, existing) -> bool:
        prompt = _CONFLICT_PROMPT.format(
            new_object=new_memory.title,
            new_decision=new_memory.decision,
            existing_object=getattr(existing, "decision_object", existing.title),
            existing_decision=existing.decision,
        )
        provider = os.getenv("MODEL_PROVIDER", "ollama").strip().lower()
        try:
            if provider == "openai" or os.getenv("OPENAI_API_KEY"):
                raw = await self._call_openai(prompt)
            else:
                raw = await self._call_ollama(prompt)
            return bool(raw and raw.get("conflict"))
        except Exception as e:
            logger.error("ConflictDetector LLM call failed: %s", e)
            raise  # 让上层触发 Fallback

    async def _call_openai(self, prompt: str) -> Optional[dict]:
        api_key = os.getenv("OPENAI_API_KEY", "")
        base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        model = os.getenv("OPENAI_MODEL", CONFLICT_MODEL)
        async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": model,
                      "messages": [{"role": "user", "content": prompt}],
                      "response_format": {"type": "json_object"}},
            )
            resp.raise_for_status()
            return json.loads(resp.json()["choices"][0]["message"]["content"])

    async def _call_ollama(self, prompt: str) -> Optional[dict]:
        async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
            resp = await client.post(
                f"{OLLAMA_URL}/api/generate",
                json={"model": CONFLICT_MODEL, "prompt": prompt,
                      "stream": False, "format": "json"},
            )
            resp.raise_for_status()
            return json.loads(resp.json().get("response", "{}"))

    # ── Fallback ──────────────────────────────────────────────────────────────

    def _jaccard_fallback(
        self, chat_id: str, new_memory: ExtractedMemory
    ) -> Optional[dict]:
        """
        Jaccard 字符级扫描，仅在 Graphiti/LLM 服务不可用时触发。
        阈值设偏保守（0.55），避免在降级模式下误判。
        """
        from memory import store
        active = [c for c in store.get_cards_for_chat(chat_id)
                  if c.status == CardStatus.ACTIVE]
        new_chars = set(new_memory.title + new_memory.decision)
        for card in active:
            card_chars = set(card.decision_object + card.decision)
            union = len(new_chars | card_chars)
            score = len(new_chars & card_chars) / union if union else 0.0
            if score >= 0.55:
                logger.info("Conflict jaccard-fallback=%.2f | new='%s' existing='%s'",
                            score, new_memory.title, card.title)
                return {"memory_id": card.memory_id, "title": card.title,
                        "decision": card.decision,
                        "reason": f"jaccard_fallback_{score:.2f}"}
        return None
