from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from benchmark.input_simulator import CaseLoader
from benchmark.replay_adapter import DualChannelReplayAdapter
from memory import store
from memory.batch_processor import BatchProcessor
from memory.evidence_store import EvidenceStore
from memory.retriever import MemoryRetriever
from memory.schemas import EvidenceBlock, FeishuMessage, MemoryCard, MemoryRelation, TopicSummary
from realtime.action_handler import RealtimeActionHandler
from realtime.dispatcher import dispatch_message
from realtime.query_handler import RealtimeQueryHandler

logger = logging.getLogger(__name__)


def _env_bool(name: str) -> bool | None:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return None
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def install_llm_thinking_patch() -> None:
    enable_thinking = _env_bool("LLM_ENABLE_THINKING")
    if enable_thinking is None:
        return

    import httpx

    original_post = httpx.AsyncClient.post
    if getattr(original_post, "_demo_trace_thinking_patch", False):
        return

    async def patched_post(self, url: str, *args: Any, **kwargs: Any):
        payload = kwargs.get("json")
        if isinstance(payload, dict) and str(url).rstrip("/").endswith("/chat/completions"):
            payload = dict(payload)
            payload.setdefault("enable_thinking", enable_thinking)
            kwargs["json"] = payload
        return await original_post(self, url, *args, **kwargs)

    setattr(patched_post, "_demo_trace_thinking_patch", True)
    httpx.AsyncClient.post = patched_post


def install_openai_embedding_patch() -> None:
    provider = os.getenv("MODEL_PROVIDER", "").strip().lower()
    if provider != "openai" and not os.getenv("OPENAI_API_KEY"):
        return

    import httpx
    import numpy as np
    import memory.card_generator as card_generator

    if getattr(card_generator._get_embedding, "_demo_trace_openai_embedding_patch", False):
        return

    async def patched_get_embedding(text: str):
        api_key = os.getenv("OPENAI_API_KEY", "")
        base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        model = os.getenv("OPENAI_EMBED_MODEL", os.getenv("EMBED_MODEL", "text-embedding-3-small"))
        if not api_key:
            logger.warning("demo trace embedding patch skipped: OPENAI_API_KEY is empty")
            return None
        try:
            async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
                resp = await client.post(
                    f"{base_url}/embeddings",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={"model": model, "input": text},
                )
                resp.raise_for_status()
                data = resp.json()
                vec = (data.get("data") or [{}])[0].get("embedding") or []
                if not vec:
                    return None
                arr = np.array(vec, dtype=np.float32)
                norm = np.linalg.norm(arr)
                return arr / norm if norm > 0 else arr
        except Exception as exc:
            logger.warning("demo trace OpenAI-compatible embedding failed: %s", exc)
            return None

    setattr(patched_get_embedding, "_demo_trace_openai_embedding_patch", True)
    card_generator._get_embedding = patched_get_embedding


def install_skip_graphiti_card_write_patch() -> None:
    import memory.card_generator as card_generator

    original_write = card_generator._write_card_to_graphiti
    if getattr(original_write, "_demo_trace_skip_graphiti_patch", False):
        return

    async def patched_write_card_to_graphiti(card, ref_time=None) -> None:
        logger.info(
            "Demo trace skipped Graphiti card write | memory_id=%s object=%s",
            getattr(card, "memory_id", ""),
            getattr(card, "decision_object", ""),
        )
        return None

    setattr(patched_write_card_to_graphiti, "_demo_trace_skip_graphiti_patch", True)
    card_generator._write_card_to_graphiti = patched_write_card_to_graphiti


@dataclass
class TraceSender:
    outputs: list[dict[str, Any]] = field(default_factory=list)

    async def send_text(self, chat_id: str, text: str) -> None:
        self.outputs.append({"kind": "text", "chat_id": chat_id, "text": text})

    async def send_card(self, chat_id: str, card: dict[str, Any]) -> None:
        header = card.get("header") or {}
        title = header.get("title") or {}
        self.outputs.append({
            "kind": "card",
            "chat_id": chat_id,
            "title": title.get("content", ""),
            "card": card,
        })

    def flush(self) -> list[dict[str, Any]]:
        outputs = list(self.outputs)
        self.outputs.clear()
        return outputs


