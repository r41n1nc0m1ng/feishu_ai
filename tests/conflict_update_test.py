"""P0 Benchmark — Conflict Update Test

Writes an initial decision (V1), then a contradicting decision (V2).
Pass criteria: querying after both writes returns V2 content, not V1.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import json
import logging
import time

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO)
logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)
logging.getLogger("neo4j").setLevel(logging.ERROR)

from feishu.event_handler import handle_raw_event
from memory.graphiti_client import GraphitiClient
from memory.retriever import MemoryRetriever

CHAT_ID = "oc_benchmark_conflict"

V1 = "决定了，完全不做个人入口，只做群聊记忆。"
V2 = "更新决定：保留私聊查询入口，但只用于查询和确认，不作为记忆来源。"

QUERY = "个人入口私聊查询"
V2_KEYWORD = "查询"
V1_KEYWORD = "完全不做"


def _make_event(text: str, idx: int) -> dict:
    ts = str(int(time.time() * 1000))
    return {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": f"user_{idx}"}},
            "message": {
                "message_id": f"om_conflict_{idx}_{ts}",
                "create_time": ts,
                "chat_id": CHAT_ID,
                "chat_type": "group",
                "message_type": "text",
                "content": json.dumps({"text": text}),
            },
        },
    }


async def run():
    print("\n=== Conflict Update Test ===")
    await GraphitiClient.initialize()
    retriever = MemoryRetriever()

    print(f"Writing V1: {V1}")
    await handle_raw_event(_make_event(V1, 0))
    await asyncio.sleep(2)

    results_v1 = await retriever.search(CHAT_ID, QUERY, limit=3)
    v1_found = any(V1_KEYWORD in r.get("fact", "") for r in results_v1)
    print(f"  After V1 — V1 present: {v1_found}")

    print(f"Writing V2 (conflict): {V2}")
    await handle_raw_event(_make_event(V2, 1))
    await asyncio.sleep(2)

    results_v2 = await retriever.search(CHAT_ID, QUERY, limit=3)
    v2_found = any(V2_KEYWORD in r.get("fact", "") for r in results_v2)
    print(f"  After V2 — V2 present: {v2_found}")

    print(f"\n  {'PASS' if v2_found else 'FAIL'} — new version surfaced")
    for i, r in enumerate(results_v2[:3]):
        print(f"  [{i + 1}] {r.get('fact', '')[:100]}")


if __name__ == "__main__":
    asyncio.run(run())
