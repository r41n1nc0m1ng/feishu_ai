"""
批量查询脚本：读取 query_cases.json，按正常查询流程逐条执行并打印结果。

启动时默认重算 embedding（因为 embedding 文本格式已变更：现在含 reason / PROGRESS 字段），
旧 SQLite 中的 embedding 与新混合检索算法不匹配。

运行：
    conda run -n feishu python benchmark/interactive_query.py
    conda run -n feishu python benchmark/interactive_query.py --cases benchmark/query_cases.json
    conda run -n feishu python benchmark/interactive_query.py --no-rebuild   # 跳过 embedding 重算
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

# 模块导入时 _restore_cache() 自动从 SQLite 恢复卡片和 embedding 缓存
from memory.card_generator import _card_cache, _cache_card_embedding
from memory.retriever import MemoryRetriever
from memory.schemas import FeishuMessage
from realtime.query_handler import RealtimeQueryHandler

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logging.getLogger("memory.retriever").setLevel(logging.INFO)   # 显示混合检索的 top3 打分

DEFAULT_CASES = Path(__file__).with_name("query_cases.json")


async def _rebuild_embeddings(chat_id: str) -> int:
    """对指定 chat 的所有 active 卡片重新计算 embedding，覆盖 SQLite 中的旧值。"""
    cards = [c for c in _card_cache.values() if c.chat_id == chat_id]
    print(f"重算 embedding ({len(cards)} 张卡片)...", flush=True)
    for card in cards:
        await _cache_card_embedding(card)
    return len(cards)


async def main(cases_path: Path, rebuild: bool) -> None:
    data = json.loads(cases_path.read_text(encoding="utf-8"))
    chat_id = data.get("chat_id", "oc_demo_ai_resume")
    queries = data.get("queries", [])

    print(f"加载 {len(queries)} 条查询  chat_id={chat_id}", flush=True)

    if rebuild:
        n = await _rebuild_embeddings(chat_id)
        print(f"  embedding 重算完成（{n} 张）\n", flush=True)
    else:
        print("  跳过 embedding 重算（--no-rebuild）\n", flush=True)

    retriever = MemoryRetriever()

    # send_text 与生产一致：直接输出，不做拦截
    async def send_text(cid: str, text: str) -> None:
        print(text)

    handler = RealtimeQueryHandler(retriever=retriever, send_text=send_text)

    for q in queries:
        qid  = q.get("id", "")
        text = q.get("text", "").strip()
        note = q.get("note", "")
        if not text:
            continue

        msg = FeishuMessage(
            message_id=f"iq_{qid}",
            sender_id="ou_evaluator",
            chat_id=chat_id,
            chat_type="group",
            text=f"@机器人 {text}",
            timestamp=datetime.now(tz=timezone.utc),
            is_at_bot=True,
        )

        print(f"{'─'*60}")
        print(f"[{qid}] {text}")
        if note:
            print(f"  提示: {note}")

        try:
            trace = await handler.handle_query_message(msg)
            print(f"  触发: {getattr(trace, 'action', '?')}")
        except Exception as e:
            print(f"  [ERROR] {e}")

        print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", default=str(DEFAULT_CASES))
    parser.add_argument("--no-rebuild", action="store_true",
                        help="跳过 embedding 重算（仅在确认 SQLite embedding 已是新格式时使用）")
    args = parser.parse_args()
    asyncio.run(main(Path(args.cases), rebuild=not args.no_rebuild))
