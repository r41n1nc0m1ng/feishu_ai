"""
批量查询脚本：读取 query_cases.json，按正常查询流程逐条执行并打印结果。

运行：
    conda run -n feishu python benchmark/interactive_query.py
    conda run -n feishu python benchmark/interactive_query.py --cases benchmark/query_cases.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from memory.graphiti_client import GraphitiClient
from memory.retriever import MemoryRetriever
from memory.schemas import FeishuMessage
from realtime.query_handler import RealtimeQueryHandler

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

DEFAULT_CASES = Path(__file__).with_name("query_cases.json")


async def main(cases_path: Path) -> None:
    data = json.loads(cases_path.read_text(encoding="utf-8"))
    chat_id = data.get("chat_id", "oc_demo_ai_resume")
    queries = data.get("queries", [])

    print(f"加载 {len(queries)} 条查询  chat_id={chat_id}", flush=True)
    print("初始化 Graphiti...", flush=True)
    await GraphitiClient.initialize()
    print("初始化完成\n", flush=True)

    retriever = MemoryRetriever()
    replies: dict[str, str] = {}

    async def collect_reply(cid: str, text: str) -> None:
        replies["_current"] = replies.get("_current", "") + text + "\n"

    handler = RealtimeQueryHandler(retriever=retriever, send_text=collect_reply)

    for q in queries:
        qid  = q.get("id", "")
        text = q.get("text", "").strip()
        note = q.get("note", "")
        if not text:
            continue

        replies["_current"] = ""
        msg = FeishuMessage(
            message_id=f"iq_{qid}",
            sender_id="ou_evaluator",
            chat_id=chat_id,
            chat_type="group",
            text=f"@机器人 {text}",
            timestamp=datetime.now(tz=timezone.utc),
            is_at_bot=True,
        )

        try:
            trace = await handler.handle_query_message(msg)
            action = getattr(trace, "action", "?")
        except Exception as e:
            action = "ERROR"
            replies["_current"] = str(e)

        reply = replies.get("_current", "").strip()

        print(f"{'─'*60}")
        print(f"[{qid}] {text}")
        if note:
            print(f"  提示: {note}")
        print(f"  触发: {action}")
        print(f"  回复: {reply}")
        print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", default=str(DEFAULT_CASES))
    args = parser.parse_args()
    asyncio.run(main(Path(args.cases)))
