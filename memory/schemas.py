from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional
import uuid

from pydantic import BaseModel, Field

_now = lambda: datetime.now(timezone.utc)


# ── 共享枚举 ──────────────────────────────────────────────────────────────────

class MemoryType(str, Enum):
    DECISION = "decision"           # 正式决策
    TRADEOFF = "tradeoff"           # 方案取舍
    RULE = "rule"                   # 协作规则
    CONSTRAINT = "constraint"       # 约束边界
    VERSION_UPDATE = "version_update"  # 版本更新
    RISK = "risk"                   # 关键风险
    PROGRESS = "progress"           # 讨论进行中，尚未形成一致决策


class CardStatus(str, Enum):
    ACTIVE = "active"
    DEPRECATED = "deprecated"


class MemoryRelationType(str, Enum):
    RELATED_TO = "related_to"       # 相关
    REFINES = "refines"             # 补充或细化（保留旧版 Active）
    SUPERSEDES = "supersedes"       # 覆盖旧版本（旧版变 Deprecated）
    CONTRADICTS = "contradicts"     # 冲突但未完成覆盖


class CardOperation(str, Enum):
    """LLM 对一个 EvidenceBlock 的处理判断结果（card_generator 使用）。"""
    ADD = "add"             # 新增记忆
    NOOP = "noop"           # 不值得记录
    PROGRESS = "progress"   # 未形成一致决策，记录讨论进度
    SUPERSEDE = "supersede" # 覆盖旧记忆（REFINE 留 P1）


# ── 实时通道对象（event_handler / 查询侧使用）────────────────────────────────

class FeishuMessage(BaseModel):
    """通过飞书 WebSocket 事件接收到的单条消息。"""
    message_id: str
    sender_id: str
    chat_id: str
    chat_type: str                          # "group" 或 "p2p"
    text: str
    timestamp: datetime
    mentions: List[str] = Field(default_factory=list)  # 被 @ 用户的 open_id 列表
    is_at_bot: bool = False                 # 是否 @ 了机器人，实时触发判断用


# ── 批处理通道对象（P0 核心，写入侧负责）─────────────────────────────────────

class ChatMemorySpace(BaseModel):
    """
    每个群聊的记忆空间元数据。
    机器人首次加入群时创建，记录增量拉取游标。
    """
    chat_id: str
    group_name: str = ""
    created_at: datetime = Field(default_factory=_now)
    last_fetch_at: Optional[datetime] = None   # 增量消息拉取的游标（上次处理到的时间）


class EvidenceMessage(BaseModel):
    """EvidenceBlock 内的单条消息，保持原始内容，不经过 LLM 处理。"""
    message_id: str
    sender_id: str
    sender_name: str = ""   # 显示名称，通过飞书 API 解析后填入
    timestamp: datetime
    text: str


class FetchBatch(BaseModel):
    """
    增量消息拉取步骤的输出（对应需求文档 4.4 批量消息获取层）。
    包含单个群聊在一个时间窗口内去重后的消息列表。
    """
    chat_id: str
    fetch_start: datetime
    fetch_end: datetime
    messages: List[EvidenceMessage] = Field(default_factory=list)


class EvidenceBlock(BaseModel):
    """
    同一事件边界内的原始聊天记录片段（对应需求文档 4.5 Event Segmentation 层）。
    原样保存，不做抽取或总结，是所有引用它的 MemoryCard 的来源依据。
    字段名与需求文档 4.5 输出示例及 4.7 存储规范对齐。
    """
    block_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    chat_id: str
    start_time: datetime
    end_time: datetime
    messages: List[EvidenceMessage] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_now)


class MemoryCard(BaseModel):
    """
    从一个或多个 EvidenceBlock 中提炼出的中粒度记忆（对应需求文档 4.6 节）。
    默认检索对象，直接回答"决定了什么、为什么"。
    字段名与需求文档 4.6 输出示例及 4.7 存储规范对齐。
    记忆间关系统一通过 MemoryRelation 表达，本类不冗余存储关系。
    """
    memory_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    chat_id: str
    decision_object: str            # 该决策所属的议题（展示用），如"企业级记忆是否进入 MVP"
    decision_object_key: Optional[str] = None   # 归一化业务主键：版本判断锚点 / Topic 聚合锚点 / 检索映射锚点
    title: str                      # 一句话标题
    decision: str                   # 决策内容
    reason: str                     # 决策理由
    memory_type: MemoryType = MemoryType.DECISION
    status: CardStatus = CardStatus.ACTIVE
    source_block_ids: List[str] = Field(default_factory=list)       # 来源 EvidenceBlock 的 block_id 列表
    supersedes_memory_id: Optional[str] = None                      # 冗余索引字段，真相源为 MemoryRelation 表
    effective_from: Optional[datetime] = None                       # 生效起始时间
    effective_until: Optional[datetime] = None                      # 生效截止时间（项目结束后可设置）
    last_retrieved_at: Optional[datetime] = None                    # 最近一次被检索的时间
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class MemoryRelation(BaseModel):
    """
    两张 MemoryCard 之间的显式关系（对应需求文档 4.7 memory_relations 表）。
    记录 supersedes / refines / related_to / contradicts 等版本链关系。

    【关系真相源】所有记忆间关系统一在此表达，MemoryCard 本冗余存储的字段
    （如 supersedes_memory_id）仅作便捷索引，以本表为准。

    P1 只保证 supersedes 关系落地；refines / related_to / contradicts 保留 schema，
    不纳入 P1 验收。
    """
    relation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    chat_id: str
    source_id: str          # 较新或主动方的 MemoryCard（memory_id）
    target_id: str          # 较旧或被引用方的 MemoryCard（memory_id）
    relation_type: MemoryRelationType
    created_at: datetime = Field(default_factory=_now)


# ── P1/P2 对象（当前不在 P0 主链路中）────────────────────────────────────────

class TopicSummary(BaseModel):
    """
    从多张相关 MemoryCard 聚合而来的高粒度主题摘要（对应需求文档 4.6 节）。
    用于回答"当前整体方案是什么""这个方向现在怎么定的"等整体性问题。
    字段名与需求文档 4.7 topic_summaries 存储规范对齐。
    """
    summary_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    chat_id: str
    topic: str                                                          # 主题标签，如"MVP 产品边界"
    summary: str                                                        # 当前状态的聚合描述
    covered_memory_ids: List[str] = Field(default_factory=list)        # 所覆盖的 MemoryCard 的 memory_id 列表
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


# ── 遗留对象（P0 之前的旧链路，保留以兼容现有代码）──────────────────────────

class MemoryStatus(str, Enum):
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    PENDING = "pending"


class ExtractedMemory(BaseModel):
    title: str
    decision: str
    reason: str
    memory_type: MemoryType
    participants: List[str] = Field(default_factory=list)


class EventBlock(BaseModel):
    """TimeWindowAccumulator 刷出的实时消息缓冲区（P0 之前的旧链路）。"""
    block_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    chat_id: str
    messages: List[FeishuMessage] = Field(default_factory=list)
    window_start: datetime
    window_end: datetime


class MemoryItem(BaseModel):
    memory_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    chat_id: str
    title: str
    content: str
    decision: str
    reason: str
    memory_type: MemoryType
    source_message_ids: List[str] = Field(default_factory=list)
    participants: List[str] = Field(default_factory=list)
    version: int = 1
    status: MemoryStatus = MemoryStatus.ACTIVE
    time: datetime = Field(default_factory=_now)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    supersedes: Optional[str] = None
