"""诊断脚本：检查 SQLite 和 Neo4j 中的记忆数据。"""
import json, os, sys, asyncio
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

import sqlite3
conn = sqlite3.connect(ROOT / "memory_store.db")

# ── SQLite ──────────────────────────────────────────────────────────────────���─
print("=" * 60)
print("SQLite memory_cards")
print("=" * 60)
rows = conn.execute(
    "SELECT data FROM memory_cards ORDER BY created_at DESC"
).fetchall()
print(f"总计 {len(rows)} 张卡\n")

status_counts = {}
for r in rows:
    d = json.loads(r[0])
    status = d.get("status", "?")
    mtype  = d.get("memory_type", "?")
    key    = f"[{status}][{mtype}]"
    status_counts[key] = status_counts.get(key, 0) + 1
    print(f"{key}  obj={d.get('decision_object','')[:30]}")
    print(f"       title={d.get('title','')[:50]}")
    print(f"       decision={d.get('decision','')[:80]}")

print()
for k, v in sorted(status_counts.items()):
    print(f"  {k}: {v}")

print()
print("=" * 60)
print("SQLite evidence_blocks")
print("=" * 60)
eb_rows = conn.execute(
    "SELECT data FROM evidence_blocks ORDER BY created_at DESC"
).fetchall()
print(f"总计 {len(eb_rows)} 个 EvidenceBlock\n")
for r in eb_rows:
    d = json.loads(r[0])
    msgs = d.get("messages", [])
    print(f"  block={d.get('block_id','')[:8]}  msgs={len(msgs)}"
          f"  start={d.get('start_time','')[:16]}")
    for m in msgs[:3]:
        print(f"    [{m.get('sender_name','?')}] {m.get('text','')[:60]}")
    if len(msgs) > 3:
        print(f"    ...（共 {len(msgs)} 条）")

print()
print("=" * 60)
print("SQLite topic_summaries")
print("=" * 60)
ts_rows = conn.execute(
    "SELECT data FROM topic_summaries ORDER BY updated_at DESC"
).fetchall()
print(f"总计 {len(ts_rows)} 条 TopicSummary\n")
for r in ts_rows:
    d = json.loads(r[0])
    print(f"  topic={d.get('topic','')}  covered={len(d.get('covered_memory_ids',[]))}")
    print(f"  summary={d.get('summary','')[:80]}")

conn.close()

# ── Neo4j ─────────────────────────────────────────────────────────────────────
print()
print("=" * 60)
print("Neo4j 节点 / 关系统计")
print("=" * 60)

async def check_neo4j():
    try:
        from neo4j import AsyncGraphDatabase
        uri  = os.getenv("NEO4J_URI", "bolt://localhost:7687")
        user = os.getenv("NEO4J_USER", "neo4j")
        pw   = os.getenv("NEO4J_PASSWORD", "password")
        async with AsyncGraphDatabase.driver(uri, auth=(user, pw)) as drv:
            async with drv.session() as s:
                # 节点按标签统计
                r = await s.run("MATCH (n) RETURN labels(n) AS lbl, count(*) AS cnt ORDER BY cnt DESC")
                records = await r.data()
                print("节点按标签：")
                for rec in records:
                    print(f"  {rec['lbl']}: {rec['cnt']}")

                # Episode 节点采样（Graphiti 存储记忆体的地方）
                r2 = await s.run(
                    "MATCH (e:Episode) RETURN e.name AS name, e.content AS content "
                    "ORDER BY e.created_at DESC LIMIT 20"
                )
                eps = await r2.data()
                print(f"\nEpisode 节点（最近 {len(eps)} 条）：")
                for ep in eps:
                    name = (ep.get("name") or "")[:50]
                    content = (ep.get("content") or "")[:80]
                    print(f"  [{name}]")
                    print(f"    {content}")

                # 关系统计
                r3 = await s.run("MATCH ()-[r]->() RETURN type(r) AS t, count(*) AS cnt ORDER BY cnt DESC")
                rels = await r3.data()
                print("\n关系按类型：")
                for rel in rels:
                    print(f"  {rel['t']}: {rel['cnt']}")

    except Exception as e:
        print(f"Neo4j 连接失败（跳过）: {e}")

asyncio.run(check_neo4j())
