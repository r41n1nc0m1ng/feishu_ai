"""
本地链路测试脚本（不依赖 HTTP/tunnel）。
直接调用 handler 函数，验证：
  消息解析 → Zep session → Ollama 抽取 → Graphiti 写入 → 飞书回复
运行前确保 Ollama 和 Neo4j 已启动。
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

CHAT_ID = "oc_test_group_demo"

CONVERSATION = [
    ("user_A", "我觉得我们不应该做企业级记忆，太复杂了。"),
    ("user_B", "同意，群聊边界更自然，Demo 也更好讲。"),
    ("user_A", "那就定了，专注群聊决策记忆，不做企业级。"),
]


def make_raw_event(sender_id: str, text: str, idx: int) -> dict:
    ts = str(int(time.time() * 1000))
    return {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": sender_id}},
            "message": {
                "message_id": f"om_test_{idx}_{ts}",
                "create_time": ts,
                "chat_id": CHAT_ID,
                "chat_type": "group",
                "message_type": "text",
                "content": json.dumps({"text": text}),
            },
        },
    }


async def run():
    print("\n=== 初始化 Graphiti ===")
    try:
        await GraphitiClient.initialize()
        print("✅ Graphiti 已连接")
    except Exception as e:
        print(f"⚠️  Graphiti 初始化失败（继续测试，写入步骤会跳过）: {e}")

    print(f"\n=== 发送 {len(CONVERSATION)} 条模拟消息 ===")
    for i, (sender, text) in enumerate(CONVERSATION):
        print(f"\n[{sender}] {text}")
        await handle_raw_event(make_raw_event(sender, text, i))
        await asyncio.sleep(1)

    print("\n=== 测试完成 ===")
    print("查看上方日志：")
    print("  'Graphiti episode added' → 记忆写入成功")
    print("  'No memory value detected' → Ollama 判断无记忆价值（可换消息重试）")
    print("  'Feishu send_text failed' → 飞书回复失败（测试用假 chat_id，正常）")


if __name__ == "__main__":
    asyncio.run(run())
