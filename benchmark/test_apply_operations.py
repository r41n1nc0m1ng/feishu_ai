"""
6 分类 + apply_operations 端到端测试。

Phase 1：对每个 case 调用 ConflictDetector.classify_pair(new, old)，
        断言返回的 CardRelationOp 与预期一致。
Phase 2：搭起完整缓存与 EvidenceStore，调用 CardGenerator.apply_operations([new_card])，
        断言记忆卡片最终状态符合该 operation 的语义：
          - ADD              new 保留 ACTIVE，old 仍 ACTIVE
          - SUPERSEDE        new 保留，old → DEPRECATED，new.supersedes_memory_ids ⊇ {old}
          - REFINE / PROGRESS_*  生成第三张合并卡，新旧两张源卡均 → DEPRECATED，
                              merged.supersedes_memory_ids == {old, new}，
                              REFINE/PROGRESS_COMPLETE → DECISION，PROGRESS_REFINE → PROGRESS

运行：
    conda run -n feishu python benchmark/test_apply_operations.py

会调用真实 LLM（优先 DeepSeek），约 12+ 次调用，单次跑约 30-90 秒。
测试结束自动清理 SQLite 与缓存。
"""
from __future__ import annotations

import asyncio
import logging
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from memory import store
from memory.card_generator import (
    CardGenerator,
    _card_cache,
    _card_embeddings,
    _cards_by_object,
    _cache_card_embedding,
    _normalize_decision_key,
)
from memory.conflict_detector import ConflictDetector
from memory.evidence_store import EvidenceStore, _block_cache
from memory.schemas import (
    CardRelationOp,
    CardStatus,
    EvidenceBlock,
    EvidenceMessage,
    MemoryCard,
    MemoryType,
)

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("memory.conflict_detector").setLevel(logging.INFO)
logging.getLogger("memory.card_generator").setLevel(logging.INFO)

CHAT_PREFIX = "test_apply_ops_"
BASE_TS = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)


# ── helpers ───────────────────────────────────────────────────────────────────

def _ts(offset_sec: int) -> datetime:
    return BASE_TS + timedelta(seconds=offset_sec)


def _msg(text: str, ts: datetime, sender: str = "u1") -> EvidenceMessage:
    return EvidenceMessage(
        message_id=str(uuid.uuid4()),
        sender_id=sender,
        sender_name=sender,
        timestamp=ts,
        text=text,
    )


def _build_block(
    chat_id: str, msgs_text: list[str], block_type: str, summary: str, base_offset: int
) -> EvidenceBlock:
    msgs = [_msg(t, _ts(base_offset + i * 30)) for i, t in enumerate(msgs_text)]
    return EvidenceBlock(
        chat_id=chat_id,
        start_time=msgs[0].timestamp,
        end_time=msgs[-1].timestamp,
        messages=msgs,
        block_type=block_type,
        one_line_summary=summary,
    )


def _make_card(
    chat_id: str,
    decision_object: str,
    decision: str,
    reason: str,
    memory_type: MemoryType,
    source_block_id: str,
    open_questions: list[str] | None = None,
    discussion_scope: list[str] | None = None,
) -> MemoryCard:
    return MemoryCard(
        chat_id=chat_id,
        decision_object=decision_object,
        decision_object_key=_normalize_decision_key(decision_object),
        decision=decision,
        reason=reason,
        memory_type=memory_type,
        source_block_ids=[source_block_id],
        open_questions=open_questions or [],
        discussion_scope=discussion_scope or [],
    )


def _reset_chat_state(chat_id: str) -> None:
    store.clear_chat_data(chat_id)
    for cid in [k for k, v in list(_card_cache.items()) if v.chat_id == chat_id]:
        _card_cache.pop(cid, None)
        _card_embeddings.pop(cid, None)
    for k in [k for k, v in list(_cards_by_object.items()) if v.chat_id == chat_id]:
        _cards_by_object.pop(k, None)
    for bid in [k for k, v in list(_block_cache.items()) if v.chat_id == chat_id]:
        _block_cache.pop(bid, None)


# ── 6 cases ──────────────────────────────────────────────────────────────────


