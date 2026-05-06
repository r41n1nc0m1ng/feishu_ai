"""
Mini benchmark：测试整体流水线（分段 → 卡片生成 → 检索）。

改动相比 mock_main：
  - 跳过 Graphiti 初始化（ConflictDetector 已禁用，检索走 embedding）
  - 默认仅跑 batch_001（--batches 可改）
  - 卡片记录包含 PROGRESS 专属字段（tentative_consensus / open_questions / ...）
  - 额外保存所有 EvidenceBlock 的 block_type / topic / one_line_summary

运行：
    conda run -n feishu python benchmark/mini_mock_main.py
    conda run -n feishu python benchmark/mini_mock_main.py --batches 5
    conda run -n feishu python benchmark/mini_mock_main.py --fixture benchmark/full_demo_case.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from benchmark.mock_main import process_batch, run_expected_checks
from benchmark.input_simulator import CaseLoader
from benchmark.replay_adapter import DualChannelReplayAdapter
from memory import store
from memory.batch_processor import BatchProcessor
from memory.card_generator import clear_cache as _clear_card_cache
from memory.evidence_store import EvidenceStore, clear_cache as _clear_block_cache
from memory.retriever import MemoryRetriever
from memory.schemas import MemoryType

logger = logging.getLogger(__name__)

RESULT_PATH = Path(__file__).with_name("result_mini.json")
EVAL_PATH   = Path(__file__).with_name("evaluation_mini.json")


def _card_to_entry(card, msg_ids: list[str]) -> dict[str, Any]:
    """将 MemoryCard 序列化为 result.json 中的 entry，PROGRESS 卡补充四个字段。"""
    entry: dict[str, Any] = {
        "memory_id":             card.memory_id,
        "decision_object":       card.decision_object,
        "decision":              card.decision,
        "reason":                card.reason,
        "status":                card.status.value,
        "memory_type":           card.memory_type.value,
        "source_block_ids":      card.source_block_ids,
        "source_message_ids":    msg_ids,
        "supersedes_memory_ids": card.supersedes_memory_ids,
    }
    if card.memory_type == MemoryType.PROGRESS:
        entry["tentative_consensus"] = card.tentative_consensus
        entry["open_questions"]      = card.open_questions
        entry["discussion_scope"]    = card.discussion_scope
        entry["next_step"]           = card.next_step
    return entry


async def _block_summary(es: EvidenceStore, block_id: str) -> dict[str, Any] | None:
    block = await es.get(block_id)
    if not block:
        return None
    return {
        "block_id":         block.block_id,
        "block_type":       block.block_type,
        "topic":            block.topic,
        "boundary_signal":  block.boundary_signal,
        "one_line_summary": block.one_line_summary,
        "message_ids":      [m.message_id for m in block.messages],
        "msg_count":        len(block.messages),
    }


async def main(fixture_path: Path = None, n_batches: int = 1) -> None:
    fixture_path = fixture_path or Path(__file__).with_name("full_demo_case.json")

    print("加载 fixture...", flush=True)
    loader = CaseLoader(fixture_path)
    n_batches = min(n_batches, len(loader.batches))
    print(f"  case_id={loader.case_id}  总批次={len(loader.batches)}  本次跑前 {n_batches} 个 batch", flush=True)

    adapter   = DualChannelReplayAdapter()
    bp        = BatchProcessor()
    es        = EvidenceStore()
    retriever = MemoryRetriever()

    chat_id = loader.chat_id

    # 跳过 Graphiti 初始化：ConflictDetector 禁用，检索走 embedding
    print(f"\n清除旧数据 chat_id={chat_id}...", flush=True)
    store.clear_chat_data(chat_id)
    _clear_card_cache(chat_id)
    _clear_block_cache(chat_id)
    print("  清除完毕（已跳过 Graphiti 清理）", flush=True)

    print(f"\n{'#'*55}")
    print(f"  Mini Benchmark  case={loader.case_id}")
    print(f"  跑 {n_batches} / {len(loader.batches)} 个批次")
    print(f"{'#'*55}")

    result: dict[str, Any] = {
        "case_id":  loader.case_id,
        "chat_id":  chat_id,
        "n_batches": n_batches,
        "batches":  [],
        "expected_checks": {},
    }

    # 处理 batches
    for i in range(n_batches):
        batch = loader.batches[i]
        batch_result = await process_batch(batch, loader, adapter, bp, es, retriever, chat_id)

        # 把 process_batch 给的卡片条目用完整字段重写一遍（含 PROGRESS 专属字段）
        new_card_ids = {entry["memory_id"] for entry in batch_result.get("memory_cards", [])}
        rich_cards = []
        for mid in new_card_ids:
            card = store.load_memory_card(mid)
            if not card:
                continue
            msg_ids: list[str] = []
            for bid in card.source_block_ids:
                blk = await es.get(bid)
                if blk:
                    msg_ids.extend(m.message_id for m in blk.messages)
            rich_cards.append(_card_to_entry(card, msg_ids))
        batch_result["memory_cards"] = rich_cards

        # 附加所有 evidence block 的元信息（按 source_block_ids 去重收集）
        block_ids = {bid for entry in rich_cards for bid in entry["source_block_ids"]}
        block_info: list[dict] = []
        for bid in block_ids:
            info = await _block_summary(es, bid)
            if info:
                block_info.append(info)
        batch_result["evidence_blocks"] = block_info

        result["batches"].append(batch_result)

    # 当前 TopicSummary
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
    print(f"  全部 batch 处理完毕，开始全量 expected 检查")
    print(f"{'#'*55}")

    result["expected_checks"] = await run_expected_checks(loader.expected, chat_id, retriever, es)

    RESULT_PATH.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n结果已写入: {RESULT_PATH}")

    # ── 终端简要总结 ────────────────────────────────────────────────────────────
    total_cards   = sum(len(b["memory_cards"]) for b in result["batches"])
    progress_cnt  = sum(1 for b in result["batches"] for c in b["memory_cards"]
                        if c["memory_type"] == "progress")
    print(f"\n卡片统计：共 {total_cards} 张（其中 PROGRESS {progress_cnt} 张）")

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
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("neo4j.notifications").setLevel(logging.WARNING)
    logging.getLogger("realtime.dispatcher").setLevel(logging.WARNING)

    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", default=None)
    parser.add_argument("--batches", type=int, default=1, help="处理前 N 个 batch（默认 1）")
    args = parser.parse_args()
    asyncio.run(main(
        Path(args.fixture) if args.fixture else None,
        n_batches=args.batches,
    ))
