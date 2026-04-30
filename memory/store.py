"""
SQLite 持久化层 — 系统的结构化事实真相源。

三层架构中的职责：
  - SQLite（本模块）：EvidenceBlock / MemoryCard / MemoryRelation / TopicSummary
    的完整 JSON 存储，负责生命周期状态管理与精确 ID 查询，重启后恢复内存缓存。
  - Graphiti（memory/graphiti_client.py）：仅作语义候选召回，不作状态真相源。
  - LLM（card_generator / topic_manager）：负责抽取与摘要，不独占主键与状态定义。

数据库文件默认位于项目根目录 memory_store.db。
"""
import logging
import sqlite3
from pathlib import Path
from typing import List, Optional

from memory.schemas import (
    ChatMemorySpace,
    EvidenceBlock,
    MemoryCard,
    MemoryRelation,
    TopicSummary,
)

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "memory_store.db"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS evidence_blocks (
                block_id   TEXT PRIMARY KEY,
                chat_id    TEXT NOT NULL,
                data       TEXT NOT NULL,
                created_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_cards (
                memory_id        TEXT PRIMARY KEY,
                chat_id          TEXT NOT NULL,
                decision_object  TEXT,
                decision         TEXT,
                data             TEXT NOT NULL,
                created_at       TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_spaces (
                chat_id       TEXT PRIMARY KEY,
                group_name    TEXT,
                created_at    TEXT,
                last_fetch_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_relations (
                relation_id  TEXT PRIMARY KEY,
                chat_id      TEXT NOT NULL,
                source_id    TEXT NOT NULL,
                target_id    TEXT NOT NULL,
                relation_type TEXT NOT NULL,
                data         TEXT NOT NULL,
                created_at   TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS topic_summaries (
                summary_id   TEXT PRIMARY KEY,
                chat_id      TEXT NOT NULL,
                topic        TEXT,
                data         TEXT NOT NULL,
                created_at   TEXT,
                updated_at   TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_blocks_chat    ON evidence_blocks(chat_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cards_chat     ON memory_cards(chat_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_relations_src  ON memory_relations(source_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_relations_tgt  ON memory_relations(target_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_relations_chat ON memory_relations(chat_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_summaries_chat ON topic_summaries(chat_id)")
    logger.debug("SQLite 数据库已初始化 | path=%s", DB_PATH)


# ── EvidenceBlock ─────────────────────────────────────────────────────────────

def save_evidence_block(block: EvidenceBlock) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO evidence_blocks VALUES (?,?,?,?)",
            (block.block_id, block.chat_id, block.model_dump_json(), str(block.created_at)),
        )


def load_evidence_block(block_id: str) -> Optional[EvidenceBlock]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT data FROM evidence_blocks WHERE block_id=?", (block_id,)
        ).fetchone()
    return EvidenceBlock.model_validate_json(row["data"]) if row else None


def load_all_evidence_blocks() -> List[EvidenceBlock]:
    with _conn() as conn:
        rows = conn.execute("SELECT data FROM evidence_blocks").fetchall()
    return [EvidenceBlock.model_validate_json(r["data"]) for r in rows]


# ── MemoryCard ────────────────────────────────────────────────────────────────

def save_memory_card(card: MemoryCard) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO memory_cards VALUES (?,?,?,?,?,?)",
            (
                card.memory_id, card.chat_id, card.decision_object,
                card.decision, card.model_dump_json(), str(card.created_at),
            ),
        )


def load_memory_card(memory_id: str) -> Optional[MemoryCard]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT data FROM memory_cards WHERE memory_id=?", (memory_id,)
        ).fetchone()
    return MemoryCard.model_validate_json(row["data"]) if row else None


def load_all_memory_cards() -> List[MemoryCard]:
    with _conn() as conn:
        rows = conn.execute("SELECT data FROM memory_cards").fetchall()
    return [MemoryCard.model_validate_json(r["data"]) for r in rows]


def get_cards_for_chat(chat_id: str) -> List[MemoryCard]:
    """返回某群聊的全部 MemoryCard，按创建时间倒序。"""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT data FROM memory_cards WHERE chat_id=? ORDER BY created_at DESC",
            (chat_id,),
        ).fetchall()
    return [MemoryCard.model_validate_json(r["data"]) for r in rows]


# ── ChatMemorySpace ───────────────────────────────────────────────────────────

def save_chat_space(space: ChatMemorySpace) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO chat_spaces VALUES (?,?,?,?)",
            (
                space.chat_id,
                space.group_name,
                str(space.created_at),
                str(space.last_fetch_at) if space.last_fetch_at else None,
            ),
        )


def load_all_chat_spaces() -> List[ChatMemorySpace]:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM chat_spaces").fetchall()
    spaces = []
    for r in rows:
        space = ChatMemorySpace(chat_id=r["chat_id"], group_name=r["group_name"] or "")
        if r["last_fetch_at"] and r["last_fetch_at"] != "None":
            from datetime import datetime
            try:
                space.last_fetch_at = datetime.fromisoformat(r["last_fetch_at"])
            except ValueError:
                pass
        spaces.append(space)
    return spaces


# ── MemoryRelation ────────────────────────────────────────────────────────────

def save_relation(relation: MemoryRelation) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO memory_relations VALUES (?,?,?,?,?,?,?)",
            (
                relation.relation_id,
                relation.chat_id,
                relation.source_id,
                relation.target_id,
                relation.relation_type.value,
                relation.model_dump_json(),
                str(relation.created_at),
            ),
        )


def load_relations_by_card(memory_id: str) -> List[MemoryRelation]:
    """返回以 memory_id 为 source 或 target 的全部关系。"""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT data FROM memory_relations WHERE source_id=? OR target_id=?",
            (memory_id, memory_id),
        ).fetchall()
    return [MemoryRelation.model_validate_json(r["data"]) for r in rows]


def load_relations_by_chat(chat_id: str) -> List[MemoryRelation]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT data FROM memory_relations WHERE chat_id=?", (chat_id,)
        ).fetchall()
    return [MemoryRelation.model_validate_json(r["data"]) for r in rows]


# ── TopicSummary ──────────────────────────────────────────────────────────────

def save_topic_summary(topic: TopicSummary) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO topic_summaries VALUES (?,?,?,?,?,?)",
            (
                topic.summary_id,
                topic.chat_id,
                topic.topic,
                topic.model_dump_json(),
                str(topic.created_at),
                str(topic.updated_at),
            ),
        )


def load_topics_by_chat(chat_id: str) -> List[TopicSummary]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT data FROM topic_summaries WHERE chat_id=? ORDER BY updated_at DESC",
            (chat_id,),
        ).fetchall()
    return [TopicSummary.model_validate_json(r["data"]) for r in rows]


def delete_topics_by_chat(chat_id: str) -> None:
    """清除某群的全部 TopicSummary，rebuild_topics 重建前调用。"""
    with _conn() as conn:
        conn.execute("DELETE FROM topic_summaries WHERE chat_id=?", (chat_id,))


# 模块加载时建表
init_db()