@dataclass
class Case:
    chat_id: str
    name: str
    expected_op: CardRelationOp
    old_card: MemoryCard
    new_card: MemoryCard
    old_block: EvidenceBlock
    new_block: EvidenceBlock


def case_refine(idx: int) -> Case:
    chat_id = f"{CHAT_PREFIX}{idx:02d}_refine"
    old_b = _build_block(
        chat_id,
        ["不要做账号体系，太复杂", "MVP 不需要登录注册"],
        "decision", "MVP 不做账号", 0,
    )
    new_b = _build_block(
        chat_id,
        ["确认一下 MVP 不做用户登录", "保持精简，只做核心"],
        "decision", "MVP 不做登录", 7200,
    )
    return Case(
        chat_id=chat_id, name="refine 同子方向同观点",
        expected_op=CardRelationOp.REFINE,
        old_card=_make_card(chat_id, "MVP-范围", "MVP 不实现用户账号体系",
                            "账号系统增加复杂度", MemoryType.DECISION, old_b.block_id),
        new_card=_make_card(chat_id, "MVP-范围", "MVP 不做注册登录功能",
                            "保持精简", MemoryType.DECISION, new_b.block_id),
        old_block=old_b, new_block=new_b,
    )


def case_supersede(idx: int) -> Case:
    chat_id = f"{CHAT_PREFIX}{idx:02d}_supersede"
    old_b = _build_block(
        chat_id, ["后端先用 Python，团队都熟", "Python 起步快"],
        "decision", "后端用 Python", 0,
    )
    new_b = _build_block(
        chat_id, ["性能压测发现 Python 扛不住", "决定后端改用 Go 重写"],
        "decision", "改用 Go", 7200,
    )
    return Case(
        chat_id=chat_id, name="supersede 同子方向反观点",
        expected_op=CardRelationOp.SUPERSEDE,
        old_card=_make_card(chat_id, "技术实现-技术选型", "后端使用 Python",
                            "团队熟悉", MemoryType.DECISION, old_b.block_id),
        new_card=_make_card(chat_id, "技术实现-技术选型", "后端改用 Go 重写",
                            "性能更好", MemoryType.DECISION, new_b.block_id),
        old_block=old_b, new_block=new_b,
    )


def case_add_diff_dir(idx: int) -> Case:
    chat_id = f"{CHAT_PREFIX}{idx:02d}_add_diff_dir"
    old_b = _build_block(
        chat_id, ["MVP 里不要账号体系", "降低复杂度"],
        "decision", "MVP 不做账号", 0,
    )
    new_b = _build_block(
        chat_id, ["简历自动解析必须包含在 MVP 里", "这是核心功能"],
        "decision", "MVP 含简历解析", 7200,
    )
    return Case(
        chat_id=chat_id, name="add 同议题不同子方向",
        expected_op=CardRelationOp.ADD,
        old_card=_make_card(chat_id, "MVP-范围", "MVP 不实现用户账号体系",
                            "复杂", MemoryType.DECISION, old_b.block_id),
        new_card=_make_card(chat_id, "MVP-范围", "MVP 包含简历自动解析功能",
                            "核心需求", MemoryType.DECISION, new_b.block_id),
        old_block=old_b, new_block=new_b,
    )


def case_progress_complete(idx: int) -> Case:
    chat_id = f"{CHAT_PREFIX}{idx:02d}_progress_complete"
    old_b = _build_block(
        chat_id,
        ["后端选 Python 还是 Go？", "我倾向 Python", "Go 性能好", "再讨论一下"],
        "progress", "后端语言待定", 0,
    )
    new_b = _build_block(
        chat_id, ["最后定了用 Python", "团队熟，迭代快"],
        "decision", "后端定 Python", 7200,
    )
    return Case(
        chat_id=chat_id, name="progress_complete 完整解答 open_questions",
        expected_op=CardRelationOp.PROGRESS_COMPLETE,
        old_card=_make_card(
            chat_id, "技术实现-技术选型", "后端语言选型讨论",
            "未达成一致", MemoryType.PROGRESS, old_b.block_id,
            open_questions=["后端选 Python 还是 Go?"],
            discussion_scope=["Python", "Go"],
        ),
        new_card=_make_card(chat_id, "技术实现-技术选型", "后端使用 Python",
                            "团队熟悉", MemoryType.DECISION, new_b.block_id),
        old_block=old_b, new_block=new_b,
    )


