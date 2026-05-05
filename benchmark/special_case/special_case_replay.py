from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from benchmark.replay_adapter import DualChannelReplayAdapter
from memory.card_generator import CardGenerator
from memory import store
from memory.batch_processor import _active_chats
from memory.card_generator import _card_cache, _cards_by_object
from memory.evidence_store import _block_cache, EvidenceStore
from memory.graphiti_client import GraphitiClient
from memory.schemas import CardStatus, ChatMemorySpace, FetchBatch, MemoryCard, TopicSummary
from memory.topic_manager import TopicManager
from preprocessor.event_segmenter import segment_async
from realtime.action_handler import RealtimeActionHandler
from realtime.dispatcher import dispatch_message
from realtime.query_handler import RealtimeQueryHandler, _LAST_QUERY_CARD_BY_CHAT


SPECIAL_CASE_DIR = Path(__file__).parent
REPORT_DIR = ROOT / "benchmark" / "reports"


@dataclass
class TimeBatch:
    batch_id: str
    window_start: datetime
    window_end: datetime
    messages: list[dict[str, Any]]


@dataclass
class CaptureSender:
    replies: list[tuple[str, str]] = field(default_factory=list)

    async def send_text(self, chat_id: str, text: str) -> None:
        self.replies.append((chat_id, text))

    async def send_card(self, chat_id: str, card: dict[str, Any]) -> None:
        title = (((card.get("header") or {}).get("title") or {}).get("content", ""))
        self.replies.append((chat_id, f"[card] {title}".strip()))

    def pop_text(self) -> str:
        if not self.replies:
            return ""
        return self.replies[-1][1]


def load_case(path: str | Path) -> dict[str, Any]:
    case_path = Path(path)
    with case_path.open("r", encoding="utf-8") as f:
        case = json.load(f)
    for key in ("case_id", "chat_id", "messages", "final_query_messages", "expected"):
        if key not in case:
            raise ValueError(f"{case_path} missing required field: {key}")
    return case


def discover_cases(case_path: Optional[str], case_dir: str | Path) -> list[Path]:
    if case_path:
        return [Path(case_path)]
    root = Path(case_dir)
    return sorted(path for path in root.glob("*.json") if path.is_file())


def parse_message_time(adapter: DualChannelReplayAdapter, raw_msg: dict[str, Any]) -> datetime:
    return adapter.parse_timestamp(adapter.raw_timestamp(raw_msg))


