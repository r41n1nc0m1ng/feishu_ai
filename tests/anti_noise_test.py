"""P0 Benchmark — Anti-Noise Test

Seeds one decision, injects NOISE_COUNT irrelevant messages, then queries.
Pass criteria: original decision is still in top-3 results after all the noise.
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

CHAT_ID = "oc_benchmark_noise"
NOISE_COUNT = 50

NOISE_POOL = [
    "哈哈哈", "收到", "好的", "明白了", "稍等", "我看看",
    "周五能来吗", "明天开会几点", "代码提交了吗", "UI改一下",
    "帮我看看这个bug", "文档更新了", "测试环境挂了", "先去吃饭",
    "这个接口加个参数", "前端那边说改布局", "数据库备份好了吗",
]

DECISION_TEXT = "确定了，我们不做企业级记忆，专注群聊决策记忆。"
QUERY = "为什么不做企业级记忆"
EXPECTED_KEYWORD = "群聊"


def _make_event(text: str, idx: int, sender: str = "user_0") -> dict:
    ts = str(int(time.time() * 1000))
    return {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": sender}},
            "message": {
                "message_id": f"om_noise_{idx}_{ts}",
                "create_time": ts,
                "chat_id": CHAT_ID,
                "chat_type": "group",
                "message_type": "text",
                "content": json.dumps({"text": text}),
            },
        },
    }


async def run():
    print("\n=== Anti-Noise Test ===")
    await GraphitiClient.initialize()

    print(f"Seeding decision: {DECISION_TEXT}")
    await handle_raw_event(_make_event(DECISION_TEXT, 0))
    await asyncio.sleep(2)

    print(f"Injecting {NOISE_COUNT} noise messages...")
    for i in range(NOISE_COUNT):
        text = NOISE_POOL[i % len(NOISE_POOL)]
        await handle_raw_event(_make_event(text, i + 1, sender=f"noise_user_{i % 4}"))
    await asyncio.sleep(2)

    print(f"Querying: '{QUERY}'")
    retriever = MemoryRetriever()
    results = await retriever.search(CHAT_ID, QUERY, limit=3)

    matched = any(EXPECTED_KEYWORD in r.get("fact", "") for r in results)
    print(f"\n  {'PASS' if matched else 'FAIL'} — decision recalled after {NOISE_COUNT} noise messages")
    for i, r in enumerate(results[:3]):
        print(f"  [{i + 1}] {r.get('fact', '')[:100]}")


if __name__ == "__main__":
    asyncio.run(run())
