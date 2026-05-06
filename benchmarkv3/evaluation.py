"""
benchmark v3 评估器：所有断言/匹配函数的纯逻辑实现。

不依赖 retriever / store 等运行时模块，只接收已经检索/生成好的对象（card / block / topic）
做关键词与结构断言，方便单测和 reuse。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from memory.schemas import CardStatus, EvidenceBlock, MemoryCard, MemoryType, TopicSummary


def _norm(text: str) -> str:
    return (text or "").strip().lower()


def _card_text(card: MemoryCard) -> str:
    parts = [
        card.decision_object or "",
        card.decision or "",
        card.reason or "",
        " ".join(card.tentative_consensus or []),
        " ".join(card.open_questions or []),
        " ".join(card.discussion_scope or []),
        card.next_step or "",
    ]
    return " ".join(p for p in parts if p).lower()


def _topic_text(topic: TopicSummary) -> str:
    return f"{topic.topic} {topic.summary}".lower()


def _block_text(block: EvidenceBlock) -> str:
    return " ".join(getattr(m, "text", "") or "" for m in block.messages).lower()


def keywords_all(text: str, kws: list[str] | None) -> bool:
    """全部关键词都必须出现。空列表视为不约束。"""
    if not kws:
        return True
    n = _norm(text)
    return all(_norm(k) in n for k in kws)


def keywords_any(text: str, kws: list[str] | None) -> bool:
    """至少出现一个关键词。空列表视为不约束。"""
    if not kws:
        return True
    n = _norm(text)
    return any(_norm(k) in n for k in kws)


def keywords_absent(text: str, kws: list[str] | None) -> bool:
    if not kws:
        return True
    n = _norm(text)
    return all(_norm(k) not in n for k in kws)


# ── 数据模型 ──────────────────────────────────────────────────────────────────


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


# ── 1) 查询样例评估 ──────────────────────────────────────────────────────────


def evaluate_per_batch_query(
    *,
    name: str,
    query: str,
    expected_granularity: str,
    actual_granularity: Optional[str],
    candidates: list,
) -> CheckResult:
    """每个 batch 末尾的"松"查询：只要 actual_granularity == expected 且至少召回 1 条，就算通过。"""
    if not candidates:
        return CheckResult(name=name, passed=False, detail=f"q={query[:30]} 0 candidates")
    granularity_ok = (actual_granularity or "memory_card") == expected_granularity
    return CheckResult(
        name=name, passed=granularity_ok,
        detail=f"q={query[:30]} hits={len(candidates)} granularity={actual_granularity}/{expected_granularity}",
    )


def evaluate_final_decision_query(
    *,
    name: str,
    spec: dict[str, Any],
    actual_granularity: Optional[str],
    cards: list[MemoryCard],
) -> CheckResult:
    """final decision query：要求 granularity 正确 + 至少有一张卡满足 keywords。"""
    expected_granularity = spec.get("expected_granularity", "memory_card")
    expected_kws = spec.get("expected_keywords") or []
    expected_kws_any = spec.get("expected_keywords_any") or []
    forbidden_kws = spec.get("forbidden_keywords") or []

    granularity_ok = (actual_granularity or "memory_card") == expected_granularity
    keyword_ok = any(
        keywords_all(_card_text(c), expected_kws)
        and keywords_any(_card_text(c), expected_kws_any)
        and keywords_absent(_card_text(c), forbidden_kws)
        for c in cards
    )
    passed = granularity_ok and keyword_ok
    detail = (
        f"q={spec.get('query', '')[:40]} "
        f"granularity={'OK' if granularity_ok else f'{actual_granularity}!={expected_granularity}'} "
        f"keyword={'OK' if keyword_ok else 'MISS'} "
        f"hits={len(cards)}"
    )
    return CheckResult(name=name, passed=passed, detail=detail)


def evaluate_final_evidence_query(
    *,
    name: str,
    spec: dict[str, Any],
    actual_granularity: Optional[str],
    block: Optional[EvidenceBlock],
) -> CheckResult:
    """final evidence query：要求 granularity 正确 + block 文本含期望关键词之一。"""
    expected_granularity = spec.get("expected_granularity", "evidence_block")
    expected_kws_any = spec.get("expected_keywords_any") or []

    granularity_ok = (actual_granularity or "memory_card") == expected_granularity
    if block is None:
        return CheckResult(
            name=name, passed=False,
            detail=f"q={spec.get('query', '')[:40]} no block returned",
        )
    keyword_ok = keywords_any(_block_text(block), expected_kws_any)
    passed = granularity_ok and keyword_ok
    detail = (
        f"q={spec.get('query', '')[:40]} "
        f"granularity={'OK' if granularity_ok else f'{actual_granularity}!={expected_granularity}'} "
        f"keyword={'OK' if keyword_ok else 'MISS'} "
        f"block_msgs={len(block.messages)}"
    )
    return CheckResult(name=name, passed=passed, detail=detail)


def evaluate_final_topic_query(
    *,
    name: str,
    spec: dict[str, Any],
    actual_granularity: Optional[str],
    topics: list[TopicSummary],
) -> CheckResult:
    """final topic_summary query：granularity 正确 + 至少一个 topic 含期望关键词之一。"""
    expected_granularity = spec.get("expected_granularity", "topic_summary")
    expected_kws_any = spec.get("expected_keywords_any") or []

    granularity_ok = (actual_granularity or "memory_card") == expected_granularity
    keyword_ok = any(keywords_any(_topic_text(t), expected_kws_any) for t in topics)
    passed = granularity_ok and keyword_ok
    detail = (
        f"q={spec.get('query', '')[:40]} "
        f"granularity={'OK' if granularity_ok else f'{actual_granularity}!={expected_granularity}'} "
        f"keyword={'OK' if keyword_ok else 'MISS'} "
        f"topics={len(topics)}"
    )
    return CheckResult(name=name, passed=passed, detail=detail)


# ── 2) 矛盾更新检测 ──────────────────────────────────────────────────────────

# 与 CardRelationOp 对齐：head 卡的 supersedes_memory_ids 长度 + memory_type 决定 op
_OP_TO_SOURCE_COUNT = {
    "supersede": 1,
    "refine": 2,
    "progress_complete": 2,
    "progress_refine": 2,
}


def evaluate_conflict_from_chat(
    *,
    name: str,
    spec: dict[str, Any],
    cards_in_chat: list[MemoryCard],
) -> CheckResult:
    """对实跑产出的 SQLite 卡片做匹配：找一张 ACTIVE 卡，其 supersedes_memory_ids 长度匹配 op，
    且 head 文本满足 head_keywords / head_decision_object_keywords_any，
    其 source 卡（按 memory_id 反查）能与 source_keywords_groups 一一匹配。
    """
    op = str(spec.get("operation", "")).strip().lower()
    head_kws = spec.get("head_keywords") or []
    head_kws_any = spec.get("head_keywords_any") or []
    head_obj_any = spec.get("head_decision_object_keywords_any") or []
    source_groups: list[list[str]] = spec.get("source_keywords_groups") or []
    expected_count = _OP_TO_SOURCE_COUNT.get(op, len(source_groups))

    by_id = {c.memory_id: c for c in cards_in_chat}

    for c in cards_in_chat:
        if c.status != CardStatus.ACTIVE:
            continue
        if not c.supersedes_memory_ids:
            continue
        if expected_count is not None and len(c.supersedes_memory_ids) != expected_count:
            continue
        if not keywords_all(_card_text(c), head_kws):
            continue
        if head_kws_any and not keywords_any(_card_text(c), head_kws_any):
            continue
        if head_obj_any and not keywords_any(_norm(c.decision_object or ""), head_obj_any):
            continue

        # progress_refine 的 head 必须是 progress；supersede / refine / progress_complete 的 head 应为 decision
        head_should_be_progress = op == "progress_refine"
        if head_should_be_progress and c.memory_type != MemoryType.PROGRESS:
            continue
        if (not head_should_be_progress) and c.memory_type != MemoryType.DECISION:
            continue

        # 解析 source 卡，做无序唯一匹配
        sources = [by_id.get(sid) for sid in c.supersedes_memory_ids]
        sources = [s for s in sources if s is not None]
        if len(sources) != len(c.supersedes_memory_ids):
            continue

        if source_groups:
            used: set[int] = set()
            ok = True
            for grp in source_groups:
                hit = False
                for j, s in enumerate(sources):
                    if j in used:
                        continue
                    if keywords_any(_card_text(s), grp):
                        used.add(j)
                        hit = True
                        break
                if not hit:
                    ok = False
                    break
            if not ok:
                continue

        return CheckResult(
            name=name, passed=True,
            detail=f"op={op} head={c.memory_id[:8]} sources={[s[:8] for s in c.supersedes_memory_ids]}",
        )

    return CheckResult(
        name=name, passed=False,
        detail=f"op={op} head_keywords={head_kws}",
    )


def evaluate_simulated_conflict(
    *,
    name: str,
    spec: dict[str, Any],
    final_cards: list[MemoryCard],
    old_card_ids: list[str],
    new_card_id: str,
) -> CheckResult:
    """模拟样例：注入 old_cards + new_card 到缓存后跑 apply_operations，
    校验 final 列表里出现的 head 是否匹配预期 op。
    final_cards 是 apply_operations 返回的列表；old/new 在调用前已写入。
    """
    op_expected = str(spec.get("operation", "")).strip().lower()

    # 找 final 中和原 new/old 都不同的"合并卡"（refine/progress_*）
    # 或新卡保留但 supersedes_memory_ids 含 old_id（supersede）
    if op_expected == "supersede":
        new_card = next((c for c in final_cards if c.memory_id == new_card_id), None)
        if not new_card:
            return CheckResult(name=name, passed=False, detail="supersede: new 卡未在 final 列表")
        if not any(oid in new_card.supersedes_memory_ids for oid in old_card_ids):
            return CheckResult(name=name, passed=False,
                               detail=f"supersede: new.supersedes={new_card.supersedes_memory_ids} "
                                      f"未包含任何 old_id={old_card_ids}")
        return CheckResult(name=name, passed=True,
                           detail=f"supersede ok: new={new_card.memory_id[:8]} "
                                  f"supersedes={[s[:8] for s in new_card.supersedes_memory_ids]}")

    # refine / progress_complete / progress_refine：合并产出第三张卡
    other_ids = set(old_card_ids + [new_card_id])
    merged = next(
        (c for c in final_cards
         if c.memory_id not in other_ids and set(c.supersedes_memory_ids) <= other_ids
         and set(c.supersedes_memory_ids) == set(old_card_ids + [new_card_id])),
        None,
    )
    if not merged:
        return CheckResult(
            name=name, passed=False,
            detail=f"{op_expected}: 未找到合并卡（应有第三张卡 supersedes={{old, new}}）",
        )

    expected_type = MemoryType.PROGRESS if op_expected == "progress_refine" else MemoryType.DECISION
    if merged.memory_type != expected_type:
        return CheckResult(
            name=name, passed=False,
            detail=f"{op_expected}: 合并卡 type={merged.memory_type.value} 期望 {expected_type.value}",
        )

    return CheckResult(
        name=name, passed=True,
        detail=f"{op_expected} ok: merged={merged.memory_id[:8]} type={merged.memory_type.value}",
    )


# ── 3) 抗干扰评估 ────────────────────────────────────────────────────────────


def evaluate_noise_classification(
    *,
    blocks: list[EvidenceBlock],
    cards_produced: list[MemoryCard],
) -> dict[str, Any]:
    """抗干扰指标：所有块都应被标 noise；不应产出任何 ACTIVE 非 PROGRESS 决策卡。

    返回 {
      "blocks_total": int,
      "blocks_noise": int,
      "noise_correct_rate": float,  # noise / total
      "cards_unexpected": int,       # 抗干扰 batch 不应有任何决策卡产出
    }
    """
    total = len(blocks)
    noise = sum(1 for b in blocks if (b.block_type or "") == "noise")
    unexpected = sum(
        1 for c in cards_produced
        if c.status == CardStatus.ACTIVE and c.memory_type == MemoryType.DECISION
    )
    return {
        "blocks_total": total,
        "blocks_noise": noise,
        "noise_correct_rate": round(noise / total, 4) if total else None,
        "cards_unexpected": unexpected,
    }
