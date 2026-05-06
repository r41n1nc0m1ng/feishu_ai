"""
Mock 主程序：模拟飞书输入 → 实时层 + 写入层完整流水线，结果保存到 result.json。

运行：
    conda run -n feishu python benchmark/mock_main.py
    conda run -n feishu python benchmark/mock_main.py --fixture benchmark/full_demo_case.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from benchmark.input_simulator import CaseLoader
from benchmark.replay_adapter import DualChannelReplayAdapter
from memory import store
from memory.batch_processor import BatchProcessor
from memory.evidence_store import EvidenceStore
from memory.retriever import MemoryRetriever
from memory.schemas import FeishuMessage
from realtime.action_handler import RealtimeActionHandler
from realtime.dispatcher import dispatch_message
from realtime.query_handler import RealtimeQueryHandler

logger = logging.getLogger(__name__)

RESULT_PATH = Path(__file__).with_name("result.json")
EVAL_PATH   = Path(__file__).with_name("evaluation.json")


# ── 工具类 ────────────────────────────────────────────────────────────────────

class ReplyCollector:
    def __init__(self):
        self.replies: list[str] = []

    async def send_text(self, chat_id: str, text: str) -> None:
        self.replies.append(text)

    def flush(self) -> str:
        text = "\n".join(self.replies)
        self.replies.clear()
        return text


def make_at_bot_message(chat_id: str, query: str, msg_id: str) -> FeishuMessage:
    return FeishuMessage(
        message_id=msg_id,
        sender_id="ou_evaluator",
        chat_id=chat_id,
        chat_type="group",
        text=f"@机器人 {query}",
        timestamp=datetime.now(tz=timezone.utc),
        is_at_bot=True,
    )


async def card_source_message_ids(card, es: EvidenceStore) -> list[str]:
    ids: list[str] = []
    for block_id in card.source_block_ids:
        block = await es.get(block_id)
        if block:
            ids.extend(m.message_id for m in block.messages)
    return ids


# ── 单 batch 处理 ─────────────────────────────────────────────────────────────

async def process_batch(
    batch: dict[str, Any],
    loader: CaseLoader,
    adapter: DualChannelReplayAdapter,
    bp: BatchProcessor,
    es: EvidenceStore,
    retriever: MemoryRetriever,
    chat_id: str,
) -> dict[str, Any]:
    batch_id   = batch.get("batch_id", "")
    total_msgs = len(loader.realtime_messages(batch))
    print(f"\n{'='*55}")
    print(f"[{batch_id}] 开始处理  共 {total_msgs} 条消息")
    print(f"{'='*55}")

    collector = ReplyCollector()
    query_handler  = RealtimeQueryHandler(retriever=retriever, send_text=collector.send_text)
    action_handler = RealtimeActionHandler(send_text=collector.send_text)

    # 1. 实时层：逐条发送，收集 @bot 回复
    print(f"[{batch_id}] 实时层处理中...")
    realtime_bot_replies: list[dict] = []
    at_bot_count = 0
    for raw_msg in loader.realtime_messages(batch):
        msg = adapter.to_realtime_message(raw_msg, chat_id)
        try:
            await dispatch_message(msg, query_handler=query_handler, action_handler=action_handler)
        except Exception:
            logger.exception("realtime dispatch 异常 | msg=%s", msg.message_id)

        if loader.is_at_bot(raw_msg) and collector.replies:
            reply_text = collector.flush()
            at_bot_count += 1
            print(f"  @bot 查询 [{raw_msg.get('message_id','')}]: {loader.message_text(raw_msg)[:50]}")
            print(f"  回复: {reply_text[:80]}{'...' if len(reply_text) > 80 else ''}")
            realtime_bot_replies.append({
                "trigger_message_id": raw_msg.get("message_id", ""),
                "query": loader.message_text(raw_msg),
                "reply": reply_text,
            })
        else:
            collector.flush()

    print(f"[{batch_id}] 实时层完成  @bot 触发 {at_bot_count} 次")

    # 2. 写入层：整批送入完整流水线（模拟轮询触发）
    write_msgs   = loader.write_messages(batch)
    cards_before = {c.memory_id for c in store.load_all_memory_cards()}
    print(f"[{batch_id}] 写入层处理中  {len(write_msgs)} 条有效消息...")
    if write_msgs:
        fetch_batch = adapter.to_fetch_batch(write_msgs, chat_id)
        try:
            await bp.process_fetch_batch(fetch_batch)
        except Exception:
            logger.exception("process_fetch_batch 异常 | batch=%s", batch_id)

    # 3. 记录本 batch 新生成的 MemoryCard
    new_cards = [c for c in store.load_all_memory_cards() if c.memory_id not in cards_before]
    memory_card_entries: list[dict] = []
    for card in new_cards:
        msg_ids = await card_source_message_ids(card, es)
        memory_card_entries.append({
            "memory_id":          card.memory_id,
            "decision_object":    card.decision_object,
            "decision":           card.decision,
            "status":             card.status.value,
            "memory_type":        card.memory_type.value,
            "source_block_ids":   card.source_block_ids,
            "source_message_ids": msg_ids,
        })
        print(f"  新卡片: [{card.memory_type.value}/{card.status.value}] {card.decision_object}")

    print(f"[{batch_id}] 完成  新增 MemoryCard {len(new_cards)} 张")

    # 注意：Graphiti 写入由 BatchProcessor.process_fetch_batch 负责（其末尾已对最终
    # 存活卡片做并发 add_episode）。此处不再重复写，避免 add_episode 被调两遍 → 慢一倍。

    return {
        "batch_id":             batch_id,
        "realtime_bot_replies": realtime_bot_replies,
        "memory_cards":         memory_card_entries,
    }


# ── expected 部分检查 ─────────────────────────────────────────────────────────

async def run_expected_checks(
    expected: dict[str, Any],
    chat_id: str,
    retriever: MemoryRetriever,
    es: EvidenceStore,
) -> dict[str, Any]:

    def _action_to_memory_type(action: str) -> str:
        if action == "source":
            return "evidence_block"
        if action in ("summary", "topic_list", "summary_fallback"):
            return "topic_summary"
        return "memory_card"

    async def query_bot(query: str, msg_id: str) -> tuple[str, list[str], str]:
        """返回 (reply, source_ids, bot_reply_memory_type)。"""
        collector = ReplyCollector()
        qh  = RealtimeQueryHandler(retriever=retriever, send_text=collector.send_text)
        msg = make_at_bot_message(chat_id, query, msg_id)
        action = "query"
        try:
            trace  = await qh.handle_query_message(msg)
            action = getattr(trace, "action", "query")
        except Exception:
            logger.exception("expected check 异常 | query=%s", query)
        reply = collector.flush()

        # 获取证据消息 id（source query 专用）
        source_ids: list[str] = []
        results = await retriever.retrieve(chat_id, query, limit=1)
        if results:
            top = results[0]
            for block_id in top.source_block_ids:
                block = await es.get(block_id)
                if block:
                    source_ids = [m.message_id for m in block.messages]
                    break
        return reply, source_ids, _action_to_memory_type(action)

    # final_memory_checks
    fmc_list = expected.get("final_memory_checks", [])
    print(f"\n[expected] 运行 final_memory_checks ({len(fmc_list)} 项)...")
    memory_results: list[dict] = []
    for i, check in enumerate(fmc_list):
        query      = check.get("query", "")
        granularity = check.get("expected_granularity", "memory_card")
        print(f"  [{i+1}/{len(fmc_list)}] {query[:55]}")
        reply, src_ids, actual_type = await query_bot(query, f"exp_mem_{i:02d}")
        type_ok = actual_type == granularity
        print(f"    granularity={granularity}  actual={actual_type}  {'OK' if type_ok else 'MISMATCH'}")

        entry: dict[str, Any] = {
            "query":                 query,
            "granularity":           granularity,
            "bot_reply_memory_type": actual_type,
            "bot_reply":             reply,
            "expected_keywords":     check.get("expected_keywords", []),
            "forbidden_keywords":    check.get("forbidden_keywords", []),
        }
        if granularity == "evidence_block":
            entry["source_message_ids"] = src_ids
        memory_results.append(entry)

    # relation_checks（直接读 SQLite，不走查询）
    rc_list = expected.get("relation_checks", [])
    print(f"\n[expected] 运行 relation_checks ({len(rc_list)} 项)...")
    all_relations = store.load_relations_by_chat(chat_id)
    cards_map = {c.memory_id: c for c in store.load_all_memory_cards()}
    relation_results: list[dict] = []
    for check in expected.get("relation_checks", []):
        rel_type = check.get("relation_type", "")
        old_kws  = check.get("old_expected_keywords", [])
        new_kws  = check.get("new_expected_keywords", [])
        found_entry: dict[str, Any] | None = None
        for rel in all_relations:
            if rel.relation_type.value.lower() != rel_type.lower():
                continue
            new_card = cards_map.get(rel.source_id)
            old_card = cards_map.get(rel.target_id)
            if not (new_card and old_card):
                continue
            new_text = f"{new_card.decision} {new_card.decision_object}"
            old_text = f"{old_card.decision} {old_card.decision_object}"
            if any(kw in old_text for kw in old_kws) and any(kw in new_text for kw in new_kws):
                found_entry = {
                    "relation_type": rel_type,
                    "found": True,
                    "old_card": {"memory_id": old_card.memory_id, "decision": old_card.decision[:100]},
                    "new_card": {"memory_id": new_card.memory_id, "decision": new_card.decision[:100]},
                }
                break
        if found_entry is None:
            found_entry = {
                "relation_type":       rel_type,
                "found":               False,
                "old_expected_keywords": old_kws,
                "new_expected_keywords": new_kws,
            }
        relation_results.append(found_entry)

    # evidence_checks
    ev_list = expected.get("evidence_checks", [])
    print(f"\n[expected] 运行 evidence_checks ({len(ev_list)} 项)...")
    evidence_results: list[dict] = []
    for i, check in enumerate(ev_list):
        query = check.get("query", "")
        print(f"  [{i+1}/{len(ev_list)}] {query[:55]}")
        reply, src_ids, actual_type = await query_bot(query, f"exp_ev_{i:02d}")
        type_ok = actual_type == "evidence_block"
        print(f"    granularity=evidence_block  actual={actual_type}  {'OK' if type_ok else 'MISMATCH'}")

        evidence_results.append({
            "query":                       query,
            "granularity":                 "evidence_block",
            "bot_reply_memory_type":       actual_type,
            "bot_reply":                   reply,
            "actual_source_message_ids":   src_ids,
            "expected_source_message_ids": check.get("expected_source_message_ids", []),
            "expected_keywords":           check.get("expected_keywords", []),
        })

    return {
        "final_memory_checks": memory_results,
        "relation_checks":     relation_results,
        "evidence_checks":     evidence_results,
    }


# ── 主入口 ────────────────────────────────────────────────────────────────────

async def main(fixture_path: Path = None) -> None:
    fixture_path = fixture_path or Path(__file__).with_name("full_demo_case.json")

    print("加载 fixture...", flush=True)
    loader = CaseLoader(fixture_path)
    print(f"  case_id={loader.case_id}  batches={len(loader.batches)}", flush=True)

    print("初始化 DualChannelReplayAdapter...", flush=True)
    adapter = DualChannelReplayAdapter()

    print("初始化 BatchProcessor...", flush=True)
    bp = BatchProcessor()

    print("初始化 EvidenceStore...", flush=True)
    es = EvidenceStore()

    print("初始化 MemoryRetriever...", flush=True)
    retriever = MemoryRetriever()

    print("初始化 Graphiti (连接 Neo4j)...", flush=True)
    from memory.graphiti_client import GraphitiClient
    await GraphitiClient.initialize()
    print("Graphiti 初始化完成", flush=True)

    chat_id = loader.chat_id

    # 每次运行前清除旧数据，确保结果可复现
    print(f"\n清除旧数据 chat_id={chat_id}...", flush=True)
    store.clear_chat_data(chat_id)
    print("  SQLite 清除完毕", flush=True)
    await GraphitiClient().clear_group(chat_id)
    print("  Neo4j 清除完毕", flush=True)
    from memory.card_generator import clear_cache as _clear_card_cache
    from memory.evidence_store import clear_cache as _clear_block_cache
    _clear_card_cache(chat_id)
    _clear_block_cache(chat_id)
    print("  内存缓存清除完毕", flush=True)

    total_batches = len(loader.batches)
    print(f"\n{'#'*55}")
    print(f"  Benchmark 开始  case={loader.case_id}")
    print(f"  共 {total_batches} 个 batch  chat={chat_id}")
    print(f"{'#'*55}")

    result: dict[str, Any] = {
        "case_id":  loader.case_id,
        "chat_id":  chat_id,
        "batches":  [],
        "expected_checks": {},
    }

    # 逐 batch 处理
    for idx, batch in enumerate(loader.batches, 1):
        print(f"\n进度: {idx}/{total_batches}")
        batch_result = await process_batch(
            batch, loader, adapter, bp, es, retriever, chat_id
        )
        result["batches"].append(batch_result)

    # 记录所有 MemoryCard 终态（含 active 与 deprecated，便于审计 supersede/merge 链）
    all_cards = store.get_cards_for_chat(chat_id)
    result["final_memory_cards"] = [
        {
            "memory_id":             c.memory_id,
            "decision_object":       c.decision_object,
            "decision":              c.decision,
            "memory_type":           c.memory_type.value,
            "status":                c.status.value,
            "supersedes_memory_ids": c.supersedes_memory_ids,
        }
        for c in all_cards
    ]
    active_n     = sum(1 for c in all_cards if c.status.value == "active")
    deprecated_n = sum(1 for c in all_cards if c.status.value == "deprecated")
    print(f"\n卡片终态：共 {len(all_cards)} 张  active={active_n}  deprecated={deprecated_n}")

    # 记录当前所有 TopicSummary
    all_topics = store.load_topics_by_chat(chat_id)
    result["topic_summaries"] = [
        {
            "summary_id": t.summary_id,
            "topic":      t.topic,
            "summary":    t.summary,
            "covered_memory_ids": t.covered_memory_ids,
        }
        for t in all_topics
    ]
    print(f"\n当前 TopicSummary 共 {len(all_topics)} 条:")
    for t in all_topics:
        print(f"  【{t.topic}】{t.summary[:60]}")

    print(f"\n{'#'*55}")
    print(f"  所有 batch 处理完毕，开始 expected 检查")
    print(f"{'#'*55}")

    # expected 部分检查（跳过 action_checks）
    result["expected_checks"] = await run_expected_checks(
        loader.expected, chat_id, retriever, es
    )

    # 写入 result.json
    RESULT_PATH.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n结果已写入: {RESULT_PATH}")

    # 运行评估
    print(f"\n{'#'*55}")
    print(f"  运行评估脚本")
    print(f"{'#'*55}")
    from benchmark.evaluator import run_evaluation
    run_evaluation(
        result_path=RESULT_PATH,
        fixture_path=fixture_path,
        output_path=EVAL_PATH,
    )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # 屏蔽高频低价值 INFO
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("neo4j.notifications").setLevel(logging.WARNING)
    logging.getLogger("realtime.dispatcher").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", default=None)
    args = parser.parse_args()
    asyncio.run(main(Path(args.fixture) if args.fixture else None))
