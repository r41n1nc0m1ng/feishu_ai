"""
SQLite 持久化层，解决进程重启后内存缓存清空的问题。

存储 EvidenceBlock 和 MemoryCard 的完整 JSON，启动时自动恢复到内存缓存。
数据库文件默认位于项目根目录 memory_store.db。
"""
import logging
import sqlite3
from pathlib import Path
from typing import List, Optional

from memory.schemas import ChatMemorySpace, EvidenceBlock, MemoryCard

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
        conn.execute("CREATE INDEX IF NOT EXISTS idx_blocks_chat ON evidence_blocks(chat_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cards_chat  ON memory_cards(chat_id)")
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


# 模块加载时建表
init_db()
