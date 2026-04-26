from datetime import datetime
from enum import Enum
from typing import List, Optional

import uuid
from pydantic import BaseModel, Field


class MemoryType(str, Enum):
    DECISION = "decision"
    TRADEOFF = "tradeoff"
    RULE = "rule"
    CONSTRAINT = "constraint"
    VERSION_UPDATE = "version_update"
    RISK = "risk"


class MemoryStatus(str, Enum):
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    PENDING = "pending"


class FeishuMessage(BaseModel):
    message_id: str
    sender_id: str
    chat_id: str
    text: str
    timestamp: datetime


class ExtractedMemory(BaseModel):
    title: str
    decision: str
    reason: str
    memory_type: MemoryType
    participants: List[str] = []


class EventBlock(BaseModel):
    """A time-windowed batch of messages flushed by TimeWindowAccumulator."""
    block_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    chat_id: str
    messages: List["FeishuMessage"]
    window_start: datetime
    window_end: datetime


class TopicNode(BaseModel):
    """High-granularity topic aggregated from multiple related decision episodes."""
    topic_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    chat_id: str
    title: str
    summary: str
    decision_ids: List[str] = []
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class MemoryItem(BaseModel):
    memory_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    chat_id: str
    title: str
    content: str
    decision: str
    reason: str
    memory_type: MemoryType
    source_message_ids: List[str] = []
    participants: List[str] = []
    version: int = 1
    status: MemoryStatus = MemoryStatus.ACTIVE
    time: datetime = Field(default_factory=datetime.utcnow)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    # Points to the memory_id this record supersedes (for version chains)
    supersedes: Optional[str] = None
