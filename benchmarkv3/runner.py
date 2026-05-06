"""
benchmark v3 主入口。

流程：
  1. 加载 fixture（full_demo_case.json）和 test_cases.json
  2. 清空 chat 数据，跑 batch_001..batch_005 的写入侧管线
     每个 batch 末尾跑 3 条松检查查询（决策/证据/topic）
  3. 全部 batch 结束后跑 23 条严格查询（10 决策 + 10 证据 + 3 topic）
  4. 矛盾更新：从实跑产出的 SQLite 卡片按关键词匹配 expected_card_relations，
     再跑 3 条模拟样例（注入 fake old/new 卡 + fake EvidenceBlock，调 apply_operations）
  5. batch_006 抗干扰：处理后统计 noise 块比例

输出报告：benchmarkv3/reports/benchmark_v3_latest.json + 控制台摘要。

运行：
    conda run -n feishu python -m benchmarkv3.runner
    conda run -n feishu python -m benchmarkv3.runner --fixture benchmarkv3/full_demo_case.json --tests benchmarkv3/test_cases.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from benchmark.input_simulator import CaseLoader
from benchmark.replay_adapter import DualChannelReplayAdapter
from memory import store
from memory.batch_processor import BatchProcessor
from memory.card_generator import (
    CardGenerator,
    _cache_card_embedding,
    _card_cache,
    _card_embeddings,
    _cards_by_object,
    _normalize_decision_key,
)
from memory.evidence_store import EvidenceStore, _block_cache
from memory.retriever import MemoryRetriever
from memory.schemas import (
    CardStatus,
    EvidenceBlock,
    EvidenceMessage,
    MemoryCard,
    MemoryType,
)
from preprocessor.event_segmenter import segment_async
from realtime.triggers import (
    is_source_query, is_summary_query, is_topic_list_query, is_version_query,
)

from benchmarkv3.evaluation import (
    CheckResult,
    evaluate_conflict_from_chat,
    evaluate_final_decision_query,
    evaluate_final_evidence_query,
    evaluate_final_topic_query,
    evaluate_noise_classification,
    evaluate_per_batch_query,
    evaluate_simulated_conflict,
)
from benchmarkv3.reporting import (
    build_anti_interference_metrics,
    build_conflict_metrics,
    build_query_metrics,
    render_console_summary,
    write_json_report,
)

logger = logging.getLogger(__name__)


# ── 工具 ──────────────────────────────────────────────────────────────────────


def detect_granularity(query: str) -> str:
    """根据 query 文本判定预期 retrieval 粒度（与 RealtimeQueryHandler 同分支逻辑）。"""
    if is_source_query(query):
        return "evidence_block"
    if is_topic_list_query(query) or is_summary_query(query):
        return "topic_summary"
    if is_version_query(query):
        return "version"
    return "memory_card"


# ── detail snapshot helpers ────────────────────────────────────────────────────


def _card_to_dict(card: MemoryCard, rank: int | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "memory_id":             card.memory_id,
        "decision_object":       card.decision_object,
        "decision":              card.decision,
        "reason":                card.reason,
        "memory_type":           card.memory_type.value,
        "status":                card.status.value,
        "supersedes_memory_ids": list(card.supersedes_memory_ids or []),
        "source_block_ids":      list(card.source_block_ids or []),
    }
    if rank is not None:
        out["rank"] = rank
    return out


def _block_to_dict(block: EvidenceBlock | None) -> dict[str, Any] | None:
    if block is None:
        return None
    return {
        "block_id":         block.block_id,
        "block_type":       block.block_type,
        "one_line_summary": block.one_line_summary,
        "msg_count":        len(block.messages),
        "source_message_ids": [m.message_id for m in block.messages],
        "sample_text": " | ".join((m.text or "")[:60] for m in block.messages[:3]),
    }


def _topic_to_dict(topic, rank: int | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "summary_id":         getattr(topic, "summary_id", ""),
        "topic":              getattr(topic, "topic", ""),
        "summary":            getattr(topic, "summary", ""),
        "covered_memory_ids": list(getattr(topic, "covered_memory_ids", []) or []),
    }
    if rank is not None:
        out["rank"] = rank
    return out


def _build_query_detail(
    *,
    scope: str,
    batch_id: str | None,
    name: str,
    query: str,
    spec: dict[str, Any] | None,
    expected_granularity: str,
    actual_granularity: str | None,
    passed: bool,
    detail: str,
    cards: list,
    block,
    topics: list,
) -> dict[str, Any]:
    return {
        "scope":                  scope,
        "batch_id":               batch_id,
        "name":                   name,
        "query":                  query,
        "expected_granularity":   expected_granularity,
        "actual_granularity":     actual_granularity,
        "expected_keywords":      (spec or {}).get("expected_keywords"),
        "expected_keywords_any":  (spec or {}).get("expected_keywords_any"),
        "forbidden_keywords":     (spec or {}).get("forbidden_keywords"),
        "passed":                 passed,
        "detail":                 detail,
        "top_cards":              [_card_to_dict(c, rank=i + 1) for i, c in enumerate((cards or [])[:5])],
        "expanded_block":         _block_to_dict(block),
        "top_topics":             [_topic_to_dict(t, rank=i + 1) for i, t in enumerate((topics or [])[:5])],
    }


def _infer_operation(head_card: MemoryCard, source_cards: list[MemoryCard]) -> str:
    """根据 head + source 推断 5 分类 operation。"""
    if len(source_cards) == 1:
        return "supersede"
    if head_card.memory_type == MemoryType.PROGRESS:
        return "progress_refine"
    if any(s.memory_type == MemoryType.PROGRESS for s in source_cards):
        return "progress_complete"
    return "refine"


def collect_conflict_updates(chat_id: str) -> list[dict[str, Any]]:
    """扫描该 chat 的全部卡片，找 supersedes_memory_ids 非空的 ACTIVE head，
    解析 source 卡并按 head/source memory_type 推断 operation。"""
    cards = store.get_cards_for_chat(chat_id)
    by_id = {c.memory_id: c for c in cards}
    out: list[dict[str, Any]] = []
    for c in cards:
        if c.status != CardStatus.ACTIVE or not c.supersedes_memory_ids:
            continue
        sources = [by_id.get(sid) for sid in c.supersedes_memory_ids]
        sources = [s for s in sources if s is not None]
        if not sources:
            continue
        op = _infer_operation(c, sources)
        out.append({
            "operation_inferred": op,
            "merged_card":        _card_to_dict(c),
            "source_cards":       [_card_to_dict(s) for s in sources],
        })
    return out


async def retrieve_for_granularity(
    retriever: MemoryRetriever,
    chat_id: str,
    query: str,
    granularity: str,
) -> dict[str, Any]:
    """根据粒度调对应 retriever 方法，返回 {'cards': ..., 'block': ..., 'topics': ...}。"""
    out: dict[str, Any] = {"cards": [], "block": None, "topics": []}
    if granularity == "memory_card":
        out["cards"] = await retriever.retrieve(chat_id, query, limit=5)
    elif granularity == "evidence_block":
        cards = await retriever.retrieve(chat_id, query, limit=1)
        out["cards"] = cards
        if cards:
            for bid in cards[0].source_block_ids:
                blk = await retriever.expand_evidence(bid)
                if blk:
                    out["block"] = blk
                    break
    elif granularity == "topic_summary":
        out["topics"] = await retriever.retrieve_topic_summary(chat_id, query, limit=5)
    return out


def reset_chat_state(chat_id: str) -> None:
    """清掉 SQLite + 内存缓存中该 chat 的所有数据，确保 benchmark 可重复。"""
    store.clear_chat_data(chat_id)
    for cid in [k for k, v in list(_card_cache.items()) if v.chat_id == chat_id]:
        _card_cache.pop(cid, None)
        _card_embeddings.pop(cid, None)
    for k in [k for k, v in list(_cards_by_object.items()) if v.chat_id == chat_id]:
        _cards_by_object.pop(k, None)
    for bid in [k for k, v in list(_block_cache.items()) if v.chat_id == chat_id]:
        _block_cache.pop(bid, None)


# ── 1) 写入侧 + per-batch 松查询 ─────────────────────────────────────────────


async def run_batch_pipeline(
    batch: dict[str, Any],
    case: dict[str, Any],
    bp: BatchProcessor,
    adapter: DualChannelReplayAdapter,
) -> None:
    """跑一个 batch 的写入管线（segmenter → generate → apply_operations → topic 重建）。"""
    chat_id = batch.get("chat_id") or case.get("chat_id")
    write_msgs = adapter.to_fetch_batch(
        [m for m in batch.get("messages", []) if adapter.should_send_to_write_layer(m)],
        chat_id,
    ) if any(adapter.should_send_to_write_layer(m) for m in batch.get("messages", [])) else None

    if write_msgs is None:
        logger.warning("[%s] 无可写入消息，跳过", batch["batch_id"])
        return
    try:
        await bp.process_fetch_batch(write_msgs)
    except Exception:
        logger.exception("process_fetch_batch 异常 | batch=%s", batch["batch_id"])


async def run_per_batch_queries(
    batch_id: str,
    chat_id: str,
    spec: dict[str, str],
    retriever: MemoryRetriever,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, int]], list[dict[str, Any]]]:
    """每 batch 末尾跑 3 条松检查（decision / evidence / topic_summary）。
    返回 (checks, granularity_breakdown, query_details)。
    """
    checks: list[dict[str, Any]] = []
    granularity_breakdown: dict[str, dict[str, int]] = {
        "memory_card":   {"expected": 0, "actual_correct": 0},
        "evidence_block": {"expected": 0, "actual_correct": 0},
        "topic_summary":  {"expected": 0, "actual_correct": 0},
    }
    details: list[dict[str, Any]] = []

    triples = [
        ("decision",       "memory_card",    spec.get("decision_query", "")),
        ("evidence",       "evidence_block", spec.get("evidence_query", "")),
        ("topic_summary",  "topic_summary",  spec.get("topic_summary_query", "")),
    ]
    for qtype, expected_gran, query in triples:
        if not query:
            continue
        actual_gran = detect_granularity(query)
        granularity_breakdown[expected_gran]["expected"] += 1
        if actual_gran == expected_gran:
            granularity_breakdown[expected_gran]["actual_correct"] += 1

        out = await retrieve_for_granularity(retriever, chat_id, query, actual_gran)
        # 候选数（针对每个粒度的"有内容"判定）
        if expected_gran == "memory_card":
            cands = out["cards"]
        elif expected_gran == "evidence_block":
            cands = [out["block"]] if out["block"] else []
        else:
            cands = out["topics"]

        check = evaluate_per_batch_query(
            name=f"{batch_id}.{qtype}",
            query=query,
            expected_granularity=expected_gran,
            actual_granularity=actual_gran,
            candidates=cands,
        )
        checks.append({
            "batch_id": batch_id,
            "query_type": qtype,
            "name": check.name,
            "passed": check.passed,
            "detail": check.detail,
        })
        details.append(_build_query_detail(
            scope=f"per_batch_{qtype}",
            batch_id=batch_id,
            name=check.name,
            query=query,
            spec=None,
            expected_granularity=expected_gran,
            actual_granularity=actual_gran,
            passed=check.passed,
            detail=check.detail,
            cards=out["cards"],
            block=out["block"],
            topics=out["topics"],
        ))

    return checks, granularity_breakdown, details


# ── 2) Final queries ──────────────────────────────────────────────────────────


async def run_final_queries(
    tests: dict[str, Any],
    chat_id: str,
    retriever: MemoryRetriever,
) -> tuple[
    list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]],
    dict[str, dict[str, int]], list[dict[str, Any]],
]:
    """跑 23 条 final 查询（10 决策 + 10 证据 + 3 topic_summary）。
    返回 (decision_checks, evidence_checks, topic_checks, granularity_breakdown, details)。
    """
    decision_specs = tests["final_queries"]["decision_queries"]
    evidence_specs = tests["final_queries"]["evidence_queries"]
    topic_specs    = tests["final_queries"]["topic_summary_queries"]

    granularity_breakdown: dict[str, dict[str, int]] = {
        "memory_card":   {"expected": 0, "actual_correct": 0},
        "evidence_block": {"expected": 0, "actual_correct": 0},
        "topic_summary":  {"expected": 0, "actual_correct": 0},
    }
    details: list[dict[str, Any]] = []

    decision_checks: list[dict[str, Any]] = []
    for i, spec in enumerate(decision_specs):
        query = spec["query"]
        expected_gran = spec.get("expected_granularity", "memory_card")
        actual_gran = detect_granularity(query)
        granularity_breakdown[expected_gran]["expected"] += 1
        if actual_gran == expected_gran:
            granularity_breakdown[expected_gran]["actual_correct"] += 1

        out = await retrieve_for_granularity(retriever, chat_id, query, actual_gran)
        check = evaluate_final_decision_query(
            name=f"final_decision[{i}]",
            spec=spec,
            actual_granularity=actual_gran,
            cards=out["cards"],
        )
        decision_checks.append({"name": check.name, "passed": check.passed, "detail": check.detail})
        details.append(_build_query_detail(
            scope="final_decision", batch_id=None, name=check.name, query=query, spec=spec,
            expected_granularity=expected_gran, actual_granularity=actual_gran,
            passed=check.passed, detail=check.detail,
            cards=out["cards"], block=out["block"], topics=out["topics"],
        ))

    evidence_checks: list[dict[str, Any]] = []
    for i, spec in enumerate(evidence_specs):
        query = spec["query"]
        expected_gran = spec.get("expected_granularity", "evidence_block")
        actual_gran = detect_granularity(query)
        granularity_breakdown[expected_gran]["expected"] += 1
        if actual_gran == expected_gran:
            granularity_breakdown[expected_gran]["actual_correct"] += 1

        out = await retrieve_for_granularity(retriever, chat_id, query, actual_gran)
        check = evaluate_final_evidence_query(
            name=f"final_evidence[{i}]",
            spec=spec,
            actual_granularity=actual_gran,
            block=out["block"],
        )
        evidence_checks.append({"name": check.name, "passed": check.passed, "detail": check.detail})
        details.append(_build_query_detail(
            scope="final_evidence", batch_id=None, name=check.name, query=query, spec=spec,
            expected_granularity=expected_gran, actual_granularity=actual_gran,
            passed=check.passed, detail=check.detail,
            cards=out["cards"], block=out["block"], topics=out["topics"],
        ))

    topic_checks: list[dict[str, Any]] = []
    for i, spec in enumerate(topic_specs):
        query = spec["query"]
        expected_gran = spec.get("expected_granularity", "topic_summary")
        actual_gran = detect_granularity(query)
        granularity_breakdown[expected_gran]["expected"] += 1
        if actual_gran == expected_gran:
            granularity_breakdown[expected_gran]["actual_correct"] += 1

        out = await retrieve_for_granularity(retriever, chat_id, query, actual_gran)
        check = evaluate_final_topic_query(
            name=f"final_topic[{i}]",
            spec=spec,
            actual_granularity=actual_gran,
            topics=out["topics"],
        )
        topic_checks.append({"name": check.name, "passed": check.passed, "detail": check.detail})
        details.append(_build_query_detail(
            scope="final_topic", batch_id=None, name=check.name, query=query, spec=spec,
            expected_granularity=expected_gran, actual_granularity=actual_gran,
            passed=check.passed, detail=check.detail,
            cards=out["cards"], block=out["block"], topics=out["topics"],
        ))

    return decision_checks, evidence_checks, topic_checks, granularity_breakdown, details


# ── 3) 矛盾更新检测 ──────────────────────────────────────────────────────────


def run_conflict_chat_checks(
    cases: list[dict[str, Any]],
    chat_id: str,
) -> list[dict[str, Any]]:
    """从实跑产出的 SQLite 卡片按 head_keywords / source_keywords 关键词匹配 expected。"""
    cards = [c for c in store.get_cards_for_chat(chat_id)]
    out: list[dict[str, Any]] = []
    for spec in cases:
        check = evaluate_conflict_from_chat(
            name=spec.get("name", "conflict_chat"),
            spec=spec,
            cards_in_chat=cards,
        )
        out.append({
            "name": check.name,
            "operation": spec.get("operation"),
            "passed": check.passed,
            "detail": check.detail,
        })
    return out


def _make_evidence_block(chat_id: str, decision_text: str) -> EvidenceBlock:
    """为模拟样例生成一个 fake 块：3 条消息覆盖 decision 文本片段。"""
    base = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    msgs = [
        EvidenceMessage(
            message_id=str(uuid.uuid4()),
            sender_id=f"sim_user_{i}",
            sender_name=f"sim_user_{i}",
            timestamp=base + timedelta(seconds=i * 30),
            text=decision_text,
        )
        for i in range(3)
    ]
    return EvidenceBlock(
        chat_id=chat_id,
        start_time=msgs[0].timestamp,
        end_time=msgs[-1].timestamp,
        messages=msgs,
        block_type="decision",
        one_line_summary=decision_text[:30],
    )


def _make_simulated_card(
    chat_id: str,
    spec: dict[str, Any],
    block_id: str,
) -> MemoryCard:
    return MemoryCard(
        chat_id=chat_id,
        decision_object=spec["decision_object"],
        decision_object_key=_normalize_decision_key(spec["decision_object"]),
        decision=spec["decision"],
        reason=spec.get("reason", ""),
        memory_type=MemoryType(spec.get("memory_type", "decision")),
        status=CardStatus.ACTIVE,
        source_block_ids=[block_id],
        open_questions=spec.get("open_questions", []),
        discussion_scope=spec.get("discussion_scope", []),
    )


async def run_simulated_conflict_cases(
    cases: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """为每个模拟样例：注入 old + new 卡 + fake 块到独立 chat_id，跑 apply_operations，评估。"""
    cg = CardGenerator()
    es = EvidenceStore()
    out: list[dict[str, Any]] = []

    for idx, spec in enumerate(cases):
        chat_id = f"benchmark_v3_sim_{idx}"
        reset_chat_state(chat_id)

        old_card_ids: list[str] = []
        for old_spec in spec["old_cards"]:
            old_block = _make_evidence_block(chat_id, old_spec["decision"])
            await es.save(old_block)
            old_card = _make_simulated_card(chat_id, old_spec, old_block.block_id)
            _card_cache[old_card.memory_id] = old_card
            _cards_by_object[old_card.decision_object_key] = old_card
            store.save_memory_card(old_card)
            await _cache_card_embedding(old_card)
            old_card_ids.append(old_card.memory_id)

        new_block = _make_evidence_block(chat_id, spec["new_card"]["decision"])
        await es.save(new_block)
        new_card = _make_simulated_card(chat_id, spec["new_card"], new_block.block_id)
        _card_cache[new_card.memory_id] = new_card
        store.save_memory_card(new_card)
        await _cache_card_embedding(new_card)

        try:
            final = await cg.apply_operations([new_card])
        except Exception as e:
            logger.exception("simulated conflict apply_operations 异常 | name=%s", spec.get("name"))
            out.append({
                "name": spec.get("name", f"sim_{idx}"),
                "operation": spec.get("operation"),
                "passed": False,
                "detail": f"apply_operations exception: {e}",
            })
            reset_chat_state(chat_id)
            continue

        check = evaluate_simulated_conflict(
            name=spec.get("name", f"sim_{idx}"),
            spec=spec,
            final_cards=final,
            old_card_ids=old_card_ids,
            new_card_id=new_card.memory_id,
        )
        out.append({
            "name": check.name,
            "operation": spec.get("operation"),
            "passed": check.passed,
            "detail": check.detail,
        })

        reset_chat_state(chat_id)

    return out


# ── 4) 抗干扰 batch_006 ──────────────────────────────────────────────────────


async def run_anti_interference(
    batch: dict[str, Any],
    case: dict[str, Any],
    bp: BatchProcessor,
    adapter: DualChannelReplayAdapter,
) -> dict[str, Any]:
    """处理 batch_006，捕获产出的 EvidenceBlock 列表 + 新卡片，做 noise 评估。"""
    chat_id = batch.get("chat_id") or case.get("chat_id")
    cards_before = {c.memory_id for c in store.get_cards_for_chat(chat_id)}
    blocks_before = set(_block_cache.keys())

    raw_msgs = batch.get("messages", [])
    writable = [m for m in raw_msgs if adapter.should_send_to_write_layer(m)]
    if writable:
        fetch_batch = adapter.to_fetch_batch(writable, chat_id)
        try:
            await bp.process_fetch_batch(fetch_batch)
        except Exception:
            logger.exception("anti-interference batch process_fetch_batch 异常")

    new_blocks = [b for bid, b in _block_cache.items()
                  if bid not in blocks_before and b.chat_id == chat_id]
    new_cards = [c for c in store.get_cards_for_chat(chat_id)
                 if c.memory_id not in cards_before]

    metrics = evaluate_noise_classification(blocks=new_blocks, cards_produced=new_cards)
    metrics["block_types"] = [b.block_type for b in new_blocks]
    metrics["new_cards_summary"] = [
        {"memory_type": c.memory_type.value, "status": c.status.value,
         "decision_object": c.decision_object, "decision": c.decision[:80]}
        for c in new_cards
    ]
    return metrics


# ── 主入口 ────────────────────────────────────────────────────────────────────


def merge_granularity(
    base: dict[str, dict[str, int]],
    add: dict[str, dict[str, int]],
) -> dict[str, dict[str, int]]:
    out = {k: dict(v) for k, v in base.items()}
    for k, v in add.items():
        out.setdefault(k, {"expected": 0, "actual_correct": 0})
        out[k]["expected"] += v["expected"]
        out[k]["actual_correct"] += v["actual_correct"]
    return out


async def main(fixture_path: Path, tests_path: Path) -> int:
    print(f"[v3] 加载 fixture: {fixture_path}", flush=True)
    print(f"[v3] 加载 tests:   {tests_path}", flush=True)

    case_data = json.loads(fixture_path.read_text(encoding="utf-8"))
    tests = json.loads(tests_path.read_text(encoding="utf-8"))

    chat_id = case_data["chat_id"]
    anti_id = tests["anti_interference_batch_id"]

    main_batches = [b for b in case_data["batches"] if b["batch_id"] != anti_id]
    anti_batch = next((b for b in case_data["batches"] if b["batch_id"] == anti_id), None)

    print(f"[v3] case_id={case_data['case_id']}  chat_id={chat_id}  "
          f"main_batches={len(main_batches)}  anti_batch={'yes' if anti_batch else 'no'}", flush=True)

    print(f"[v3] 重置 chat 数据 chat_id={chat_id}", flush=True)
    reset_chat_state(chat_id)

    bp = BatchProcessor()
    adapter = DualChannelReplayAdapter()
    retriever = MemoryRetriever()

    per_batch_checks: list[dict[str, Any]] = []
    per_batch_gran: dict[str, dict[str, int]] = {}
    all_query_details: list[dict[str, Any]] = []

    # ── 1+2) 写入 + per-batch 查询 ────────────────────────────────────────────
    for batch in main_batches:
        bid = batch["batch_id"]
        print(f"\n[v3] === {bid} ===", flush=True)
        await run_batch_pipeline(batch, case_data, bp, adapter)

        spec = (tests.get("per_batch_queries") or {}).get(bid) or {}
        if spec:
            checks, gran, details = await run_per_batch_queries(bid, chat_id, spec, retriever)
            per_batch_checks.extend(checks)
            per_batch_gran = merge_granularity(per_batch_gran, gran)
            all_query_details.extend(details)
            for c in checks:
                print(f"  [{('PASS' if c['passed'] else 'FAIL'):4s}] {c['name']}: {c['detail']}", flush=True)

    # ── 3) Final queries ─────────────────────────────────────────────────────
    print("\n[v3] === final queries ===", flush=True)
    decision_checks, evidence_checks, topic_checks, final_gran, final_details = await run_final_queries(
        tests, chat_id, retriever
    )
    all_query_details.extend(final_details)
    for c in decision_checks + evidence_checks + topic_checks:
        print(f"  [{('PASS' if c['passed'] else 'FAIL'):4s}] {c['name']}: {c['detail']}", flush=True)

    # ── 4) 矛盾更新 ──────────────────────────────────────────────────────────
    print("\n[v3] === conflict updates (from chat) ===", flush=True)
    chat_conflict_checks = run_conflict_chat_checks(
        tests["conflict_update_cases"]["from_result_json"], chat_id,
    )
    for c in chat_conflict_checks:
        print(f"  [{('PASS' if c['passed'] else 'FAIL'):4s}] {c['name']}: {c['detail']}", flush=True)

    print("\n[v3] === conflict updates (simulated) ===", flush=True)
    sim_conflict_checks = await run_simulated_conflict_cases(
        tests["conflict_update_cases"]["simulated"],
    )
    for c in sim_conflict_checks:
        print(f"  [{('PASS' if c['passed'] else 'FAIL'):4s}] {c['name']}: {c['detail']}", flush=True)

    # ── 5) 抗干扰 batch_006 ──────────────────────────────────────────────────
    anti_metrics: dict[str, Any] = {
        "blocks_total": 0, "blocks_noise": 0, "noise_correct_rate": None,
        "cards_unexpected": 0,
    }
    if anti_batch:
        print(f"\n[v3] === anti-interference {anti_id} ===", flush=True)
        anti_metrics = await run_anti_interference(anti_batch, case_data, bp, adapter)
        print(f"  blocks_total={anti_metrics.get('blocks_total')}  "
              f"blocks_noise={anti_metrics.get('blocks_noise')}  "
              f"noise_correct_rate={anti_metrics.get('noise_correct_rate')}  "
              f"unexpected_cards={anti_metrics.get('cards_unexpected')}", flush=True)
        for bt in anti_metrics.get("block_types", []):
            print(f"    block_type={bt}", flush=True)

    # ── 6) 报告 ──────────────────────────────────────────────────────────────
    granularity_breakdown = merge_granularity(per_batch_gran, final_gran)

    query_metrics = build_query_metrics(
        per_batch_checks=per_batch_checks,
        final_decision_checks=decision_checks,
        final_evidence_checks=evidence_checks,
        final_topic_checks=topic_checks,
        granularity_breakdown=granularity_breakdown,
    )
    conflict_metrics = build_conflict_metrics(
        chat_checks=chat_conflict_checks,
        simulated_checks=sim_conflict_checks,
    )
    anti_interference_metrics = build_anti_interference_metrics(
        blocks_total=anti_metrics.get("blocks_total", 0),
        blocks_noise=anti_metrics.get("blocks_noise", 0),
        noise_correct_rate=anti_metrics.get("noise_correct_rate"),
        cards_unexpected=anti_metrics.get("cards_unexpected", 0),
    )
    anti_interference_metrics["block_types"] = anti_metrics.get("block_types", [])
    anti_interference_metrics["new_cards_summary"] = anti_metrics.get("new_cards_summary", [])

    report = {
        "case_id": case_data["case_id"],
        "chat_id": chat_id,
        "fixture": str(fixture_path),
        "tests":   str(tests_path),
        "query_metrics":              query_metrics,
        "conflict_metrics":           conflict_metrics,
        "anti_interference_metrics":  anti_interference_metrics,
    }

    report_path = write_json_report("benchmark_v3_latest.json", report)
    report["report_path"] = str(report_path)

    # ── detailed report：每条查询完整请求/响应 + 卡片快照 + 矛盾更新链路 ──
    cards_snapshot = [_card_to_dict(c) for c in store.get_cards_for_chat(chat_id)]
    conflict_updates_observed = collect_conflict_updates(chat_id)
    detailed = {
        "case_id":                    case_data["case_id"],
        "chat_id":                    chat_id,
        "fixture":                    str(fixture_path),
        "tests":                      str(tests_path),
        "summary": {
            "per_batch_passed":  query_metrics["per_batch"]["passed"],
            "per_batch_total":   query_metrics["per_batch"]["total"],
            "final_passed":      query_metrics["final"]["passed"],
            "final_total":       query_metrics["final"]["total"],
            "final_overall_pass_rate": query_metrics["final"]["overall_pass_rate"],
            "anti_interference_passed": anti_interference_metrics["passed"],
        },
        "queries": {
            "per_batch":      [d for d in all_query_details if d["scope"].startswith("per_batch_")],
            "final_decision": [d for d in all_query_details if d["scope"] == "final_decision"],
            "final_evidence": [d for d in all_query_details if d["scope"] == "final_evidence"],
            "final_topic":    [d for d in all_query_details if d["scope"] == "final_topic"],
        },
        "memory_cards_snapshot":      cards_snapshot,
        "conflict_updates_observed":  conflict_updates_observed,
        "conflict_update_checks": {
            "from_chat":  chat_conflict_checks,
            "simulated":  sim_conflict_checks,
        },
        "anti_interference": {
            "blocks_total":      anti_metrics.get("blocks_total"),
            "blocks_noise":      anti_metrics.get("blocks_noise"),
            "noise_correct_rate": anti_metrics.get("noise_correct_rate"),
            "cards_unexpected":  anti_metrics.get("cards_unexpected"),
            "block_types":       anti_metrics.get("block_types", []),
            "new_cards_summary": anti_metrics.get("new_cards_summary", []),
        },
    }
    detailed_path = write_json_report("benchmark_v3_latest_detailed.json", detailed)
    print(f"detailed report: {detailed_path}")

    print(render_console_summary(report))

    # 退出码：final 通过率 ≥ 0.7 + anti-interference passed → exit 0
    final_rate = (query_metrics.get("final") or {}).get("overall_pass_rate") or 0.0
    anti_pass = anti_interference_metrics.get("passed", False)
    overall_ok = final_rate >= 0.7 and anti_pass
    return 0 if overall_ok else 1


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.ERROR)

    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", default=str(Path(__file__).with_name("full_demo_case.json")))
    parser.add_argument("--tests",   default=str(Path(__file__).with_name("test_cases.json")))
    args = parser.parse_args()

    sys.exit(asyncio.run(main(Path(args.fixture), Path(args.tests))))
