"""清除 SQLite 和 Neo4j 中的全部数据，重置为干净测试状态。"""
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from memory.store import DB_PATH, _conn


def clear_sqlite():
    tables = [
        "evidence_blocks",
        "memory_cards",
        "memory_relations",
        "topic_summaries",
        "chat_spaces",
    ]
    with _conn() as conn:
        for table in tables:
            row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            count = row[0]
            conn.execute(f"DELETE FROM {table}")
            print(f"  {table}: {count} 条记录已删除")
    print(f"SQLite 完成 | 文件: {DB_PATH}")


async def clear_neo4j():
    from neo4j import AsyncGraphDatabase

    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    user = os.getenv("NEO4J_USER", "neo4j")
    password = os.getenv("NEO4J_PASSWORD", "password")

    async with AsyncGraphDatabase.driver(uri, auth=(user, password)) as driver:
        async with driver.session() as session:
            result = await session.run("MATCH (n) DETACH DELETE n")
            summary = await result.consume()
            nodes = summary.counters.nodes_deleted
            rels = summary.counters.relationships_deleted
            print(f"Neo4j 完成 | 删除 {nodes} 个节点、{rels} 条关系")


async def main():
    print("=== 清除测试数据 ===\n")

    print("[1] 清除 SQLite ...")
    clear_sqlite()

    print("\n[2] 清除 Neo4j ...")
    try:
        await clear_neo4j()
    except Exception as e:
        print(f"  Neo4j 连接失败（跳过）: {e}")

    print("\n完成。")


if __name__ == "__main__":
    asyncio.run(main())
