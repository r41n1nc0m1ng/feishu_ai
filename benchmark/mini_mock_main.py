"""
Mini benchmark：仅处理 batch_001，expected 检查全量保留。
用于快速验证分段器和卡片生成效果，不跑全量 5 个 batch。

运行：
    conda run -n feishu python benchmark/mini_mock_main.py
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

# 复用 mock_main 的全部工具函数，只改主流程
from benchmark.mock_main import (
    ReplyCollector,
    card_source_message_ids,
    process_batch,
    run_expected_checks,
)
from benchmark.input_simulator import CaseLoader
from benchmark.replay_adapter import DualChannelReplayAdapter
from memory import store
from memory.batch_processor import BatchProcessor
from memory.card_generator import clear_cache as _clear_card_cache
from memory.evidence_store import EvidenceStore, clear_cache as _clear_block_cache
from memory.graphiti_client import GraphitiClient
from memory.retriever import MemoryRetriever

logger = logging.getLogger(__name__)

RESULT_PATH = Path(__file__).with_name("result_mini.json")
EVAL_PATH   = Path(__file__).with_name("evaluation_mini.json")


async def main(fixture_path: Path = None) -> None:
    fixture_path = fixture_path or Path(__file__).with_name("full_demo_case.json")

    print("加载 fixture...", flush=True)
    loader = CaseLoader(fixture_path)
    print(f"  case_id={loader.case_id}  总批次={len(loader.batches)}  本次只跑 batch_001", flush=True)

    adapter = DualChannelReplayAdapter()
    bp      = BatchProcessor()
    es      = EvidenceStore()
    retriever = MemoryRetriever()

    print("初始化 Graphiti...", flush=True)
    await GraphitiClient.initialize()
    print("Graphiti 初始化完成", flush=True)

    chat_id = loader.chat_id

    print(f"\n清除旧数据 chat_id={chat_id}...", flush=True)
    store.clear_chat_data(chat_id)
    await GraphitiClient().clear_group(chat_id)
    _clear_card_cache(chat_id)
    _clear_block_cache(chat_id)
    print("  清除完毕", flush=True)

    print(f"\n{'#'*55}")
    print(f"  Mini Benchmark  case={loader.case_id}")
    print(f"  只处理 batch_001 / {len(loader.batches)} 个批次")
    print(f"{'#'*55}")

    result: dict[str, Any] = {
        "case_id":  loader.case_id,
        "chat_id":  chat_id,
        "batches":  [],
        "expected_checks": {},
    }

    # 只处理第一个 batch
    batch_001 = loader.batches[0]
    batch_result = await process_batch(
        batch_001, loader, adapter, bp, es, retriever, chat_id
    )
    result["batches"].append(batch_result)

    # 记录当前 TopicSummary
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
    print(f"  batch_001 处理完毕，开始全量 expected 检查")
    print(f"{'#'*55}")

    result["expected_checks"] = await run_expected_checks(
        loader.expected, chat_id, retriever, es
    )

    RESULT_PATH.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n结果已写入: {RESULT_PATH}")

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
    args = parser.parse_args()
    asyncio.run(main(Path(args.fixture) if args.fixture else None))
