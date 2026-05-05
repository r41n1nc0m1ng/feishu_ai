"""
手动触发 TopicSummary 重建脚本。

读取 SQLite 中指定 chat_id 的 ACTIVE + 非 PROGRESS 卡片，调用 TopicManager.rebuild_topics
重新归并主题，覆盖原有 TopicSummary。

用法：
    conda run -n feishu python scripts/rebuild_topic_summary.py <chat_id>
    conda run -n feishu python scripts/rebuild_topic_summary.py oc_demo_ai_resume

不带参数时列出 SQLite 内所有已注册 chat_id 与卡片数。
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from memory import store
from memory.schemas import CardStatus, MemoryType
from memory.topic_manager import TopicManager


def _list_chats() -> None:
    spaces = store.load_all_chat_spaces()
    if not spaces:
        print("（SQLite 中暂无已注册 chat_space）")
    else:
        print(f"已注册 chat_space ({len(spaces)} 个):")
        for s in spaces:
            cards = store.get_cards_for_chat(s.chat_id)
            active = sum(1 for c in cards
                         if c.status == CardStatus.ACTIVE and c.memory_type != MemoryType.PROGRESS)
            print(f"  {s.chat_id:40s} 总卡片={len(cards):>3}  可聚合={active}")


async def main(chat_id: str) -> int:
    cards = store.get_cards_for_chat(chat_id)
    eligible = [c for c in cards
                if c.status == CardStatus.ACTIVE and c.memory_type != MemoryType.PROGRESS]
    print(f"chat_id={chat_id}")
    print(f"  SQLite 总卡片={len(cards)}  ACTIVE+非PROGRESS={len(eligible)}")
    if len(eligible) < 2:
        print("  可聚合卡片 < 2，TopicManager 会跳过重建。")
        return 0

    print(f"\n参与重建的卡片:")
    for c in eligible:
        print(f"  [{c.memory_type.value}] {c.decision_object}  →  {c.decision[:60]}")

    print(f"\n调用 LLM 归并主题…", flush=True)
    summaries = await TopicManager().rebuild_topics(chat_id)
    print(f"\n重建结果：{len(summaries)} 个 topic")
    for s in summaries:
        print(f"\n  【{s.topic}】covered={len(s.covered_memory_ids)} 张")
        print(f"    summary: {s.summary}")
        for mid in s.covered_memory_ids:
            card = next((c for c in cards if c.memory_id == mid), None)
            if card:
                print(f"      - {card.decision_object}: {card.decision[:50]}")
    return 0 if summaries else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if len(sys.argv) < 2:
        _list_chats()
        print("\nUsage: python scripts/rebuild_topic_summary.py <chat_id>")
        sys.exit(0)

    sys.exit(asyncio.run(main(sys.argv[1])))