def _now_label() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _as_jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _message_text(loader: CaseLoader, raw_msg: dict[str, Any]) -> str:
    return loader.message_text(raw_msg).replace("\n", " ").strip()


def _short(text: str, limit: int = 160) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "..."


def _card_key(card: MemoryCard) -> tuple[Any, ...]:
    return (
        card.decision_object,
        card.decision,
        card.reason,
        card.memory_type.value,
        card.status.value,
        tuple(card.source_block_ids),
        tuple(card.tentative_consensus),
        tuple(card.open_questions),
        tuple(card.discussion_scope),
        card.next_step,
        card.supersedes_memory_id,
    )


def _card_to_report(card: MemoryCard) -> dict[str, Any]:
    return {
        "memory_id": card.memory_id,
        "decision_object": card.decision_object,
        "decision_object_key": card.decision_object_key,
        "decision": card.decision,
        "reason": card.reason,
        "memory_type": card.memory_type.value,
        "status": card.status.value,
        "source_block_ids": card.source_block_ids,
        "source_message_ids": [],
        "tentative_consensus": card.tentative_consensus,
        "open_questions": card.open_questions,
        "discussion_scope": card.discussion_scope,
        "next_step": card.next_step,
        "supersedes_memory_id": card.supersedes_memory_id,
    }


async def _card_to_report_async(card: MemoryCard, es: EvidenceStore) -> dict[str, Any]:
    entry = _card_to_report(card)
    entry["source_message_ids"] = await card_source_message_ids(card, es)
    return entry


async def card_source_message_ids(card: MemoryCard, es: EvidenceStore) -> list[str]:
    ids: list[str] = []
    for block_id in card.source_block_ids:
        block = await es.get(block_id)
        if block:
            ids.extend(message.message_id for message in block.messages)
    return ids


def _block_to_report(block: EvidenceBlock) -> dict[str, Any]:
    return {
        "block_id": block.block_id,
        "chat_id": block.chat_id,
        "start_time": block.start_time.isoformat(),
        "end_time": block.end_time.isoformat(),
        "topic": block.topic,
        "block_type": block.block_type,
        "boundary_signal": block.boundary_signal,
        "one_line_summary": block.one_line_summary,
        "messages": [
            {
                "message_id": message.message_id,
                "sender_name": message.sender_name,
                "timestamp": message.timestamp.isoformat(),
                "text": message.text,
            }
            for message in block.messages
        ],
    }


def _relation_to_report(relation: MemoryRelation, cards: dict[str, MemoryCard]) -> dict[str, Any]:
    source = cards.get(relation.source_id)
    target = cards.get(relation.target_id)
    return {
        "relation_id": relation.relation_id,
        "relation_type": relation.relation_type.value,
        "source_id": relation.source_id,
        "source_decision": source.decision if source else "",
        "target_id": relation.target_id,
        "target_decision": target.decision if target else "",
    }


def _topic_to_report(topic: TopicSummary) -> dict[str, Any]:
    return {
        "summary_id": topic.summary_id,
        "topic": topic.topic,
        "summary": topic.summary,
        "covered_memory_ids": topic.covered_memory_ids,
    }


def _raw_message_to_report(loader: CaseLoader, raw_msg: dict[str, Any], adapter: DualChannelReplayAdapter) -> dict[str, Any]:
    return {
        "message_id": adapter.message_id(raw_msg),
        "sender_id": adapter.sender_id(raw_msg),
        "sender_name": adapter.sender_name(raw_msg),
        "sender_type": adapter.sender_type(raw_msg),
        "create_time": str(adapter.raw_timestamp(raw_msg)),
        "is_at_bot": loader.is_at_bot(raw_msg) or adapter.is_at_bot(raw_msg, adapter.mentions(raw_msg)),
        "text": _message_text(loader, raw_msg),
    }


def _make_at_bot_message(chat_id: str, query: str, message_id: str) -> FeishuMessage:
    return FeishuMessage(
        message_id=message_id,
        sender_id="ou_demo_trace",
        chat_id=chat_id,
        chat_type="group",
        text=f"@机器人 {query}",
        timestamp=datetime.now(tz=timezone.utc),
        is_at_bot=True,
    )


