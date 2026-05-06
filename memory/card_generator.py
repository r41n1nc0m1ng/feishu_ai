"""
MemoryCard 生成层（对应需求文档 4.6 多粒度记忆生成层）。

流程：EvidenceBlock → LLM 提炼 decision → ConflictDetector 判断 SUPERSEDE → 写入
block_type=noise 的块由 event_segmenter 标注，generate() 直接跳过。
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
from memory.topics import format_for_prompt as _topics_for_prompt
from memory.schemas import (
    CardRelationOp,
    CardStatus,
    EvidenceBlock,
    ExtractedMemory,
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


def _normalize_decision_object(text: str) -> str:
    """归一化 decision_object 显式字符串：把 LLM 偶尔写错的 全角冒号/半角冒号 强制改成 ASCII 连字符。
    这是写入卡片前的硬归一化，防止格式变体造成"假重复"卡片。
    """
    text = (text or "").strip()
    # 替换全角冒号、半角冒号为 ASCII 连字符
    text = text.replace("：", "-").replace(":", "-")
    # 折叠连续的连字符为单个
    text = re.sub(r'-{2,}', '-', text)
    return text


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
你是一个群聊决策记忆提炼助手。根据以下消息片段，提炼记忆卡片。

【块类型】{block_type}

【消息片段】
{messages}

【已有相关记忆（供参考）】
{existing}

{topic_hint}
【decision_object 必须从以下议题列表中选择，原文照抄，不得改写】：
- 格式严格为"大方向-小方向"，使用 ASCII 连字符 `-`（U+002D）
- 严禁使用全角冒号 `：`、半角冒号 `:`、空格或其他字符替代连字符
- 正例：`产品规划-项目范围与MVP边界`
- 反例（禁止）：`产品规划：项目范围与MVP边界`、`产品规划:项目范围与MVP边界`、`产品规划 项目范围与MVP边界`

{topics}

═══════════════════════════════════════
输出规则（按 block_type 选择对应 schema，只返回 JSON）
═══════════════════════════════════════

【若 block_type = decision】（已收口的决策）
{{
  "decision_object": "从议题列表选择最贴近的一项；若分段器已标注议题，优先选用",
  "decision": "一句话决策结论，需包含消息中的关键词。如：使用 Python 而不用 Go",
  "reason": "给出理由；若讨论中未明确说明则填'无'"
}}

【若 block_type = progress】（讨论中，未最终收口）
此块未最终收口，但你必须从中抽取所有可记录的信息，**严禁只输出"未达成结论"这类空泛描述**。
按以下三类抽取（每类没有内容时填空数组）：

1. tentative_consensus（候选共识）：被提出且**无人明确反对**的小结论。
   判定：有人提出X → 无人反对 → 后续话题转向，则X 是候选共识。
2. open_questions（待决议子问题）：被提出但**未给出答案**的子问题。
   判定：以问句提出 / 有分歧未解决 / 明确说"待定"。
3. discussion_scope（讨论涉及的具体对象/维度）：便于下一轮接续。

{{
  "decision_object": "从议题列表选择最贴近的一项",
  "decision": "本轮讨论的核心议题，一句话（如：简历隐私字段的脱敏策略）",
  "tentative_consensus": ["候选共识1（用消息中的具体名词）", "候选共识2"],
  "open_questions": ["待决议问题1", "待决议问题2"],
  "discussion_scope": ["对象1", "对象2"],
  "next_step": "下一轮要解决什么",
  "reason": "本轮未收口的原因"
}}

【progress 强制要求】
- tentative_consensus 数组若为空，先内部检查：真的没有任何小共识吗？即使是"X应该也要做"这种被附议的小点，也算共识。
- open_questions 数组若为空，检查：讨论中真的没有问句或分歧吗？
- 严禁输出"未达成结论""讨论了X但未确定"这类零信息描述。
- 必须用消息中的具体名词（如手机号、邮箱、身份证、照片），不能用"等敏感字段"这种模糊归纳。
"""

_BLOCK_TYPE_TO_MEMORY_TYPE: dict[str, MemoryType] = {
    "decision": MemoryType.DECISION,
    "progress": MemoryType.PROGRESS,
}


