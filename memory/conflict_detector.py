"""
冲突检测层。

提供两套接口（共存）：
  1) classify_pair(new, old) → CardRelationOp
       新接口：apply_operations 使用，按 6 分类返回操作类型
       - 旧卡为 DECISION：3 分类（same_direction_same_opinion / same_direction_opposite_opinion / different_direction）
       - 旧卡为 PROGRESS：3 分类（fully_answered / partially_answered / not_answered）
       LLM 不可用时一律返回 ADD（保守降级）。
  2) find_conflict(chat_id, new) → dict | None
       旧接口：detect_and_apply_conflicts / batch_processor 使用，仅判定 SUPERSEDE。
       三段式（key match → semantic+LLM → Jaccard fallback）。
"""
import json
import logging
import os
import re
from typing import Optional

import httpx

from memory.llm_runtime import apply_thinking_payload
from memory.retriever import MemoryRetriever
from memory.schemas import CardRelationOp, CardStatus, MemoryCard, MemoryType

logger = logging.getLogger(__name__)

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
CONFLICT_MODEL = os.getenv("LOCAL_MODEL", "qwen2.5:7b")

# ── 6 分类 prompts（apply_operations 走 classify_pair 调用）────────────────────

_DECISION_PAIR_PROMPT = """\
你是记忆卡片关系判定助手。比较两张同议题的决策卡片，给出关系。

议题：{decision_object}

新卡片：
  决策：{new_decision}
  理由：{new_reason}

已有卡片：
  决策：{old_decision}
  理由：{old_reason}

【判定步骤】
1. 这两张卡片讨论的是同一个**子方向**吗？同议题下可能有多个子方向。
   例如议题 "MVP-范围" 下可有"是否做账号体系""是否做简历解析""哪些字段脱敏"等独立子方向。
   只有当 decision 中描述的具体对象/动作/维度对齐时，才视为同子方向。
2. 若是同子方向，新旧观点是相同（互为补充/重复/同方向加强）还是相反（结论冲突）？

【关系类型】
- same_direction_same_opinion：同子方向 + 观点一致（重复或补充）
- same_direction_opposite_opinion：同子方向 + 结论冲突（互为否定）
- different_direction：同议题但子方向不同（如一个谈账号，一个谈解析）

【输出 JSON】只返回 JSON，不要其他内容：
{{"relation": "same_direction_same_opinion | same_direction_opposite_opinion | different_direction", "reason": "一句话依据"}}
"""

_PROGRESS_PAIR_PROMPT = """\
你是记忆卡片关系判定助手。判断一张新决策卡片对一张未收口的 progress 卡片的解答程度。

议题：{decision_object}

旧 progress 卡片：
  本轮议题：{old_decision}
  待决议子问题（open_questions）：{old_open_questions}
  讨论涉及对象（discussion_scope）：{old_discussion_scope}

新决策卡片：
  决策：{new_decision}
  理由：{new_reason}

【判定步骤】
1. 新决策与旧 progress 卡是否指向同一子方向？看 new.decision 的具体对象是否落在 old.open_questions / discussion_scope 内。
2. 若同子方向，新决策是否覆盖 open_questions 中**所有**子问题？

【关系类型】
- fully_answered：同子方向 + 完整解答 open_questions 中的全部子问题
- partially_answered：同子方向 + 解答了部分（>=1 项）但非全部 open_questions
- not_answered：不同子方向，或 open_questions 中没有任何一项被新决策回答

【输出 JSON】只返回 JSON，不要其他内容：
{{"relation": "fully_answered | partially_answered | not_answered", "reason": "一句话依据"}}
"""

_DECISION_RELATION_TO_OP = {
    "same_direction_same_opinion":     CardRelationOp.REFINE,
    "same_direction_opposite_opinion": CardRelationOp.SUPERSEDE,
    "different_direction":             CardRelationOp.ADD,
}
_PROGRESS_RELATION_TO_OP = {
    "fully_answered":     CardRelationOp.PROGRESS_COMPLETE,
    "partially_answered": CardRelationOp.PROGRESS_REFINE,
    "not_answered":       CardRelationOp.ADD,
}

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

    # ── 新接口：6 分类 classify_pair ──────────────────────────────────────────

    async def classify_pair(
        self, new_card: MemoryCard, old_card: MemoryCard
    ) -> tuple[CardRelationOp, str]:
        """对 (new, old) 卡片对做关系判定，返回 (operation, reason)。

        约定：
          - new_card 的 memory_type 由调用方保证为 DECISION（PROGRESS 新卡走 add 短路）
          - 旧卡为 PROGRESS → 走 progress prompt（fully/partially/not_answered）
          - 旧卡为非 PROGRESS → 走 decision prompt（same+same / same+oppos / different）
          - LLM 不可用或 relation 字段缺失 → 返回 (ADD, 'llm_unavailable') 保守降级
        """
        if old_card.memory_type == MemoryType.PROGRESS:
            prompt = _PROGRESS_PAIR_PROMPT.format(
                decision_object       = old_card.decision_object,
                old_decision          = old_card.decision,
                old_open_questions    = old_card.open_questions or [],
                old_discussion_scope  = old_card.discussion_scope or [],
                new_decision          = new_card.decision,
                new_reason            = new_card.reason or "无",
            )
            mapping = _PROGRESS_RELATION_TO_OP
            kind = "progress"
        else:
            prompt = _DECISION_PAIR_PROMPT.format(
                decision_object = old_card.decision_object,
                new_decision    = new_card.decision,
                new_reason      = new_card.reason or "无",
                old_decision    = old_card.decision,
                old_reason      = old_card.reason or "无",
            )
            mapping = _DECISION_RELATION_TO_OP
            kind = "decision"

        try:
            raw = await self._call_llm(prompt)
        except Exception as e:
            logger.warning("classify_pair LLM 调用异常 | err=%s", e)
            return CardRelationOp.ADD, "llm_unavailable"

        if not raw:
            return CardRelationOp.ADD, "llm_no_response"

        relation = (raw.get("relation") or "").strip()
        op = mapping.get(relation)
        reason = (raw.get("reason") or "").strip()
        if op is None:
            logger.warning("classify_pair 未识别 relation=%s kind=%s", relation, kind)
            return CardRelationOp.ADD, f"unknown_relation:{relation}"

        logger.info(
            "classify_pair | kind=%s relation=%s op=%s new='%s' old='%s' reason=%s",
            kind, relation, op.value,
            new_card.decision[:30], old_card.decision[:30], reason,
        )
        return op, reason

    async def _call_llm(self, prompt: str) -> Optional[dict]:
        """统一 LLM 入口，优先级：DeepSeek → OpenAI → Ollama。"""
        if os.getenv("DEEPSEEK_API_KEY"):
            return await self._call_deepseek(prompt)
        provider = os.getenv("MODEL_PROVIDER", "ollama").strip().lower()
        if provider == "openai" or os.getenv("OPENAI_API_KEY"):
            return await self._call_openai(prompt)
        return await self._call_ollama(prompt)

    # ── 旧接口（batch_processor / detect_and_apply_conflicts 仍在使用）─────────

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
                json=apply_thinking_payload({
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "response_format": {"type": "json_object"},
                    "temperature": 0,
                    "top_p": 1,
                    "seed": seed,
                }),
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
                json=apply_thinking_payload({
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "response_format": {"type": "json_object"},
                    "temperature": 0,
                    "top_p": 1,
                    "seed": seed,
                }),
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