async def _run_query(
    *,
    chat_id: str,
    query: str,
    message_id: str,
    retriever: MemoryRetriever,
    es: EvidenceStore,
) -> dict[str, Any]:
    sender = TraceSender()
    query_handler = RealtimeQueryHandler(retriever=retriever, send_text=sender.send_text)
    msg = _make_at_bot_message(chat_id, query, message_id)
    action = "query"
    try:
        trace = await query_handler.handle_query_message(msg)
        action = getattr(trace, "action", "query")
    except Exception as exc:
        logger.exception("final query failed | query=%s", query)
        sender.outputs.append({"kind": "error", "chat_id": chat_id, "text": str(exc)})

    source_message_ids: list[str] = []
    retrieved_cards: list[dict[str, Any]] = []
    try:
        results = await retriever.retrieve(chat_id, query, limit=3)
        for card in results:
            retrieved_cards.append(await _card_to_report_async(card, es))
        if results:
            for block_id in results[0].source_block_ids:
                block = await es.get(block_id)
                if block:
                    source_message_ids = [message.message_id for message in block.messages]
                    break
    except Exception:
        logger.exception("retriever diagnostic failed | query=%s", query)

    outputs = sender.flush()
    return {
        "query": query,
        "action": action,
        "outputs": outputs,
        "actual_reply": "\n".join(output.get("text", "") for output in outputs if output.get("kind") == "text"),
        "source_message_ids": source_message_ids,
        "retrieved_cards": retrieved_cards,
    }


def _render_outputs(outputs: list[dict[str, Any]]) -> str:
    if not outputs:
        return "_无输出_"
    chunks: list[str] = []
    for output in outputs:
        if output.get("kind") == "text":
            chunks.append(output.get("text", ""))
        elif output.get("kind") == "card":
            chunks.append(f"[CARD] {output.get('title') or json.dumps(output.get('card'), ensure_ascii=False)}")
        else:
            chunks.append(json.dumps(output, ensure_ascii=False, indent=2))
    return "\n\n".join(chunks)


