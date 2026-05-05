"""
单独测试 card_generator：读取 segmenter_output.json，对每个 block 生成 MemoryCard，
dry_run 模式下不写入 SQLite / 内存缓存 / Graphiti，结果直接打印。

运行：
    conda run -n feishu python benchmark/test_card_gen.py
    conda run -n feishu python benchmark/test_card_gen.py --input benchmark/segmenter_output.json
    conda run -n feishu python benchmark/test_card_gen.py --skip-noise false
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from memory import store
from memory.card_generator import CardGenerator, clear_cache as _clear_card_cache
from memory.evidence_store import clear_cache as _clear_block_cache
from memory.schemas import EvidenceBlock, EvidenceMessage

DEFAULT_INPUT = Path(__file__).with_name("segmenter_output.json")


def _parse_dt(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _load_blocks(path: Path) -> tuple[str, list[EvidenceBlock]]:
    data   = json.loads(path.read_text(encoding="utf-8"))
    chat_id = data["chat_id"]
    blocks: list[EvidenceBlock] = []
    for b in data["blocks"]:
        msgs = [
            EvidenceMessage(
                message_id  = m["message_id"],
                sender_id   = m["sender_id"],
                sender_name = m.get("sender_name", ""),
                timestamp   = _parse_dt(m["timestamp"]),
                text        = m["text"],
            )
            for m in b["messages"]
        ]
        block = EvidenceBlock(
            block_id         = b["block_id"],
            chat_id          = chat_id,
            start_time       = _parse_dt(b["start_time"]),
            end_time         = _parse_dt(b["end_time"]),
            messages         = msgs,
            block_type       = b.get("block_type"),
            topic            = b.get("topic"),
            boundary_signal  = b.get("boundary_signal"),
            one_line_summary = b.get("one_line_summary"),
        )
        blocks.append(block)
    return chat_id, blocks


async def main(input_path: Path, skip_noise: bool) -> None:
    if not input_path.exists():
        print(f"[ERROR] 找不到输入文件: {input_path}")
        print("请先运行: python benchmark/test_segmenter.py")
        sys.exit(1)

    chat_id, blocks = _load_blocks(input_path)

    # 清空 SQLite + 内存缓存，确保 _format_existing 注入的 prompt 上下文每次都一致
    print(f"清空 chat_id={chat_id} 的 SQLite 和缓存（保证 prompt 输入稳定）...")
    store.clear_chat_data(chat_id)
    _clear_card_cache(chat_id)
    _clear_block_cache(chat_id)

    generator = CardGenerator()

    print(f"输入: {input_path.name}  chat_id={chat_id}  blocks={len(blocks)}")
    print(f"LLM_SEED={os.getenv('LLM_SEED', '42')}（修改环境变量可换种子）")
    print("=" * 60)

    noise_skipped = 0
    for i, block in enumerate(blocks, 1):
        bt = block.block_type or "?"
        summary = block.one_line_summary or ""

        if skip_noise and block.block_type == "noise":
            noise_skipped += 1
            print(f"\nBlock {i}  [noise]  {summary[:50]}  → 跳过")
            continue

        print(f"\nBlock {i}  [{bt}]  {summary[:50]}")
        print(f"    消息数={len(block.messages)} ")

        card = await generator.generate(block, skip_graphiti=True, dry_run=True)

        if card is None:
            print("  → NOOP（无记忆价值）")
            continue

        print(f"  ┌ object    : {card.decision_object}")
        print(f"  │ decision  : {card.decision}")
        print(f"  │ reason    : {card.reason}")
        print(f"  │ type      : {card.memory_type.value}")
        if card.memory_type.value == "progress":
            for tc in card.tentative_consensus:
                print(f"  │   ✓ 共识: {tc}")
            for oq in card.open_questions:
                print(f"  │   ? 待决: {oq}")
            if card.discussion_scope:
                print(f"  │   ⊙ 范围: {', '.join(card.discussion_scope)}")
            if card.next_step:
                print(f"  │   → 下步: {card.next_step}")
        print(f"  └ memory_id : {card.memory_id[:8]}…")

    print(f"\n{'=' * 60}")
    print(f"共处理 {len(blocks)} 个 block，跳过 noise {noise_skipped} 个")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input",      default=str(DEFAULT_INPUT))
    parser.add_argument("--skip-noise", default="true",
                        type=lambda x: x.lower() not in ("false", "0", "no"))
    args = parser.parse_args()
    asyncio.run(main(Path(args.input), args.skip_noise))
