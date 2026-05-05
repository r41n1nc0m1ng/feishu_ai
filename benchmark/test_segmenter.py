"""
单独测试 event_segmenter 对 batch_001 的切分效果，并将结果保存为 JSON。

运行：
    conda run -n feishu python benchmark/test_segmenter.py
    conda run -n feishu python benchmark/test_segmenter.py --strategy llm
    conda run -n feishu python benchmark/test_segmenter.py --batch 2
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from benchmark.input_simulator import CaseLoader
from benchmark.replay_adapter import DualChannelReplayAdapter
from preprocessor.event_segmenter import segment_async

OUTPUT_PATH = Path(__file__).with_name("segmenter_output.json")


async def main(strategy: str, batch_index: int, fixture_path: Path) -> None:
    os.environ["SEGMENTER_STRATEGY"] = strategy

    loader  = CaseLoader(fixture_path)
    adapter = DualChannelReplayAdapter()

    batch_raw   = loader.batches[batch_index]
    chat_id     = loader.chat_id
    fetch_batch = adapter.to_fetch_batch(batch_raw["messages"], chat_id)

    print(f"fixture : {fixture_path.name}")
    print(f"batch   : {batch_raw.get('batch_id', f'batch_{batch_index+1:03d}')}")
    print(f"strategy: {strategy}")
    print(f"消息总数 : {len(fetch_batch.messages)}")
    print()

    blocks = await segment_async(fetch_batch)

    # ── 终端打印 ──────────────────────────────────────────────────────────────
    print(f"切分结果：共 {len(blocks)} 个 block\n{'=' * 60}")
    for i, block in enumerate(blocks, 1):
        tag   = f"[{block.block_type or '?'}]"
        topic = f"[{block.topic}]" if block.topic else "[topic 未标注]"
        print(f"\nBlock {i}  {tag}  {topic}  消息数={len(block.messages)}")
        if block.one_line_summary:
            print(f"  摘要: {block.one_line_summary}")
        if block.boundary_signal:
            print(f"  信号: {block.boundary_signal}")
        print(f"  时间: {block.start_time.strftime('%H:%M')} – {block.end_time.strftime('%H:%M')}")
        print("  " + "─" * 40)
        for m in block.messages:
            sender = m.sender_name or m.sender_id
            text   = m.text.replace("\n", " ")[:80]
            print(f"  [{m.timestamp.strftime('%H:%M')}] {sender}: {text}")

    print(f"\n{'=' * 60}")
    print("block_type 汇总：")
    from collections import Counter
    for bt, cnt in Counter(b.block_type or "?" for b in blocks).items():
        print(f"  {bt:12} {cnt} 块")

    # ── JSON 序列化 ───────────────────────────────────────────────────────────
    output = {
        "chat_id":   chat_id,
        "batch_id":  batch_raw.get("batch_id", f"batch_{batch_index+1:03d}"),
        "strategy":  strategy,
        "blocks": [
            {
                "block_id":        block.block_id,
                "block_type":      block.block_type,
                "topic":           block.topic,
                "boundary_signal": block.boundary_signal,
                "one_line_summary":block.one_line_summary,
                "start_time":      block.start_time.isoformat(),
                "end_time":        block.end_time.isoformat(),
                "messages": [
                    {
                        "message_id":  m.message_id,
                        "sender_id":   m.sender_id,
                        "sender_name": m.sender_name,
                        "timestamp":   m.timestamp.isoformat(),
                        "text":        m.text,
                    }
                    for m in block.messages
                ],
            }
            for block in blocks
        ],
    }
    OUTPUT_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已写入: {OUTPUT_PATH}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", default="llm", choices=["llm", "semantic", "time"])
    parser.add_argument("--batch",   default=0, type=int, help="batch 下标（从 0 开始）")
    parser.add_argument("--fixture", default=str(ROOT / "benchmark" / "full_demo_case.json"))
    args = parser.parse_args()
    asyncio.run(main(args.strategy, args.batch, Path(args.fixture)))