def case_progress_refine(idx: int) -> Case:
    chat_id = f"{CHAT_PREFIX}{idx:02d}_progress_refine"
    old_b = _build_block(
        chat_id,
        ["后端选 Python 还是 Go？", "数据库 PostgreSQL 还是 MongoDB？", "都待定"],
        "progress", "技术栈待定", 0,
    )
    new_b = _build_block(
        chat_id, ["后端就用 Python 吧", "团队熟"],
        "decision", "后端定 Python", 7200,
    )
    return Case(
        chat_id=chat_id, name="progress_refine 仅部分解答",
        expected_op=CardRelationOp.PROGRESS_REFINE,
        old_card=_make_card(
            chat_id, "技术实现-技术选型", "技术栈选型讨论",
            "多项未定", MemoryType.PROGRESS, old_b.block_id,
            open_questions=[
                "后端选 Python 还是 Go?",
                "数据库选 PostgreSQL 还是 MongoDB?",
            ],
            discussion_scope=["Python", "Go", "PostgreSQL", "MongoDB"],
        ),
        new_card=_make_card(chat_id, "技术实现-技术选型", "后端使用 Python",
                            "团队熟悉", MemoryType.DECISION, new_b.block_id),
        old_block=old_b, new_block=new_b,
    )


def case_add_progress_unrelated(idx: int) -> Case:
    chat_id = f"{CHAT_PREFIX}{idx:02d}_add_progress_unrelated"
    old_b = _build_block(
        chat_id,
        ["MVP 里要不要做账号？", "登录方式选什么？", "继续讨论"],
        "progress", "账号待定", 0,
    )
    new_b = _build_block(
        chat_id, ["简历解析功能要做", "这是 MVP 核心"],
        "decision", "做简历解析", 7200,
    )
    return Case(
        chat_id=chat_id, name="add 同议题但 progress 完全未答",
        expected_op=CardRelationOp.ADD,
        old_card=_make_card(
            chat_id, "MVP-范围", "MVP 账号体系讨论",
            "未达成一致", MemoryType.PROGRESS, old_b.block_id,
            open_questions=["是否包含用户账号体系?", "登录方式选哪种?"],
            discussion_scope=["账号", "登录"],
        ),
        new_card=_make_card(chat_id, "MVP-范围", "MVP 包含简历自动解析功能",
                            "核心需求", MemoryType.DECISION, new_b.block_id),
        old_block=old_b, new_block=new_b,
    )


CASE_BUILDERS = [
    case_refine,
    case_supersede,
    case_add_diff_dir,
    case_progress_complete,
    case_progress_refine,
    case_add_progress_unrelated,
]


def all_cases() -> list[Case]:
    return [build(i) for i, build in enumerate(CASE_BUILDERS, 1)]


# ── phase 1: classification accuracy ──────────────────────────────────────────


async def phase1_classify(cases: list[Case]) -> int:
    print("\nPhase 1 — classify_pair 6 分类准确率")
    print("─" * 70)
    detector = ConflictDetector()
    pass_count = 0
    for case in cases:
        op, reason = await detector.classify_pair(case.new_card, case.old_card)
        ok = op == case.expected_op
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {case.name}")
        print(f"         expected={case.expected_op.value:<18} got={op.value:<18} reason={reason}")
        if ok:
            pass_count += 1
    print(f"Phase 1 result: {pass_count}/{len(cases)}")
    return pass_count


# ── phase 2: end-to-end apply_operations ──────────────────────────────────────


async def _setup_case(case: Case) -> None:
    es = EvidenceStore()
    await es.save(case.old_block)
    await es.save(case.new_block)

    # old_card：写入缓存（ACTIVE）+ SQLite + embedding
    _card_cache[case.old_card.memory_id] = case.old_card
    _cards_by_object[case.old_card.decision_object_key] = case.old_card
    store.save_memory_card(case.old_card)
    await _cache_card_embedding(case.old_card)

    # new_card：同样持久化 + 入缓存（apply_operations 之前 generate() 已写入的等价状态）
    _card_cache[case.new_card.memory_id] = case.new_card
    store.save_memory_card(case.new_card)
    await _cache_card_embedding(case.new_card)