class CardGenerator:

    async def generate(self, block: EvidenceBlock, skip_graphiti: bool = False, dry_run: bool = False) -> Optional[
        MemoryCard]:
        """
        从 EvidenceBlock 生成 MemoryCard，写入缓存和 Graphiti。
        skip_graphiti=True 时跳过 Graphiti 写入，只写 SQLite + 内存缓存。
        返回生成的 MemoryCard，NOOP 时返回 None。
        """
        if block.block_type == "noise":
            logger.info("CardGenerator: 跳过 noise 块 | block=%s", block.block_id)
            return None

        messages_text = "\n".join(
            f"{m.sender_name or m.sender_id}  {m.timestamp.strftime('%H:%M')}：{m.text}"
            for m in block.messages
        )
        existing_text = self._format_existing(block.chat_id)
        hints: list[str] = []
        if block.topic:
            hints.append(f"分段器已标注本段议题：{block.topic}（优先选用）")
        if block.one_line_summary:
            hints.append(f"分段器摘要：{block.one_line_summary}")
        topic_hint = "\n".join(hints) + "\n" if hints else ""
        block_type = block.block_type or "decision"
        prompt = _CARD_PROMPT.format(
            block_type=block_type,
            messages=messages_text,
            existing=existing_text,
            topic_hint=topic_hint,
            topics=_topics_for_prompt(),
        )

        raw = await self._call_llm(prompt)
        if not raw:
            return None

        decision_object = _normalize_decision_object(raw.get("decision_object", "")) or "其他-无关消息"
        decision        = raw.get("decision", "").strip()
        if not decision:
            logger.info("CardGenerator: LLM 未返回 decision，跳过 | block=%s", block.block_id)
            return None

        logger.info(
            "CardGenerator result | chat=%s block_id=%s type=%s object=%s",
            block.chat_id, block.block_id, block_type, decision_object,
        )

        is_progress = block_type == "progress"
        memory_type = _BLOCK_TYPE_TO_MEMORY_TYPE.get(block_type, MemoryType.DECISION)

        card = MemoryCard(
            chat_id=block.chat_id,
            decision_object=decision_object,
            decision_object_key=_normalize_decision_key(decision_object),
            decision=decision,
            reason=raw.get("reason", "无"),
            memory_type=memory_type,
            status=CardStatus.ACTIVE,
            source_block_ids=[block.block_id],
            tentative_consensus=raw.get("tentative_consensus", []) if is_progress else [],
            open_questions     =raw.get("open_questions", [])      if is_progress else [],
            discussion_scope   =raw.get("discussion_scope", [])    if is_progress else [],
            next_step          =raw.get("next_step")               if is_progress else None,
        )

        # 注意：冲突检测（SUPERSEDE）已移至 batch_processor 在批处理结束后统一调用
        # detect_and_apply_conflicts()，避免每张卡片都触发 LLM。
        if not dry_run:
            await self._save(card, block, skip_graphiti=skip_graphiti)
        return card

    async def detect_and_apply_conflicts(self, new_cards: list) -> int:
        """批处理结束后批量检测冲突并应用 SUPERSEDE。
        new_cards 按生成顺序传入；每张依次检测，确认冲突则将旧卡置 DEPRECATED 并建立关系。
        返回被废弃的旧卡数量。
        """
        from memory.conflict_detector import ConflictDetector
        cd = ConflictDetector()
        deprecated_count = 0

        for card in new_cards:
            if card.status == CardStatus.DEPRECATED:
                continue   # 已被本批次前面的迭代废弃
            try:
                conflict = await cd.find_conflict(card.chat_id, card)
            except Exception:
                logger.exception("ConflictDetector 异常 | new=%s", card.memory_id[:8])
                continue
            if not conflict:
                continue

            old_id = conflict["memory_id"]
            if old_id == card.memory_id:
                continue
            old = _card_cache.get(old_id) or store.load_memory_card(old_id)
            if not old or old.status == CardStatus.DEPRECATED:
                continue

            await self._apply_supersede_post(card, old, reason=conflict.get("reason", ""))
            deprecated_count += 1

        if deprecated_count:
            logger.info("ConflictDetector batch | new_cards=%d deprecated=%d",
                        len(new_cards), deprecated_count)
        return deprecated_count

    # ── 五分类 operation 框架（取代 detect_and_apply_conflicts，待 conflict_detector 重写后接入）

    async def apply_operations(self, new_cards: list[MemoryCard]) -> list[MemoryCard]:
        """对一批新卡片按 5 分类（add/refine/supersede/progress_complete/progress_refine）应用更新。

        返回经处理后的最终 ACTIVE 卡片列表（add 直接保留；supersede 保留新卡；
        refine/progress_* 返回合并生成的第三张卡）。调用方应用此列表替代原 new_cards
        作为后续 graphiti 写入与 topic 重建的真相源。

        约定：
          - 新卡为 PROGRESS：直接 add，不与旧卡比对
          - 新卡为 DECISION：以同 chat + 同 decision_object 为候选，按 created_at 倒序逐张分类，
            首个非 add 命中即应用并停止
        """
        final: list[MemoryCard] = []
        for new_card in new_cards:
            if new_card.status == CardStatus.DEPRECATED:
                continue   # 已被本批次前面迭代废弃

            if new_card.memory_type == MemoryType.PROGRESS:
                final.append(new_card)
                continue

            candidates = self._candidates_for(new_card)
            if not candidates:
                final.append(new_card)
                continue

            op, source = await self._classify_against(new_card, candidates)
            if op == CardRelationOp.ADD or source is None:
                final.append(new_card)
                continue

            if op == CardRelationOp.SUPERSEDE:
                await self._apply_supersede_post(new_card, source, reason="apply_operations:supersede")
                final.append(new_card)
                continue

            # refine / progress_complete / progress_refine 走合并路径
            merged = await self._apply_merge(new_card, source, op)
            if merged:
                final.append(merged)
            else:
                # 合并失败（拿不到原始消息块等）→ 退化为 add，避免丢卡
                logger.warning("apply_merge failed, falling back to add | new=%s old=%s op=%s",
                               new_card.memory_id[:8], source.memory_id[:8], op.value)
                final.append(new_card)

        return final

    def _candidates_for(self, new_card: MemoryCard) -> list[MemoryCard]:
        """返回 chat 内同 decision_object 的活跃旧卡，按 created_at 倒序（最近优先）。"""
        cands = [
            c for c in _card_cache.values()
            if c.chat_id == new_card.chat_id
               and c.status == CardStatus.ACTIVE
               and c.memory_id != new_card.memory_id
               and c.decision_object == new_card.decision_object
        ]
        cands.sort(key=lambda c: c.created_at, reverse=True)
        return cands

    async def _classify_against(
        self, new_card: MemoryCard, candidates: list[MemoryCard]
    ) -> tuple[CardRelationOp, Optional[MemoryCard]]:
        """逐张候选调用 ConflictDetector.classify_pair，首个非 ADD 命中即返回。

        candidates 已按 created_at 倒序，最近的卡先比对。
        """
        from memory.conflict_detector import ConflictDetector
        detector = ConflictDetector()
        for cand in candidates:
            try:
                op, reason = await detector.classify_pair(new_card, cand)
            except Exception:
                logger.exception("classify_pair 抛异常 | new=%s old=%s",
                                 new_card.memory_id[:8], cand.memory_id[:8])
                continue
            if op != CardRelationOp.ADD:
                logger.info(
                    "classify hit | op=%s new=%s old=%s reason=%s",
                    op.value, new_card.memory_id[:8], cand.memory_id[:8], reason,
                )
                return op, cand
        return CardRelationOp.ADD, None

    async def _apply_merge(
        self, new_card: MemoryCard, old_card: MemoryCard, op: CardRelationOp
    ) -> Optional[MemoryCard]:
        """REFINE / PROGRESS_COMPLETE / PROGRESS_REFINE 共用合并路径。

        构造合成 EvidenceBlock（消息合并 + block_type 由 op 决定），复用 generate()
        从 LLM 重新提炼一张新卡。完成后：新旧两张源卡均置 DEPRECATED，合并卡
        supersedes_memory_ids = [old, new]，写入两条 SUPERSEDES 关系。
        """
        from memory.evidence_store import EvidenceStore
        es = EvidenceStore()

        block_ids = list(dict.fromkeys(old_card.source_block_ids + new_card.source_block_ids))
        blocks: list[EvidenceBlock] = []
        for bid in block_ids:
            b = await es.get(bid)
            if b:
                blocks.append(b)
        if not blocks:
            logger.warning("apply_merge: 无法加载源 block | old=%s new=%s",
                           old_card.memory_id[:8], new_card.memory_id[:8])
            return None

        all_msgs = sorted(
            [m for b in blocks for m in b.messages],
            key=lambda m: m.timestamp,
        )
        synthetic_type = "progress" if op == CardRelationOp.PROGRESS_REFINE else "decision"
        summaries = [b.one_line_summary for b in blocks if b.one_line_summary]
        synthetic_summary = "；".join(summaries) if summaries else None

        synthetic = EvidenceBlock(
            chat_id=new_card.chat_id,
            start_time=min(b.start_time for b in blocks),
            end_time=max(b.end_time for b in blocks),
            messages=all_msgs,
            topic=old_card.decision_object,
            block_type=synthetic_type,
            boundary_signal=None,
            one_line_summary=synthetic_summary,
        )

        # dry_run=True：只走 LLM 抽取，跳过 _save，待我们手工修正字段后再保存
        merged = await self.generate(synthetic, skip_graphiti=True, dry_run=True)
        if not merged:
            logger.warning("apply_merge: 合成块 LLM 抽取失败 | op=%s", op.value)
            return None

        # 覆盖：source_block_ids 指向原始 block（不是合成块），supersedes 记录两张源卡
        merged.source_block_ids = block_ids
        merged.supersedes_memory_ids = [old_card.memory_id, new_card.memory_id]

        # 1) 持久化合并卡（_save 会写 SQLite + embedding，跳过 graphiti）
        await self._save(merged, synthetic, skip_graphiti=True)

        # 2) 两张源卡置 DEPRECATED
        for src in (old_card, new_card):
            src.status = CardStatus.DEPRECATED
            _card_cache[src.memory_id] = src
            try:
                store.save_memory_card(src)
            except Exception:
                logger.exception("源卡 DEPRECATED 写入失败 | memory_id=%s", src.memory_id)

        # 3) _cards_by_object 索引指向合并卡
        merged_key = merged.decision_object_key or _normalize_decision_key(merged.decision_object)
        _cards_by_object[merged_key] = merged

        # 4) 写两条 SUPERSEDES 关系
        for src in (old_card, new_card):
            try:
                store.save_relation(MemoryRelation(
                    chat_id=merged.chat_id,
                    source_id=merged.memory_id,
                    target_id=src.memory_id,
                    relation_type=MemoryRelationType.SUPERSEDES,
                ))
            except Exception:
                logger.exception("MemoryRelation 写入失败 | source=%s target=%s",
                                 merged.memory_id, src.memory_id)

        logger.info(
            "MERGE applied | op=%s merged=%s ← old=%s + new=%s",
            op.value, merged.memory_id[:8], old_card.memory_id[:8], new_card.memory_id[:8],
        )
        return merged

    async def _apply_supersede_post(
        self, new_card: MemoryCard, old_card: MemoryCard, reason: str = ""
    ) -> None:
        """对已写入的两张卡片应用 SUPERSEDE 关系（后处理路径，与 _handle_supersede 区分）。
        - 旧卡：status → DEPRECATED，更新缓存与 SQLite
        - 新卡：写入 supersedes_memory_id，更新缓存与 SQLite，重建 _cards_by_object 索引
        - 写入 MemoryRelation
        """
        old_card.status = CardStatus.DEPRECATED
        _card_cache[old_card.memory_id] = old_card
        try:
            store.save_memory_card(old_card)
        except Exception:
            logger.exception("旧卡片 DEPRECATED 写入失败 | memory_id=%s", old_card.memory_id)

        if old_card.memory_id not in new_card.supersedes_memory_ids:
            new_card.supersedes_memory_ids.append(old_card.memory_id)
        _card_cache[new_card.memory_id] = new_card
        # _cards_by_object 索引指向新卡（如果两张卡 key 一致）
        new_key = new_card.decision_object_key or _normalize_decision_key(new_card.decision_object)
        _cards_by_object[new_key] = new_card
        try:
            store.save_memory_card(new_card)
        except Exception:
            logger.exception("新卡片 supersedes_memory_ids 写入失败 | memory_id=%s", new_card.memory_id)

        relation = MemoryRelation(
            chat_id=new_card.chat_id,
            source_id=new_card.memory_id,
            target_id=old_card.memory_id,
            relation_type=MemoryRelationType.SUPERSEDES,
        )
        try:
            store.save_relation(relation)
        except Exception:
            logger.exception("MemoryRelation 写入失败 | source=%s target=%s",
                             new_card.memory_id, old_card.memory_id)
        logger.info(
            "SUPERSEDE applied (post-batch) | new=%s deprecated=%s reason=%s",
            new_card.memory_id[:8], old_card.memory_id[:8], reason,
        )

    async def _handle_supersede(
            self, new_card: MemoryCard, old_memory_id: Optional[str] = None
    ) -> MemoryCard:
        """将旧卡片标记为 Deprecated，并建立 supersedes 关系。
        old_memory_id 由 ConflictDetector 语义检测时提供；为 None 时按 key 查找。
        """
        if old_memory_id:
            old = _card_cache.get(old_memory_id) or store.load_memory_card(old_memory_id)
        else:
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

        if old.memory_id not in new_card.supersedes_memory_ids:
            new_card.supersedes_memory_ids.append(old.memory_id)

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
        lines: list[str] = []
        for c in cards[-5:]:
            if c.memory_type == MemoryType.PROGRESS:
                lines.append(f"- [{c.decision_object}] (PROGRESS) {c.decision}")
                if c.tentative_consensus:
                    lines.append(f"    候选共识: {'; '.join(c.tentative_consensus[:3])}")
                if c.open_questions:
                    lines.append(f"    待决议: {'; '.join(c.open_questions[:3])}")
            else:
                lines.append(f"- [{c.decision_object}] {c.decision[:60]}")
        return "\n".join(lines)

    async def _call_llm(self, prompt: str) -> Optional[dict]:
        """优先级：DeepSeek → OpenAI → Ollama。"""
        if os.getenv("DEEPSEEK_API_KEY"):
            return await self._call_deepseek(prompt)
        provider = os.getenv("MODEL_PROVIDER", "ollama").strip().lower()
        if provider == "openai" or os.getenv("OPENAI_API_KEY"):
            return await self._call_openai_compatible(prompt)
        return await self._call_ollama(prompt)

    async def _call_deepseek(self, prompt: str) -> Optional[dict]:
        api_key  = os.getenv("DEEPSEEK_API_KEY", "")
        base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
        model    = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
        seed     = int(os.getenv("LLM_SEED", "42"))
        try:
            async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
                resp = await client.post(
                    f"{base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": prompt}],
                        "response_format": {"type": "json_object"},
                        "temperature": 0,
                        "top_p": 1,
                        "seed": seed,
                    },
                )
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]
                return json.loads(content)
        except Exception as e:
            logger.error("CardGenerator DeepSeek 调用失败: %s", e)
            return None

    async def _call_openai_compatible(self, prompt: str) -> Optional[dict]:
        api_key  = os.getenv("OPENAI_API_KEY", "")
        base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        model    = os.getenv("OPENAI_MODEL", CARD_MODEL)
        seed     = int(os.getenv("LLM_SEED", "42"))
        if not api_key:
            logger.error("CardGenerator OpenAI 调用失败: OPENAI_API_KEY 未配置")
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
                        "temperature": 0,
                        "top_p": 1,
                        "seed": seed,
                    },
                )
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]
                return json.loads(content)
        except Exception as e:
            logger.error("CardGenerator OpenAI 调用失败: %s", e)
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
            logger.error("CardGenerator Ollama 调用失败: %s", e)
            return None


