"""P0 Benchmark — Recall Accuracy Test

Measures whether the system surfaces the correct decision and reason
given a natural-language query, simulating a user asking "why did we
decide X?"

Pass criteria: expected_keyword appears in top-3 results within TIMEOUT_S seconds.
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

CHAT_ID = "oc_benchmark_recall"
TIMEOUT_S = 5.0

SEED_EPISODES = [
    "团队决定MVP阶段专注群聊决策记忆，不做企业级记忆。原因：企业级记忆涉及跨群权限和个人文档，Demo难以聚焦。",
    "确定了，按群聊存储决策，一个群就是一个记忆空间。",
    "那我们接下来讨论 Benchmark 怎么设计。",
]

TEST_CASES = [
    {"query": "为什么不做企业级记忆", "expected_keyword": "群聊"},
    {"query": "记忆边界怎么定的", "expected_keyword": "群"},
]


def _make_event(text: str, idx: int) -> dict:
    ts = str(int(time.time() * 1000))
    return {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": f"recall_user_{idx % 3}"}},
            "message": {
                "message_id": f"om_recall_{idx}_{ts}",
                "create_time": ts,
                "chat_id": CHAT_ID,
                "chat_type": "group",
                "message_type": "text",
                "content": json.dumps({"text": text}),
            },
        },
    }


async def run():
    print("\n=== Recall Accuracy Test ===")
    await GraphitiClient.initialize()

    print("Seeding episodes...")
    for i, text in enumerate(SEED_EPISODES):
        await handle_raw_event(_make_event(text, i))
        await asyncio.sleep(1)
    await asyncio.sleep(2)

    retriever = MemoryRetriever()
    passed = 0
    for case in TEST_CASES:
        start = time.perf_counter()
        results = await retriever.search(CHAT_ID, case["query"], limit=3)
        elapsed = time.perf_counter() - start

        matched = any(case["expected_keyword"] in r.get("fact", "") for r in results)
        ok = matched and elapsed <= TIMEOUT_S
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] '{case['query']}' | {elapsed:.2f}s | matched={matched}")
        if ok:
            passed += 1

    print(f"\nResult: {passed}/{len(TEST_CASES)} passed")


if __name__ == "__main__":
    asyncio.run(run())
