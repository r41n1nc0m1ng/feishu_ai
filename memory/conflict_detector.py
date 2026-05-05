"""
冲突检测层（Option B 架构）。

三段式，触发条件互斥：
  Stage 1 — 规则：decision_object_key 精确匹配（无外部依赖，始终可用）
  Stage 2 — 语义：embedding 召回候选 → LLM 判断是否同议题
  Fallback — 降级：Jaccard 直扫 SQLite（仅当 Stage 2 服务不可用时触发）

Jaccard 不在 LLM 判断"否"之后运行，只在 embedding/LLM 服务不可用时接管。
接收 MemoryCard（由 card_generator 在保存前传入）。
"""
import json
import logging
import os
import re
from typing import Optional

import httpx

from memory.retriever import MemoryRetriever
from memory.schemas import CardStatus, MemoryCard

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
    接收尚未写入缓存的新 MemoryCard。
    """

    async def find_conflict(
        self, chat_id: str, new_card: MemoryCard
    ) -> Optional[dict]:
        """
        返回冲突的已有记忆 dict（含 memory_id / decision_object / decision / reason），
        未发现冲突返回 None。

        关键设计：同 decision_object_key 不等于同决策——可能是同议题下的不同子决策
        （如 MVP 范围 vs MVP 输出形式）。Stage 1 仅作为候选，必须经 Stage 2 LLM
        判断"是否实质相同且冲突"才能确认 SUPERSEDE。
        """
        # ── Stage 1：规则 — decision_object_key 命中 → 拿到候选 ──────────────
        candidate = self._key_match(chat_id, new_card)

        # ── Stage 2：LLM 判断 ────────────────────────────────────────────────
        try:
            if candidate:
                # 同 key 的候选交给 LLM 复核：是不是同决策且冲突？
                from memory.card_generator import _card_cache
                existing = _card_cache.get(candidate["memory_id"])
                if existing and await self._llm_judge(new_card, existing):
                    candidate["reason"] = f"{candidate['reason']}+llm_confirmed"
                    logger.info("Conflict confirmed by LLM | new='%s' existing='%s'",
                                new_card.decision_object, existing.decision_object)
                    return candidate
                logger.info("Same key but LLM says different/no_conflict, keeping both | object=%s",
                            new_card.decision_object)
                return None  # 同 key 但不是同决策（如同议题下的不同子决策）

            # 无 key 候选 → 走语义召回 + LLM 判断
            return await self._semantic_llm_check(chat_id, new_card)
        except Exception as e:
            logger.warning("LLM 不可用，降级到 Jaccard | reason=%s", e)

        # ── Fallback：Jaccard — 仅在 LLM 服务不可用时触发 ─────────────────────
        return self._jaccard_fallback(chat_id, new_card)

    # ── Stage 1 ───────────────────────────────────────────────────────────────

    def _key_match(self, chat_id: str, new_card: MemoryCard) -> Optional[dict]:
        from memory.card_generator import _cards_by_object
        new_key = new_card.decision_object_key or _simple_key(new_card.decision_object)
        if not new_key:
            return None
        existing = _cards_by_object.get(new_key)
        if existing and existing.chat_id == chat_id and existing.status == CardStatus.ACTIVE:
            logger.info("Conflict key-match | new='%s' existing='%s'",
                        new_card.decision_object, existing.decision_object)
            return {"memory_id": existing.memory_id, "decision_object": existing.decision_object,
                    "decision": existing.decision, "reason": "key_match"}
        return None

    # ── Stage 2 ───────────────────────────────────────────────────────────────

    async def _semantic_llm_check(
        self, chat_id: str, new_card: MemoryCard
    ) -> Optional[dict]:
        """
        embedding 召回候选，取 top-1 让 LLM 判断是否同议题。
        embedding 缓存为空或检索失败时抛出异常，由调用方触发 Fallback。
        """
        candidates = await MemoryRetriever().retrieve(chat_id, new_card.decision_object, limit=3)
        # 排除新卡片本身（理论上不会出现，但防御性检查）
        candidates = [c for c in candidates if c.memory_id != new_card.memory_id]
        if not candidates:
            return None  # 语义上无相关候选，判定无冲突

        top = candidates[0]
        is_conflict = await self._llm_judge(new_card, top)
        if is_conflict:
            logger.info("Conflict semantic+LLM | new='%s' existing='%s'",
                        new_card.decision_object, top.decision_object)
            return {"memory_id": top.memory_id, "decision_object": top.decision_object,
                    "decision": top.decision, "reason": "semantic_llm"}
        return None

    async def _llm_judge(self, new_card: MemoryCard, existing: MemoryCard) -> bool:
        prompt = _CONFLICT_PROMPT.format(
            new_object=new_card.decision_object,
            new_decision=new_card.decision,
            existing_object=existing.decision_object,
            existing_decision=existing.decision,
        )
        try:
            # 优先级：DeepSeek → OpenAI → Ollama（与 card_generator 一致）
            if os.getenv("DEEPSEEK_API_KEY"):
                raw = await self._call_deepseek(prompt)
            else:
                provider = os.getenv("MODEL_PROVIDER", "ollama").strip().lower()
                if provider == "openai" or os.getenv("OPENAI_API_KEY"):
                    raw = await self._call_openai(prompt)
                else:
                    raw = await self._call_ollama(prompt)
            return bool(raw and raw.get("relation") == "same_and_conflict")
        except Exception as e:
            logger.error("ConflictDetector LLM call failed: %s", e)
            raise  # 让上层触发 Fallback

    async def _call_deepseek(self, prompt: str) -> Optional[dict]:
        api_key  = os.getenv("DEEPSEEK_API_KEY", "")
        base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
        model    = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
        seed     = int(os.getenv("LLM_SEED", "42"))
        async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": model,
                      "messages": [{"role": "user", "content": prompt}],
                      "response_format": {"type": "json_object"},
                      "temperature": 0,
                      "top_p": 1,
                      "seed": seed},
            )
            resp.raise_for_status()
            return json.loads(resp.json()["choices"][0]["message"]["content"])

    async def _call_openai(self, prompt: str) -> Optional[dict]:
        api_key  = os.getenv("OPENAI_API_KEY", "")
        base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        model    = os.getenv("OPENAI_MODEL", CONFLICT_MODEL)
        seed     = int(os.getenv("LLM_SEED", "42"))
        async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": model,
                      "messages": [{"role": "user", "content": prompt}],
                      "response_format": {"type": "json_object"},
                      "temperature": 0,
                      "top_p": 1,
                      "seed": seed},
            )
            resp.raise_for_status()
            return json.loads(resp.json()["choices"][0]["message"]["content"])

    async def _call_ollama(self, prompt: str) -> Optional[dict]:
        seed = int(os.getenv("LLM_SEED", "42"))
        async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
            resp = await client.post(
                f"{OLLAMA_URL}/api/generate",
                json={"model": CONFLICT_MODEL, "prompt": prompt,
                      "stream": False, "format": "json",
                      "options": {"temperature": 0, "seed": seed}},
            )
            resp.raise_for_status()
            return json.loads(resp.json().get("response", "{}"))

    # ── Fallback ──────────────────────────────────────────────────────────────

    def _jaccard_fallback(
        self, chat_id: str, new_card: MemoryCard
    ) -> Optional[dict]:
        """
        Jaccard 字符级扫描，仅在 embedding/LLM 服务不可用时触发。
        阈值设偏保守（0.55），避免在降级模式下误判。
        """
        from memory import store
        active = [c for c in store.get_cards_for_chat(chat_id)
                  if c.status == CardStatus.ACTIVE and c.memory_id != new_card.memory_id]
        new_chars = set(new_card.decision_object + new_card.decision)
        for card in active:
            card_chars = set(card.decision_object + card.decision)
            union = len(new_chars | card_chars)
            score = len(new_chars & card_chars) / union if union else 0.0
            if score >= 0.55:
                logger.info("Conflict jaccard-fallback=%.2f | new='%s' existing='%s'",
                            score, new_card.decision_object, card.decision_object)
                return {"memory_id": card.memory_id, "decision_object": card.decision_object,
                        "decision": card.decision,
                        "reason": f"jaccard_fallback_{score:.2f}"}
        return None