async def _get_embedding(text: str) -> Optional[np.ndarray]:
    """调用 embedding 接口，返回归一化向量。失败返回 None。"""
    try:
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            provider = os.getenv("MODEL_PROVIDER", "ollama").strip().lower()
            if provider == "openai" or os.getenv("OPENAI_API_KEY"):
                api_key = os.getenv("OPENAI_API_KEY", "")
                base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
                model = os.getenv("OPENAI_EMBED_MODEL", os.getenv("EMBED_MODEL", "text-embedding-3-small"))
                resp = await client.post(
                    f"{base_url}/embeddings",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={"model": model, "input": text},
                )
                resp.raise_for_status()
                vec = (resp.json().get("data") or [{}])[0].get("embedding") or []
            else:
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
    """为卡片计算 embedding，写入内存缓存并持久化到 SQLite。
    embedding 文本含 decision_object + decision + reason，PROGRESS 卡还含 tentative_consensus
    和 open_questions——这些字段才是 PROGRESS 卡的真正内容。
    """
    parts: list[str] = [card.decision_object, card.decision]
    if card.reason and card.reason != "无":
        parts.append(card.reason)
    if card.memory_type == MemoryType.PROGRESS:
        if card.tentative_consensus:
            parts.append(" ".join(card.tentative_consensus))
        if card.open_questions:
            parts.append(" ".join(card.open_questions))
    text = " ".join(parts)
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
            name=f"card::{card.memory_id}::{card.decision_object}",
            episode_body=episode_body,
            source=EpisodeType.text,
            source_description=f"MemoryCard | 群聊 {card.chat_id}",
            reference_time=ref_time,
            group_id=card.chat_id,
        )
        logger.info("MemoryCard 已写入 Graphiti | memory_id=%s object=%s",
                    card.memory_id, card.decision_object)
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