def _verify_state(case: Case, final: list[MemoryCard]) -> tuple[bool, str]:
    old_cur = _card_cache.get(case.old_card.memory_id) or store.load_memory_card(case.old_card.memory_id)
    new_cur = _card_cache.get(case.new_card.memory_id) or store.load_memory_card(case.new_card.memory_id)

    if case.expected_op == CardRelationOp.ADD:
        if len(final) != 1 or final[0].memory_id != case.new_card.memory_id:
            return False, f"final 应仅含 new (got {len(final)})"
        if old_cur.status != CardStatus.ACTIVE:
            return False, f"old 不应变 DEPRECATED"
        return True, "new 保留, old 仍 ACTIVE"

    if case.expected_op == CardRelationOp.SUPERSEDE:
        if len(final) != 1 or final[0].memory_id != case.new_card.memory_id:
            return False, f"final 应仅含 new"
        if old_cur.status != CardStatus.DEPRECATED:
            return False, f"old 应 DEPRECATED (got {old_cur.status.value})"
        if case.old_card.memory_id not in new_cur.supersedes_memory_ids:
            return False, f"new.supersedes_memory_ids 应含 old (got {new_cur.supersedes_memory_ids})"
        return True, f"old DEPRECATED, new.supersedes_memory_ids={new_cur.supersedes_memory_ids}"

    # REFINE / PROGRESS_COMPLETE / PROGRESS_REFINE
    if len(final) != 1:
        return False, f"final 应有 1 张合并卡 (got {len(final)})"
    merged = final[0]
    if merged.memory_id in (case.old_card.memory_id, case.new_card.memory_id):
        return False, "merged 应为新生成卡 (不能是 old 或 new)"
    if old_cur.status != CardStatus.DEPRECATED:
        return False, f"old 应 DEPRECATED (got {old_cur.status.value})"
    if new_cur.status != CardStatus.DEPRECATED:
        return False, f"new 应 DEPRECATED (got {new_cur.status.value})"
    if set(merged.supersedes_memory_ids) != {case.old_card.memory_id, case.new_card.memory_id}:
        return False, f"merged.supersedes_memory_ids 应={{old,new}} got={merged.supersedes_memory_ids}"
    expected_type = (
        MemoryType.PROGRESS if case.expected_op == CardRelationOp.PROGRESS_REFINE else MemoryType.DECISION
    )
    if merged.memory_type != expected_type:
        return False, f"merged.memory_type 应={expected_type.value} got={merged.memory_type.value}"
    return True, (
        f"merged.id={merged.memory_id[:8]} type={merged.memory_type.value} "
        f"supersedes={[i[:8] for i in merged.supersedes_memory_ids]}"
    )


async def phase2_apply(cases: list[Case]) -> int:
    print("\nPhase 2 — apply_operations 端到端状态校验")
    print("─" * 70)
    cg = CardGenerator()
    pass_count = 0
    for case in cases:
        _reset_chat_state(case.chat_id)
        await _setup_case(case)
        try:
            final = await cg.apply_operations([case.new_card])
        except Exception as e:
            print(f"  [FAIL] {case.name} — apply_operations 异常: {e}")
            _reset_chat_state(case.chat_id)
            continue
        ok, detail = _verify_state(case, final)
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {case.name}")
        print(f"         {detail}")
        if ok:
            pass_count += 1
        _reset_chat_state(case.chat_id)
    print(f"Phase 2 result: {pass_count}/{len(cases)}")
    return pass_count


# ── main ──────────────────────────────────────────────────────────────────────


async def main() -> int:
    cases = all_cases()
    print(f"运行 6-case 分类 + 端到端测试  共 {len(cases)} 个 case")

    p1 = await phase1_classify(cases)
    p2 = await phase2_apply(cases)

    total = len(cases)
    print("\n" + "═" * 70)
    print(f"汇总: 分类 {p1}/{total}    端到端 {p2}/{total}")
    print("═" * 70)
    return 0 if (p1 == total and p2 == total) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
