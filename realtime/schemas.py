"""
实时通道执行流对象（P1 新增）。

避免 ScheduleCandidate / TaskCandidate / CardActionPayload 以临时 dict
在 action_handler → api_client → event_handler 之间漫游，统一类型边界。

三个对象均只在实时通道内使用，不进入长期记忆主链。
"""
from datetime import datetime, timezone
from typing import List, Optional
import uuid

from pydantic import BaseModel, Field

_now = lambda: datetime.now(timezone.utc)


class ScheduleCandidate(BaseModel):
    """从群聊消息中识别出的日程候选，等待用户确认后才真正创建飞书日程。

    P1 约定：只支持单次日程（不做重复日程）。
    """
    candidate_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    chat_id: str
    title: str
    start_time: Optional[datetime] = None       # 解析后的开始时间，None 表示未能解析
    duration_minutes: Optional[int] = None      # 预估时长（分钟），None 表示未知
    participants: List[str] = Field(default_factory=list)  # 飞书 open_id 列表
    raw_text: str = ""                          # 触发识别的原始消息文本
    created_at: datetime = Field(default_factory=_now)


class TaskCandidate(BaseModel):
    """从群聊消息中识别出的待办候选，等待用户确认后才真正创建飞书待办。

    P1 约定：只支持单负责人。
    """
    candidate_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    chat_id: str
    title: str
    assignee_id: Optional[str] = None          # 负责人飞书 open_id，None 表示未能解析
    assignee_name: str = ""                     # 负责人显示名称
    due_date: Optional[datetime] = None         # 截止时间，None 表示未知
    raw_text: str = ""
    created_at: datetime = Field(default_factory=_now)


class CardActionPayload(BaseModel):
    """飞书卡片回调载体，用户点击确认/忽略按钮时由 event_handler 解析并传递。"""
    action_type: str                            # "confirm_schedule" | "confirm_task" | "reject"
    candidate_id: str                           # 对应 ScheduleCandidate 或 TaskCandidate 的 ID
    candidate_type: str                         # "schedule" | "task"
    operator_id: str                            # 点击按钮的飞书用户 open_id
    chat_id: str
    timestamp: datetime = Field(default_factory=_now)