def split_time_batches(
    messages: list[dict[str, Any]],
    *,
    batch_hours: float = 1,
    overlap: int = 3,
    adapter: Optional[DualChannelReplayAdapter] = None,
) -> list[TimeBatch]:
    if batch_hours <= 0:
        raise ValueError("batch_hours must be > 0")
    if overlap < 0:
        raise ValueError("overlap must be >= 0")
    if not messages:
        return []

    adapter = adapter or DualChannelReplayAdapter()
    sorted_messages = sorted(messages, key=lambda msg: parse_message_time(adapter, msg))
    first_ts = parse_message_time(adapter, sorted_messages[0])
    window_seconds = batch_hours * 3600

    buckets: dict[int, list[dict[str, Any]]] = {}
    for msg in sorted_messages:
        delta = (parse_message_time(adapter, msg) - first_ts).total_seconds()
        bucket = int(delta // window_seconds)
        buckets.setdefault(bucket, []).append(msg)

    batches: list[TimeBatch] = []
    previous_current_messages: list[dict[str, Any]] = []
    for index, bucket in enumerate(sorted(buckets)):
        current = buckets[bucket]
        prefix = previous_current_messages[-overlap:] if overlap and previous_current_messages else []
        window_start = first_ts + timedelta(seconds=bucket * window_seconds)
        window_end = window_start + timedelta(seconds=window_seconds)
        batches.append(
            TimeBatch(
                batch_id=f"batch_{index + 1:03d}",
                window_start=window_start,
                window_end=window_end,
                messages=[*prefix, *current],
            )
        )
        previous_current_messages = current
    return batches


def reset_local_caches() -> None:
    _active_chats.clear()
    _card_cache.clear()
    _cards_by_object.clear()
    _block_cache.clear()
    _LAST_QUERY_CARD_BY_CHAT.clear()


def clear_sqlite() -> None:
    tables = [
        "evidence_blocks",
        "memory_cards",
        "memory_relations",
        "topic_summaries",
        "chat_spaces",
    ]
    with store._conn() as conn:
        for table in tables:
            conn.execute(f"DELETE FROM {table}")


async def clear_neo4j() -> None:
    from neo4j import AsyncGraphDatabase

    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    user = os.getenv("NEO4J_USER", "neo4j")
    password = os.getenv("NEO4J_PASSWORD", "password")
    async with AsyncGraphDatabase.driver(uri, auth=(user, password)) as driver:
        async with driver.session() as session:
            result = await session.run("MATCH (n) DETACH DELETE n")
            await result.consume()


async def reset_test_data() -> None:
    reset_local_caches()
    clear_sqlite()
    await clear_neo4j()


async def reset_test_data_sqlite_only() -> None:
    reset_local_caches()
    clear_sqlite()


async def ensure_graphiti_ready() -> None:
    await GraphitiClient.initialize()


async def write_fetch_batch(fetch_batch: FetchBatch, *, llm_concurrency: int = 1) -> dict[str, Any]:
    if llm_concurrency <= 0:
        raise ValueError("llm_concurrency must be >= 1")

    evidence_store = EvidenceStore()
    blocks = await segment_async(fetch_batch)

    async def process_block(block) -> int:
        await evidence_store.save(block)
        card = await CardGenerator().generate(block)
        return 1 if card else 0

    if llm_concurrency == 1 or len(blocks) <= 1:
        card_count = 0
        for block in blocks:
            card_count += await process_block(block)
    else:
        semaphore = asyncio.Semaphore(llm_concurrency)

        async def guarded_process(block) -> int:
            async with semaphore:
                return await process_block(block)

        card_counts = await asyncio.gather(*(guarded_process(block) for block in blocks))
        card_count = sum(card_counts)

    def enum_value(value: Any) -> Any:
        return getattr(value, "value", value)

    active_non_progress = sum(
        1
        for card in _card_cache.values()
        if card.chat_id == fetch_batch.chat_id
        and enum_value(getattr(card, "status", "")) == "active"
        and enum_value(getattr(card, "memory_type", "")) != "progress"
    )
    if active_non_progress:
        try:
            await TopicManager().rebuild_topics(fetch_batch.chat_id)
        except Exception as exc:
            return {
                "block_count": len(blocks),
                "card_count": card_count,
                "topic_error": str(exc),
            }
    return {"block_count": len(blocks), "card_count": card_count}


async def write_case_history(
    case: dict[str, Any],
    *,
    batch_hours: float,
    overlap: int,
    adapter: DualChannelReplayAdapter,
    llm_concurrency: int = 1,
    batch_concurrency: int = 1,
) -> list[dict[str, Any]]:
    if batch_concurrency <= 0:
        raise ValueError("batch_concurrency must be >= 1")

    batches = split_time_batches(
        case.get("messages") or [],
        batch_hours=batch_hours,
        overlap=overlap,
        adapter=adapter,
    )
    chat_id = str(case.get("chat_id") or "")
    space = ChatMemorySpace(chat_id=chat_id)
    _active_chats[chat_id] = space
    store.save_chat_space(space)

    async def process_batch(batch: TimeBatch) -> dict[str, Any]:
        fetch_batch = adapter.to_fetch_batch(batch.messages, chat_id)
        result = await write_fetch_batch(fetch_batch, llm_concurrency=llm_concurrency)
        return {
            "batch_id": batch.batch_id,
            "window_start": batch.window_start.isoformat(),
            "window_end": batch.window_end.isoformat(),
            "input_message_ids": [adapter.message_id(msg) for msg in batch.messages],
            "write_input_count": len(fetch_batch.messages),
            "llm_concurrency": llm_concurrency,
            "batch_concurrency": batch_concurrency,
            **result,
        }

    if batch_concurrency == 1 or len(batches) <= 1:
        return [await process_batch(batch) for batch in batches]

    semaphore = asyncio.Semaphore(batch_concurrency)

    async def guarded_process(batch: TimeBatch) -> dict[str, Any]:
        async with semaphore:
            return await process_batch(batch)

    return await asyncio.gather(*(guarded_process(batch) for batch in batches))


def expected_checks_by_query(case: dict[str, Any]) -> dict[str, dict[str, Any]]:
    checks = (case.get("expected") or {}).get("final_memory_checks") or []
    return {str(item.get("query_message_id") or ""): item for item in checks}


def check_keywords(actual: str, check: dict[str, Any]) -> dict[str, Any]:
    expected_keywords = list(check.get("expected_keywords") or [])
    forbidden_keywords = list(check.get("forbidden_keywords") or [])
    missing = [kw for kw in expected_keywords if kw and kw not in actual]
    forbidden_hits = [kw for kw in forbidden_keywords if kw and kw in actual]
    return {
        "expected_keywords": expected_keywords,
        "missing_expected_keywords": missing,
        "forbidden_keywords": forbidden_keywords,
        "forbidden_keyword_hits": forbidden_hits,
        "passed": not missing and not forbidden_hits,
    }


class SQLiteKeywordRetriever:
    """
    Benchmark-only fallback retriever.

    It keeps the write side on the normal SQLite MemoryCard truth source, but
    bypasses Graphiti semantic recall so special-case replay can run when the
    graph backend is unavailable or unstable.
    """

    async def retrieve(self, chat_id: str, query: str, limit: int = 5) -> list[MemoryCard]:
        cards = [
            card
            for card in store.get_cards_for_chat(chat_id)
            if card.status == CardStatus.ACTIVE
        ]
        scored = [
            (self._score(query, card), card)
            for card in cards
        ]
        scored.sort(key=lambda item: item[0], reverse=True)
        hits = [card for score, card in scored if score > 0]
        return hits[:limit]

    async def retrieve_all(self, chat_id: str, query: str, limit: int = 5) -> list[MemoryCard]:
        cards = store.get_cards_for_chat(chat_id)
        scored = [(self._score(query, card), card) for card in cards]
        scored.sort(key=lambda item: item[0], reverse=True)
        hits = [card for score, card in scored if score > 0]
        return hits[:limit]

    async def retrieve_topic_summary(self, chat_id: str, query: str, limit: int = 3) -> list[TopicSummary]:
        from memory.topic_manager import TopicManager

        summaries = await TopicManager().get_topics(chat_id)
        if not summaries:
            return []
        scored = [(self._score_topic(query, summary), summary) for summary in summaries]
        scored.sort(key=lambda item: item[0], reverse=True)
        hits = [summary for score, summary in scored if score > 0]
        return hits[:limit] if hits else summaries[:limit]

    async def expand_evidence(self, block_id: str):
        return await EvidenceStore().get(block_id)

    async def get_version_chain(self, memory_id: str) -> list[MemoryCard]:
        from memory.retriever import MemoryRetriever

        return await MemoryRetriever().get_version_chain(memory_id)

    def _score(self, query: str, card: MemoryCard) -> float:
        haystack = " ".join(
            [
                card.decision_object or "",
                card.title or "",
                card.decision or "",
                card.reason or "",
                card.memory_type.value if hasattr(card.memory_type, "value") else str(card.memory_type),
            ]
        )
        return self._char_overlap_score(query, haystack)

    def _score_topic(self, query: str, summary: TopicSummary) -> float:
        haystack = f"{summary.topic or ''} {summary.summary or ''}"
        return self._char_overlap_score(query, haystack)

    def _char_overlap_score(self, query: str, haystack: str) -> float:
        query_chars = {
            ch for ch in (query or "").strip()
            if not ch.isspace() and ch not in "@机器人？?，,。.!！"
        }
        haystack_chars = {
            ch for ch in (haystack or "").strip()
            if not ch.isspace()
        }
        if not query_chars or not haystack_chars:
            return 0.0
        inter = len(query_chars & haystack_chars)
        union = len(query_chars | haystack_chars)
        jaccard = inter / union if union else 0.0
        coverage = inter / len(query_chars)
        return jaccard + coverage


async def run_queries(
    case: dict[str, Any],
    *,
    adapter: DualChannelReplayAdapter,
    llm_judge: bool,
    retriever_backend: str = "graphiti",
) -> list[dict[str, Any]]:
    checks = expected_checks_by_query(case)
    sender = CaptureSender()
    retriever = SQLiteKeywordRetriever() if retriever_backend == "sqlite-keyword" else None
    handler = RealtimeQueryHandler(retriever=retriever, send_text=sender.send_text)
    action_handler = RealtimeActionHandler(send_text=sender.send_text, send_card=sender.send_card)
    results: list[dict[str, Any]] = []

    for raw_query in case.get("final_query_messages") or []:
        before_count = len(sender.replies)
        message = adapter.to_realtime_message(raw_query, str(case.get("chat_id") or ""))
        trace = await dispatch_message(
            message,
            query_handler=handler,
            action_handler=action_handler,
        )
        new_replies = sender.replies[before_count:]
        actual = "\n".join(reply for _, reply in new_replies)
        check = checks.get(message.message_id, {})
        expected_answer = str(check.get("expected_answer") or "")
        keyword_result = check_keywords(actual, check) if check else {}
        judge_result = await judge_answer(
            query=adapter.parse_content_text(raw_query),
            expected_answer=expected_answer,
            actual_reply=actual,
        ) if llm_judge and check else {}

        results.append(
            {
                "query_message_id": message.message_id,
                "query": adapter.parse_content_text(raw_query),
                "action": trace.action,
                "actual_reply": actual,
                "expected_answer": expected_answer,
                "expected_granularity": check.get("expected_granularity"),
                "keyword_check": keyword_result,
                "llm_judge": judge_result,
            }
        )
    return results


def _load_json_rows(table: str, chat_id: str) -> list[dict[str, Any]]:
    with sqlite3.connect(store.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(f"SELECT data FROM {table} WHERE chat_id=?", (chat_id,)).fetchall()
    return [json.loads(row["data"]) for row in rows]


def sqlite_summary(chat_id: str) -> dict[str, Any]:
    cards = _load_json_rows("memory_cards", chat_id)
    blocks = _load_json_rows("evidence_blocks", chat_id)
    relations = _load_json_rows("memory_relations", chat_id)
    topics = _load_json_rows("topic_summaries", chat_id)
    return {
        "db_path": str(store.DB_PATH),
        "memory_card_count": len(cards),
        "evidence_block_count": len(blocks),
        "memory_relation_count": len(relations),
        "topic_summary_count": len(topics),
        "memory_cards": [
            {
                "memory_id": card.get("memory_id"),
                "status": card.get("status"),
                "memory_type": card.get("memory_type"),
                "decision_object": card.get("decision_object"),
                "title": card.get("title"),
                "decision": card.get("decision"),
                "supersedes_memory_id": card.get("supersedes_memory_id"),
            }
            for card in cards
        ],
        "memory_relations": [
            {
                "relation_type": rel.get("relation_type"),
                "source_id": rel.get("source_id"),
                "target_id": rel.get("target_id"),
            }
            for rel in relations
        ],
    }


def _card_matches(card: dict[str, Any], keywords: Iterable[str]) -> bool:
    text = " ".join(str(card.get(key) or "") for key in ("decision_object", "title", "decision", "reason"))
    return all(keyword in text for keyword in keywords if keyword)


def relation_diagnostics(case: dict[str, Any], summary: dict[str, Any]) -> list[dict[str, Any]]:
    cards = summary.get("memory_cards") or []
    relations = summary.get("memory_relations") or []
    diagnostics: list[dict[str, Any]] = []
    for check in ((case.get("expected") or {}).get("relation_checks") or []):
        old_matches = [card for card in cards if _card_matches(card, check.get("old_expected_keywords") or [])]
        new_matches = [card for card in cards if _card_matches(card, check.get("new_expected_keywords") or [])]
        required = check.get("relation_type")
        forbidden = check.get("forbidden_relation_type")

        required_found = False
        forbidden_found = False
        for old in old_matches:
            for new in new_matches:
                for rel in relations:
                    is_pair = rel.get("source_id") == new.get("memory_id") and rel.get("target_id") == old.get("memory_id")
                    if is_pair and rel.get("relation_type") == required:
                        required_found = True
                    if forbidden and is_pair and rel.get("relation_type") == forbidden:
                        forbidden_found = True

        diagnostics.append(
            {
                "relation_type": required,
                "forbidden_relation_type": forbidden,
                "old_match_count": len(old_matches),
                "new_match_count": len(new_matches),
                "required_found": required_found,
                "forbidden_found": forbidden_found,
                "passed": bool(required_found if not forbidden else not forbidden_found),
            }
        )
    return diagnostics


async def judge_answer(*, query: str, expected_answer: str, actual_reply: str) -> dict[str, Any]:
    prompt = (
        "Score whether the actual answer matches the expected answer for the user query.\n"
        "Return only one digit: 0, 1, or 2.\n"
        "2 means semantically correct and complete. 1 means partially correct. 0 means wrong.\n\n"
        f"Query:\n{query}\n\n"
        f"Expected answer:\n{expected_answer}\n\n"
        f"Actual answer:\n{actual_reply}\n"
    )
    provider = os.getenv("MODEL_PROVIDER", "ollama").strip().lower()
    try:
        if provider == "openai" or os.getenv("OPENAI_API_KEY"):
            text = await _call_openai_judge(prompt)
        else:
            text = await _call_ollama_judge(prompt)
        score_text = "".join(ch for ch in text.strip() if ch in "012")[:1]
        if score_text not in {"0", "1", "2"}:
            raise ValueError(f"judge returned non-score output: {text!r}")
        return {"score": int(score_text)}
    except Exception as exc:
        return {"judge_error": str(exc)}


async def _call_openai_judge(prompt: str) -> str:
    api_key = os.getenv("OPENAI_API_KEY", "")
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.getenv("OPENAI_MODEL", os.getenv("LOCAL_MODEL", ""))
    async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
        resp = await client.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model, "messages": [{"role": "user", "content": prompt}]},
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


async def _call_ollama_judge(prompt: str) -> str:
    url = os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/")
    model = os.getenv("LOCAL_MODEL", "qwen2.5:7b")
    async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
        resp = await client.post(
            f"{url}/api/generate",
            json={"model": model, "prompt": prompt, "stream": False},
        )
        resp.raise_for_status()
        return resp.json().get("response", "")


def print_case_report(report: dict[str, Any]) -> None:
    print("\n" + "=" * 80)
    print(f"CASE {report['case_id']} | {report['test_type']}")
    print("=" * 80)
    print(f"SQLite: {report['sqlite']['db_path']}")
    print(
        "retriever: "
        f"{report.get('retriever_backend', 'graphiti')} "
        f"(graphiti_initialized={report.get('graphiti_initialized', True)})"
    )
    print(f"llm_concurrency: {report.get('llm_concurrency', 1)}")
    print(f"batch_concurrency: {report.get('batch_concurrency', 1)}")
    print(
        "written: "
        f"cards={report['sqlite']['memory_card_count']} "
        f"blocks={report['sqlite']['evidence_block_count']} "
        f"relations={report['sqlite']['memory_relation_count']} "
        f"topics={report['sqlite']['topic_summary_count']}"
    )
    print(f"batches: {len(report['batches'])}")

    if report.get("relation_diagnostics"):
        print("\nRelation diagnostics:")
        for item in report["relation_diagnostics"]:
            print(
                f"- {item['relation_type']} old={item['old_match_count']} "
                f"new={item['new_match_count']} required={item['required_found']} "
                f"forbidden={item['forbidden_found']} passed={item['passed']}"
            )

    for item in report["query_results"]:
        print("\n" + "-" * 80)
        print(f"Query {item['query_message_id']} | action={item['action']}")
        print(item["query"])
        print("\nActual:")
        print(item["actual_reply"] or "(empty)")
        print("\nExpected:")
        print(item["expected_answer"] or "(none)")
        keyword = item.get("keyword_check") or {}
        if keyword:
            print(
                "\nKeyword check: "
                f"passed={keyword.get('passed')} "
                f"missing={keyword.get('missing_expected_keywords')} "
                f"forbidden_hits={keyword.get('forbidden_keyword_hits')}"
            )
        judge = item.get("llm_judge") or {}
        if judge:
            print(f"LLM judge: {judge}")


async def run_case(
    case_path: str | Path,
    *,
    batch_hours: float,
    overlap: int,
    llm_judge: bool,
    reset: bool,
    retriever_backend: str,
    llm_concurrency: int,
    batch_concurrency: int,
) -> dict[str, Any]:
    case = load_case(case_path)
    if reset:
        if retriever_backend == "sqlite-keyword":
            await reset_test_data_sqlite_only()
        else:
            await reset_test_data()
    else:
        _LAST_QUERY_CARD_BY_CHAT.clear()

    if retriever_backend != "sqlite-keyword":
        await ensure_graphiti_ready()
    adapter = DualChannelReplayAdapter()
    batches = await write_case_history(
        case,
        batch_hours=batch_hours,
        overlap=overlap,
        adapter=adapter,
        llm_concurrency=llm_concurrency,
        batch_concurrency=batch_concurrency,
    )
    query_results = await run_queries(
        case,
        adapter=adapter,
        llm_judge=llm_judge,
        retriever_backend=retriever_backend,
    )
    summary = sqlite_summary(str(case.get("chat_id") or ""))
    relation_report = relation_diagnostics(case, summary)
    return {
        "case_path": str(case_path),
        "case_id": case.get("case_id"),
        "test_type": case.get("test_type"),
        "dirty_run": not reset,
        "retriever_backend": retriever_backend,
        "graphiti_initialized": retriever_backend != "sqlite-keyword",
        "llm_concurrency": llm_concurrency,
        "batch_concurrency": batch_concurrency,
        "batch_hours": batch_hours,
        "overlap": overlap,
        "batches": batches,
        "sqlite": summary,
        "relation_diagnostics": relation_report,
        "query_results": query_results,
    }


def default_report_path() -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return REPORT_DIR / f"special_case_{timestamp}.json"


async def main_async(args: argparse.Namespace) -> int:
    report_path = Path(args.report) if args.report else default_report_path()
    all_reports = []
    cases = discover_cases(args.case, args.dir)
    if not cases:
        raise ValueError("no special case JSON files found")

    for index, path in enumerate(cases):
        case_reset = not args.no_reset
        # Keep cases independent by resetting before every case unless explicitly disabled.
        report = await run_case(
            path,
            batch_hours=args.batch_hours,
            overlap=args.overlap,
            llm_judge=args.llm_judge,
            reset=case_reset,
            retriever_backend=args.retriever_backend,
            llm_concurrency=args.llm_concurrency,
            batch_concurrency=args.batch_concurrency,
        )
        print_case_report(report)
        all_reports.append(report)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "report_path": str(report_path),
        "case_count": len(all_reports),
        "reports": all_reports,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\nReport written:", report_path)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay benchmark/special_case single-stream cases.")
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument("--case", help="Path to one special case JSON file.")
    group.add_argument("--dir", default=str(SPECIAL_CASE_DIR), help="Directory containing special case JSON files.")
    parser.add_argument("--batch-hours", type=float, default=1.0, help="Time window size in hours.")
    parser.add_argument("--overlap", type=int, default=3, help="Messages from previous non-empty window to prepend.")
    parser.add_argument(
        "--llm-concurrency",
        type=int,
        default=1,
        help="Concurrent EvidenceBlock LLM generations inside each batch. Default 1 preserves write order.",
    )
    parser.add_argument(
        "--batch-concurrency",
        type=int,
        default=1,
        help="Concurrent time batches to process. Default 1 preserves batch order.",
    )
    parser.add_argument("--llm-judge", action="store_true", help="Ask the configured model to score each answer.")
    parser.add_argument("--report", help="JSON report output path.")
    parser.add_argument("--no-reset", action="store_true", help="Do not clear SQLite/Neo4j before running.")
    parser.add_argument(
        "--retriever-backend",
        choices=("graphiti", "sqlite-keyword"),
        default="graphiti",
        help="Query retriever for replay. sqlite-keyword bypasses Graphiti and searches SQLite MemoryCards.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