def _render_batch_markdown(batch_report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"# {batch_report['batch_id']}")
    lines.append("")
    lines.append("## 1. Realtime input")
    for message in batch_report["realtime_input"]:
        marker = " @bot" if message["is_at_bot"] else ""
        lines.append(f"- `{message['message_id']}`{marker} {message['sender_name'] or message['sender_id']}: {message['text']}")
    lines.append("")
    lines.append("## 2. Realtime outputs")
    for item in batch_report["realtime_results"]:
        lines.append(f"### `{item['message_id']}` action={item.get('action', '')}")
        lines.append(f"> {item['text']}")
        lines.append("")
        lines.append(_render_outputs(item["outputs"]))
        lines.append("")
    if not batch_report["realtime_results"]:
        lines.append("_本批实时侧没有返回给前端/飞书的内容。_")
        lines.append("")

    lines.append("## 3. Write input")
    lines.append(f"- loader.write_messages: {len(batch_report['write_input'])}")
    lines.append(f"- adapter.to_fetch_batch.messages: {len(batch_report['fetch_batch_messages'])}")
    lines.append("")
    lines.append("### Messages entering FetchBatch")
    for message in batch_report["fetch_batch_messages"]:
        lines.append(f"- `{message['message_id']}` {message['sender_name'] or message['sender_id']}: {message['text']}")
    if not batch_report["fetch_batch_messages"]:
        lines.append("_本批没有进入写入侧的消息。_")
    lines.append("")

    lines.append("## 4. EvidenceBlocks")
    for block in batch_report["evidence_blocks"]:
        lines.append(f"### `{block['block_id']}` {block.get('block_type') or ''} | {block.get('topic') or ''}")
        if block.get("one_line_summary"):
            lines.append(f"- summary: {block['one_line_summary']}")
        if block.get("boundary_signal"):
            lines.append(f"- boundary: {block['boundary_signal']}")
        for message in block["messages"]:
            lines.append(f"- `{message['message_id']}` {message['sender_name']}: {message['text']}")
        lines.append("")
    if not batch_report["evidence_blocks"]:
        lines.append("_本批没有生成 EvidenceBlock。_")
        lines.append("")

    lines.append("## 5. MemoryCards changed in this batch")
    lines.append(f"- new: {len(batch_report['new_cards'])}")
    lines.append(f"- updated/status changed: {len(batch_report['updated_cards'])}")
    lines.append("")
    for card in batch_report["new_cards"] + batch_report["updated_cards"]:
        lines.append(f"### `{card['memory_id']}` {card['memory_type']}/{card['status']}")
        lines.append(f"- object: {card['decision_object']}")
        lines.append(f"- decision: {card['decision']}")
        lines.append(f"- reason: {card['reason']}")
        if card.get("open_questions"):
            lines.append(f"- open_questions: {', '.join(card['open_questions'])}")
        lines.append(f"- source_blocks: {', '.join(card['source_block_ids'])}")
        lines.append(f"- source_messages: {', '.join(card['source_message_ids'])}")
        lines.append("")
    if not batch_report["new_cards"] and not batch_report["updated_cards"]:
        lines.append("_本批没有新增或更新 MemoryCard。_")
        lines.append("")

    lines.append("## 6. Relations added")
    for relation in batch_report["new_relations"]:
        lines.append(f"- `{relation['relation_type']}` `{relation['source_id']}` -> `{relation['target_id']}`")
        lines.append(f"  - new: {_short(relation['source_decision'])}")
        lines.append(f"  - old: {_short(relation['target_decision'])}")
    if not batch_report["new_relations"]:
        lines.append("_本批没有新增关系。_")
    lines.append("")

    lines.append("## 7. TopicSummary after this batch")
    for topic in batch_report["topics_after"]:
        lines.append(f"### {topic['topic']}")
        lines.append(topic["summary"])
        lines.append(f"- covered: {', '.join(topic['covered_memory_ids'])}")
        lines.append("")
    if not batch_report["topics_after"]:
        lines.append("_当前没有 TopicSummary。_")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_final_queries_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = ["# Final query results", ""]
    for section_name, title in (
        ("final_memory_checks", "Final Memory Checks"),
        ("evidence_checks", "Evidence Checks"),
    ):
        checks = report.get(section_name, [])
        lines.append(f"## {title}")
        if not checks:
            lines.append("_无_")
            lines.append("")
            continue
        for index, item in enumerate(checks, 1):
            lines.append(f"### {index}. {item['query']}")
            lines.append(f"- action: {item.get('action', '')}")
            if item.get("expected_granularity"):
                lines.append(f"- expected_granularity: {item['expected_granularity']}")
            if item.get("expected_source_message_ids"):
                lines.append(f"- expected_source_message_ids: {', '.join(item['expected_source_message_ids'])}")
            if item.get("source_message_ids"):
                lines.append(f"- actual_source_message_ids: {', '.join(item['source_message_ids'])}")
            if item.get("expected_keywords"):
                lines.append(f"- expected_keywords: {', '.join(item['expected_keywords'])}")
            if item.get("forbidden_keywords"):
                lines.append(f"- forbidden_keywords: {', '.join(item['forbidden_keywords'])}")
            lines.append("")
            lines.append("Actual reply:")
            lines.append("")
            lines.append(item.get("actual_reply") or _render_outputs(item.get("outputs", [])))
            lines.append("")
            if item.get("retrieved_cards"):
                lines.append("Retrieved cards:")
                for card in item["retrieved_cards"]:
                    lines.append(f"- `{card['memory_id']}` {card['memory_type']}/{card['status']} | {card['decision_object']} | {card['decision']}")
                lines.append("")

    relation_checks = report.get("relation_checks", [])
    lines.append("## Relation Checks")
    if not relation_checks:
        lines.append("_无_")
    for index, item in enumerate(relation_checks, 1):
        lines.append(f"### {index}. {item.get('relation_type', '')}")
        lines.append(f"- found: {item.get('found')}")
        if item.get("old_expected_keywords"):
            lines.append(f"- old_expected_keywords: {', '.join(item['old_expected_keywords'])}")
        if item.get("new_expected_keywords"):
            lines.append(f"- new_expected_keywords: {', '.join(item['new_expected_keywords'])}")
        if item.get("old_card"):
            lines.append(f"- old_card: `{item['old_card']['memory_id']}` {item['old_card']['decision']}")
        if item.get("new_card"):
            lines.append(f"- new_card: `{item['new_card']['memory_id']}` {item['new_card']['decision']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


async def run_relation_checks(expected: dict[str, Any], chat_id: str) -> list[dict[str, Any]]:
    cards_map = {card.memory_id: card for card in store.load_all_memory_cards()}
    relations = store.load_relations_by_chat(chat_id)
    results: list[dict[str, Any]] = []
    for check in expected.get("relation_checks", []):
        relation_type = check.get("relation_type", "")
        old_keywords = check.get("old_expected_keywords", [])
        new_keywords = check.get("new_expected_keywords", [])
        found: dict[str, Any] | None = None
        for relation in relations:
            if relation.relation_type.value.lower() != relation_type.lower():
                continue
            new_card = cards_map.get(relation.source_id)
            old_card = cards_map.get(relation.target_id)
            if not new_card or not old_card:
                continue
            new_text = f"{new_card.decision_object} {new_card.decision} {new_card.reason}"
            old_text = f"{old_card.decision_object} {old_card.decision} {old_card.reason}"
            if any(keyword in old_text for keyword in old_keywords) and any(keyword in new_text for keyword in new_keywords):
                found = {
                    "relation_type": relation_type,
                    "found": True,
                    "old_expected_keywords": old_keywords,
                    "new_expected_keywords": new_keywords,
                    "old_card": {"memory_id": old_card.memory_id, "decision": old_card.decision},
                    "new_card": {"memory_id": new_card.memory_id, "decision": new_card.decision},
                }
                break
        results.append(found or {
            "relation_type": relation_type,
            "found": False,
            "old_expected_keywords": old_keywords,
            "new_expected_keywords": new_keywords,
        })
    return results


async def run_final_checks(
    *,
    loader: CaseLoader,
    chat_id: str,
    retriever: MemoryRetriever,
    es: EvidenceStore,
) -> dict[str, Any]:
    expected = loader.expected
    final_memory_checks: list[dict[str, Any]] = []
    for index, check in enumerate(expected.get("final_memory_checks", [])):
        query = check.get("query", "")
        result = await _run_query(
            chat_id=chat_id,
            query=query,
            message_id=f"demo_final_memory_{index:02d}",
            retriever=retriever,
            es=es,
        )
        result.update({
            "expected_granularity": check.get("expected_granularity"),
            "expected_keywords": check.get("expected_keywords", []),
            "forbidden_keywords": check.get("forbidden_keywords", []),
        })
        final_memory_checks.append(result)

    evidence_checks: list[dict[str, Any]] = []
    for index, check in enumerate(expected.get("evidence_checks", [])):
        query = check.get("query", "")
        result = await _run_query(
            chat_id=chat_id,
            query=query,
            message_id=f"demo_evidence_{index:02d}",
            retriever=retriever,
            es=es,
        )
        result.update({
            "expected_source_message_ids": check.get("expected_source_message_ids", []),
            "expected_keywords": check.get("expected_keywords", []),
        })
        evidence_checks.append(result)

    return {
        "final_memory_checks": final_memory_checks,
        "relation_checks": await run_relation_checks(expected, chat_id),
        "evidence_checks": evidence_checks,
    }


async def process_batch_trace(
    *,
    batch: dict[str, Any],
    loader: CaseLoader,
    adapter: DualChannelReplayAdapter,
    bp: BatchProcessor,
    es: EvidenceStore,
    retriever: MemoryRetriever,
    chat_id: str,
) -> dict[str, Any]:
    batch_id = str(batch.get("batch_id", ""))
    realtime_messages = loader.realtime_messages(batch)
    write_messages = loader.write_messages(batch)

    sender = TraceSender()
    query_handler = RealtimeQueryHandler(retriever=retriever, send_text=sender.send_text)
    action_handler = RealtimeActionHandler(send_text=sender.send_text)
    realtime_results: list[dict[str, Any]] = []

    for raw_msg in realtime_messages:
        msg = adapter.to_realtime_message(raw_msg, chat_id)
        action = ""
        error = ""
        try:
            trace = await dispatch_message(msg, query_handler=query_handler, action_handler=action_handler)
            action = getattr(trace, "action", "")
        except Exception as exc:
            logger.exception("realtime dispatch failed | batch=%s msg=%s", batch_id, msg.message_id)
            error = str(exc)
        outputs = sender.flush()
        if outputs or error:
            realtime_results.append({
                "message_id": msg.message_id,
                "text": msg.text,
                "action": action,
                "error": error,
                "outputs": outputs,
            })

    cards_before = {card.memory_id: card for card in store.load_all_memory_cards()}
    card_keys_before = {memory_id: _card_key(card) for memory_id, card in cards_before.items()}
    relations_before = {relation.relation_id for relation in store.load_relations_by_chat(chat_id)}

    fetch_batch = adapter.to_fetch_batch(write_messages, chat_id) if write_messages else None
    blocks: list[EvidenceBlock] = []
    if fetch_batch and fetch_batch.messages:
        blocks = await bp.process_fetch_batch(fetch_batch)

    cards_after = {card.memory_id: card for card in store.load_all_memory_cards()}
    new_card_ids = [memory_id for memory_id in cards_after if memory_id not in cards_before]
    updated_card_ids = [
        memory_id
        for memory_id, card in cards_after.items()
        if memory_id in cards_before and _card_key(card) != card_keys_before[memory_id]
    ]
    relations_after = store.load_relations_by_chat(chat_id)
    new_relations = [relation for relation in relations_after if relation.relation_id not in relations_before]
    topics_after = store.load_topics_by_chat(chat_id)

    fetch_messages = [
        {
            "message_id": message.message_id,
            "sender_id": message.sender_id,
            "sender_name": message.sender_name,
            "timestamp": message.timestamp.isoformat(),
            "text": message.text,
        }
        for message in (fetch_batch.messages if fetch_batch else [])
    ]

    return {
        "batch_id": batch_id,
        "realtime_input": [_raw_message_to_report(loader, raw_msg, adapter) for raw_msg in realtime_messages],
        "realtime_results": realtime_results,
        "write_input": [_raw_message_to_report(loader, raw_msg, adapter) for raw_msg in write_messages],
        "fetch_batch_messages": fetch_messages,
        "evidence_blocks": [_block_to_report(block) for block in blocks],
        "new_cards": [await _card_to_report_async(cards_after[memory_id], es) for memory_id in new_card_ids],
        "updated_cards": [await _card_to_report_async(cards_after[memory_id], es) for memory_id in updated_card_ids],
        "new_relations": [_relation_to_report(relation, cards_after) for relation in new_relations],
        "topics_after": [_topic_to_report(topic) for topic in topics_after],
    }


async def reset_like_mock_main(chat_id: str) -> None:
    from memory.card_generator import clear_cache as clear_card_cache
    from memory.evidence_store import clear_cache as clear_block_cache
    from memory.graphiti_client import GraphitiClient

    store.clear_chat_data(chat_id)
    await GraphitiClient().clear_group(chat_id)
    clear_card_cache(chat_id)
    clear_block_cache(chat_id)


async def main_async(args: argparse.Namespace) -> int:
    install_llm_thinking_patch()
    install_openai_embedding_patch()
    if args.skip_graphiti_card_write:
        install_skip_graphiti_card_write_patch()
    fixture_path = Path(args.fixture).resolve()
    loader = CaseLoader(fixture_path)
    chat_id = loader.chat_id
    out_root = Path(args.out_dir).resolve()
    run_dir = out_root / f"{loader.case_id or fixture_path.stem}_{_now_label()}"
    run_dir.mkdir(parents=True, exist_ok=True)

    adapter = DualChannelReplayAdapter()
    bp = BatchProcessor()
    es = EvidenceStore()
    retriever = MemoryRetriever()

    from memory.graphiti_client import GraphitiClient

    print("Initializing Graphiti/Neo4j exactly like benchmark.mock_main...")
    await GraphitiClient.initialize()

    if not args.no_reset:
        print(f"Resetting SQLite + Neo4j data for chat_id={chat_id}...")
        await reset_like_mock_main(chat_id)
    else:
        print("Keeping existing SQLite + Neo4j data (--no-reset).")

    selected_batches = loader.batches
    if args.max_batches is not None:
        selected_batches = selected_batches[: max(args.max_batches, 0)]

    report: dict[str, Any] = {
        "case_id": loader.case_id,
        "fixture": str(fixture_path),
        "chat_id": chat_id,
        "reset": not args.no_reset,
        "chain": "mock_main: realtime dispatch_message per message, then adapter.to_fetch_batch + BatchProcessor.process_fetch_batch per fixture batch",
        "script_patches": {
            "llm_enable_thinking": _env_bool("LLM_ENABLE_THINKING"),
            "openai_embedding_patch": os.getenv("MODEL_PROVIDER", "").strip().lower() == "openai" or bool(os.getenv("OPENAI_API_KEY")),
            "skip_graphiti_card_write": args.skip_graphiti_card_write,
        },
        "run_dir": str(run_dir),
        "batches": [],
        "final_queries": {},
    }

    for index, batch in enumerate(selected_batches, 1):
        batch_id = str(batch.get("batch_id", f"batch_{index:03d}"))
        print(f"[{index}/{len(selected_batches)}] Processing {batch_id}...")
        batch_report = await process_batch_trace(
            batch=batch,
            loader=loader,
            adapter=adapter,
            bp=bp,
            es=es,
            retriever=retriever,
            chat_id=chat_id,
        )
        report["batches"].append(batch_report)
        _write_text(run_dir / f"{index:02d}_{batch_id}.md", _render_batch_markdown(batch_report))
        _write_text(run_dir / "trace.json", json.dumps(report, ensure_ascii=False, indent=2, default=_as_jsonable))

    print("Running final query checks through RealtimeQueryHandler...")
    report["final_queries"] = await run_final_checks(
        loader=loader,
        chat_id=chat_id,
        retriever=retriever,
        es=es,
    )
    _write_text(run_dir / "final_queries.md", _render_final_queries_markdown(report["final_queries"]))
    _write_text(run_dir / "trace.json", json.dumps(report, ensure_ascii=False, indent=2, default=_as_jsonable))

    index_lines = [
        f"# Demo trace: {loader.case_id}",
        "",
        f"- fixture: `{fixture_path}`",
        f"- chat_id: `{chat_id}`",
        f"- reset: `{not args.no_reset}`",
        f"- chain: `{report['chain']}`",
        "",
        "## Batch reports",
    ]
    for index, batch_report in enumerate(report["batches"], 1):
        name = f"{index:02d}_{batch_report['batch_id']}.md"
        index_lines.append(f"- [{batch_report['batch_id']}]({name})")
    index_lines.extend([
        "",
        "## Final queries",
        "- [final_queries.md](final_queries.md)",
        "- [trace.json](trace.json)",
        "",
    ])
    _write_text(run_dir / "00_index.md", "\n".join(index_lines))

    print(f"\nDone. Open report index:\n{run_dir / '00_index.md'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay benchmark/full_demo_case.json with the same chain as benchmark.mock_main and write visible per-batch reports.",
    )
    parser.add_argument("--fixture", default=str(ROOT / "benchmark" / "full_demo_case.json"), help="Benchmark fixture JSON path.")
    parser.add_argument("--out-dir", default=str(ROOT / "benchmark" / "demo_trace"), help="Directory for Markdown/JSON trace reports.")
    parser.add_argument("--max-batches", type=int, default=None, help="Optional prefix of batches to run for a faster smoke test.")
    parser.add_argument("--no-reset", action="store_true", help="Keep existing SQLite/Neo4j data instead of clearing this chat first.")
    parser.add_argument(
        "--skip-graphiti-card-write",
        action="store_true",
        help="Demo-speed mode: keep SQLite write flow, but skip MemoryCard add_episode writes to Graphiti.",
    )
    return parser


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("neo4j.notifications").setLevel(logging.WARNING)
    logging.getLogger("realtime.dispatcher").setLevel(logging.WARNING)
    args = build_parser().parse_args()
    raise SystemExit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
