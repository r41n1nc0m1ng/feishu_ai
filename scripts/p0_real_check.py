"""
P0 real-environment smoke check.

This script validates runtime dependencies that unit tests intentionally mock:
- .env and Feishu credentials
- LLM provider: Ollama or OpenAI-compatible cloud API
- Neo4j connectivity
- Graphiti initialization
- EvidenceBlock -> MemoryCard generation -> source expansion

It writes one smoke EvidenceBlock/MemoryCard into the configured local SQLite
store and Graphiti group. Run only in a test Feishu/Neo4j environment.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from neo4j import GraphDatabase

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from feishu.api_client import FeishuAPIClient
from memory.card_generator import CardGenerator
from memory.evidence_store import EvidenceStore
from memory.graphiti_client import GraphitiClient
from memory.retriever import MemoryRetriever
from memory.schemas import EvidenceBlock, EvidenceMessage
from realtime.query_handler import render_evidence_reply


def _ok(label: str) -> None:
    print(f"OK   {label}")


def _fail(label: str, detail: str = "") -> None:
    print(f"FAIL {label}{': ' + detail if detail else ''}")
    raise SystemExit(1)


def _require_env(names: list[str]) -> None:
    missing = [name for name in names if not os.getenv(name)]
    if missing:
        _fail("required env", ", ".join(missing))
    _ok("required env")


async def _check_llm() -> None:
    provider = os.getenv("MODEL_PROVIDER", "ollama").strip().lower()
    if provider == "openai" or os.getenv("OPENAI_API_KEY"):
        _require_env(["OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_EMBED_MODEL"])
        base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}"},
                json={
                    "model": os.getenv("OPENAI_MODEL"),
                    "messages": [{"role": "user", "content": "Return JSON: {\"ok\": true}"}],
                    "response_format": {"type": "json_object"},
                },
            )
            resp.raise_for_status()
        _ok("openai-compatible chat")
        return

    ollama_url = os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/")
    async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
        resp = await client.get(f"{ollama_url}/api/tags")
        resp.raise_for_status()
    _ok("ollama")


def _check_neo4j() -> None:
    _require_env(["NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD"])
    driver = GraphDatabase.driver(
        os.getenv("NEO4J_URI"),
        auth=(os.getenv("NEO4J_USER"), os.getenv("NEO4J_PASSWORD")),
    )
    try:
        with driver.session() as session:
            session.run("RETURN 1").single()
    finally:
        driver.close()
    _ok("neo4j")


async def _check_feishu() -> None:
    _require_env(["FEISHU_APP_ID", "FEISHU_APP_SECRET"])
    client = FeishuAPIClient()
    token = await client._get_token()
    if not token:
        _fail("feishu token", "empty token")
    bot_open_id = os.getenv("FEISHU_BOT_OPEN_ID") or await client.get_bot_open_id()
    if not bot_open_id:
        _fail("feishu bot open_id", "empty open_id")
    print(f"OK   feishu bot open_id={bot_open_id}")


async def _check_memory_pipeline() -> None:
    await GraphitiClient.initialize()
    _ok("graphiti initialize")

    now = datetime.now(timezone.utc)
    chat_id = os.getenv("P0_SMOKE_CHAT_ID", f"p0_smoke_{int(now.timestamp())}")
    block = EvidenceBlock(
        chat_id=chat_id,
        start_time=now,
        end_time=now,
        messages=[
            EvidenceMessage(
                message_id="p0_smoke_1",
                sender_id="smoke_user_a",
                sender_name="烟测A",
                timestamp=now,
                text="我们决定 P0 阶段先聚焦群聊决策记忆，不做企业级记忆。",
            ),
            EvidenceMessage(
                message_id="p0_smoke_2",
                sender_id="smoke_user_b",
                sender_name="烟测B",
                timestamp=now,
                text="同意，企业级权限复杂，群聊边界更适合 Demo。",
            ),
        ],
    )

    await EvidenceStore().save(block)
    _ok("evidence save")

    card = await CardGenerator().generate(block)
    if not card:
        _fail("memory card generate", "LLM returned NOOP or failed")
    print(f"OK   memory card title={card.title}")

    expanded = await MemoryRetriever().expand_evidence(card.source_block_ids[0])
    if not expanded:
        _fail("expand evidence", card.source_block_ids[0])
    _ok("expand evidence")
    print(render_evidence_reply("原话在哪", card, expanded))


async def main() -> None:
    load_dotenv()
    try:
        await _check_llm()
        _check_neo4j()
        await _check_feishu()
        await _check_memory_pipeline()
    except Exception as exc:
        _fail("p0 real check", repr(exc))


if __name__ == "__main__":
    if not os.path.exists(".env"):
        _fail(".env", "missing; copy .env.example and fill real credentials")
    asyncio.run(main())
