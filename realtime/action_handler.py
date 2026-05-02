from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from realtime.schemas import CardActionPayload, ScheduleCandidate, TaskCandidate

logger = logging.getLogger(__name__)

_TEMPLATE_CACHE: dict[str, dict[str, Any]] = {}
_SCHEDULE_CANDIDATES: dict[str, ScheduleCandidate] = {}
_TASK_CANDIDATES: dict[str, TaskCandidate] = {}

_WEEKDAY_MAP = {
    "一": 0,
    "二": 1,
    "三": 2,
    "四": 3,
    "五": 4,
    "六": 5,
    "日": 6,
    "天": 6,
}


@dataclass
class ActionTrace:
    triggered: bool
    action: str
    reply_preview: str
    reason: str


def _local_tz() -> ZoneInfo:
    return ZoneInfo(os.getenv("APP_TIMEZONE", "Asia/Shanghai"))


def _now_local() -> datetime:
    return datetime.now(_local_tz())


def _title_from_schedule_text(text: str) -> str:
    title = re.sub(r"[，。,.！？!?]", " ", text).strip()
    title = re.sub(r"(今天|明天|后天|本周|下周|周[一二三四五六日天])", " ", title)
    title = re.sub(r"(上午|中午|下午|晚上|早上)", " ", title)
    title = re.sub(r"\d{1,2}[:：]\d{2}", " ", title)
    title = re.sub(r"\d{1,2}点半?", " ", title)
    title = re.sub(r"\s+", " ", title).strip()
    return title or text.strip()


def _title_from_task_text(text: str) -> str:
    cleaned = re.sub(
        r"^[A-Za-z0-9_\-\u4e00-\u9fa5]{1,12}?(?=(负责|周[一二三四五六日天]|今天|明天|后天|本周|下周|完成|提交|处理|搞定))",
        "",
        text,
    ).strip()
    cleaned = re.sub(r"^负责", "", cleaned).strip()
    cleaned = re.sub(r"(今天|明天|后天|本周|下周|周[一二三四五六日天])前?", " ", cleaned)
    cleaned = re.sub(r"(截止|完成|搞定|提交|处理)", " ", cleaned)
    cleaned = re.sub(r"[，。,.！？!?]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or text.strip()


def _extract_relative_day(text: str, base: datetime) -> Optional[datetime]:
    if "后天" in text:
        return base + timedelta(days=2)
    if "明天" in text:
        return base + timedelta(days=1)
    if "今天" in text:
        return base

    match = re.search(r"(本周|下周)?周([一二三四五六日天])", text)
    if not match:
        return None
    week_prefix = match.group(1) or ""
    weekday = _WEEKDAY_MAP[match.group(2)]
    days_ahead = weekday - base.weekday()
    if days_ahead < 0 or week_prefix == "下周":
        days_ahead += 7
    return base + timedelta(days=days_ahead)


def _extract_time_of_day(text: str) -> tuple[int, int]:
    hour = 9
    minute = 0

    match = re.search(r"(\d{1,2})[:：](\d{2})", text)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2))
    else:
        match = re.search(r"(\d{1,2})点(半|(\d{1,2})分?)?", text)
        if match:
            hour = int(match.group(1))
            if match.group(2) == "半":
                minute = 30
            elif match.group(3):
                minute = int(match.group(3))

    if any(token in text for token in ("下午", "晚上")) and hour < 12:
        hour += 12
    if "中午" in text and hour < 11:
        hour += 12
    return hour, minute


def _extract_duration_minutes(text: str) -> Optional[int]:
    if "半小时" in text:
        return 30
    match = re.search(r"(\d+)\s*小时", text)
    if match:
        return int(match.group(1)) * 60
    match = re.search(r"(\d+)\s*分钟", text)
    if match:
        return int(match.group(1))
    return 60


def extract_schedule_candidate(message) -> ScheduleCandidate:
    text = getattr(message, "text", "").strip()
    now = _now_local()
    day = _extract_relative_day(text, now) or now
    hour, minute = _extract_time_of_day(text)
    start_time = day.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if day == now and start_time < now and not re.search(r"(今天|明天|后天|周[一二三四五六日天])", text):
        start_time = start_time + timedelta(days=1)

    return ScheduleCandidate(
        chat_id=getattr(message, "chat_id", ""),
        title=_title_from_schedule_text(text),
        start_time=start_time,
        duration_minutes=_extract_duration_minutes(text),
        participants=list(getattr(message, "mentions", []) or []),
        raw_text=text,
    )


def extract_task_candidate(message) -> TaskCandidate:
    text = getattr(message, "text", "").strip()
    now = _now_local()
    due_anchor = _extract_relative_day(text, now)
    due_date = None
    if due_anchor:
        due_date = due_anchor.replace(hour=18, minute=0, second=0, microsecond=0)

    assignee_id = None
    assignee_name = ""
    mentions = list(getattr(message, "mentions", []) or [])
    if mentions:
        assignee_id = mentions[0]
    match = re.search(
        r"^([A-Za-z0-9_\-\u4e00-\u9fa5]{1,12}?)(?=(负责|周[一二三四五六日天]|今天|明天|后天|本周|下周|完成|提交|处理|搞定))",
        text,
    )
    if match:
        assignee_name = match.group(1)

    return TaskCandidate(
        chat_id=getattr(message, "chat_id", ""),
        title=_title_from_task_text(text),
        assignee_id=assignee_id,
        assignee_name=assignee_name,
        due_date=due_date,
        raw_text=text,
    )


def _format_time(dt: Optional[datetime]) -> str:
    if not dt:
        return "待确认"
    return dt.astimezone(_local_tz()).strftime("%m-%d %H:%M")


def render_schedule_reply(candidate: ScheduleCandidate) -> str:
    return (
        "检测到一个日程：\n\n"
        f"主题：{candidate.title}\n"
        f"时间：{_format_time(candidate.start_time)}\n"
        f"时长：{candidate.duration_minutes or 60} 分钟\n\n"
        "已发送确认卡片，点击后可直接创建。"
    )


def render_task_reply(candidate: TaskCandidate) -> str:
    assignee = candidate.assignee_name or candidate.assignee_id or "待确认"
    return (
        "检测到一个待办：\n\n"
        f"任务：{candidate.title}\n"
        f"负责人：{assignee}\n"
        f"截止时间：{_format_time(candidate.due_date)}\n\n"
        "已发送确认卡片，点击后可直接创建。"
    )


def _load_card_template(name: str) -> dict[str, Any]:
    if name not in _TEMPLATE_CACHE:
        path = Path(__file__).resolve().parents[1] / "feishu" / "cards" / name
        _TEMPLATE_CACHE[name] = json.loads(path.read_text(encoding="utf-8"))
    return json.loads(json.dumps(_TEMPLATE_CACHE[name], ensure_ascii=False))


def _fill_template(node: Any, variables: dict[str, str]) -> Any:
    if isinstance(node, dict):
        return {key: _fill_template(value, variables) for key, value in node.items()}
    if isinstance(node, list):
        return [_fill_template(item, variables) for item in node]
    if isinstance(node, str):
        for key, value in variables.items():
            node = node.replace(f"{{{{{key}}}}}", value)
        return node
    return node


def build_schedule_card(candidate: ScheduleCandidate) -> dict[str, Any]:
    template = _load_card_template("schedule_card.json")
    return _fill_template(
        template,
        {
            "candidate_id": candidate.candidate_id,
            "chat_id": candidate.chat_id,
            "title": candidate.title,
            "time_text": _format_time(candidate.start_time),
            "duration_text": str(candidate.duration_minutes or 60),
            "raw_text": candidate.raw_text,
        },
    )


def build_task_card(candidate: TaskCandidate) -> dict[str, Any]:
    template = _load_card_template("task_card.json")
    return _fill_template(
        template,
        {
            "candidate_id": candidate.candidate_id,
            "chat_id": candidate.chat_id,
            "title": candidate.title,
            "assignee_text": candidate.assignee_name or candidate.assignee_id or "待确认",
            "due_text": _format_time(candidate.due_date),
            "raw_text": candidate.raw_text,
        },
    )


def _store_schedule_candidate(candidate: ScheduleCandidate) -> None:
    _SCHEDULE_CANDIDATES[candidate.candidate_id] = candidate


def _store_task_candidate(candidate: TaskCandidate) -> None:
    _TASK_CANDIDATES[candidate.candidate_id] = candidate


def _pop_candidate(candidate_type: str, candidate_id: str):
    if candidate_type == "schedule":
        return _SCHEDULE_CANDIDATES.pop(candidate_id, None)
    return _TASK_CANDIDATES.pop(candidate_id, None)


def _get_candidate(candidate_type: str, candidate_id: str):
    if candidate_type == "schedule":
        return _SCHEDULE_CANDIDATES.get(candidate_id)
    return _TASK_CANDIDATES.get(candidate_id)


class RealtimeActionHandler:
    def __init__(
        self,
        send_text: Optional[Callable[[str, str], object]] = None,
        send_card: Optional[Callable[[str, dict[str, Any]], object]] = None,
        api_client=None,
    ):
        if api_client is None:
            from feishu.api_client import FeishuAPIClient

            api_client = FeishuAPIClient()
        self.api_client = api_client
        self.send_text = send_text if send_text is not None else getattr(api_client, "send_text", None)
        self.send_card = (
            send_card
            if send_card is not None
            else (None if send_text is not None else getattr(api_client, "send_card", None))
        )

    async def handle_schedule_message(self, message) -> ActionTrace:
        candidate = extract_schedule_candidate(message)
        _store_schedule_candidate(candidate)
        reply = render_schedule_reply(candidate)
        if self.send_card:
            await self.send_card(message.chat_id, build_schedule_card(candidate))
        elif self.send_text:
            await self.send_text(message.chat_id, reply)
        logger.info(
            "Realtime schedule card sent | chat=%s candidate=%s title=%s start=%s",
            message.chat_id,
            candidate.candidate_id,
            candidate.title,
            candidate.start_time,
        )
        return ActionTrace(
            triggered=True,
            action="schedule",
            reply_preview=reply[:120],
            reason="schedule-like text",
        )

    async def handle_task_message(self, message) -> ActionTrace:
        candidate = extract_task_candidate(message)
        _store_task_candidate(candidate)
        reply = render_task_reply(candidate)
        if self.send_card:
            await self.send_card(message.chat_id, build_task_card(candidate))
        elif self.send_text:
            await self.send_text(message.chat_id, reply)
        logger.info(
            "Realtime task card sent | chat=%s candidate=%s title=%s due=%s assignee=%s",
            message.chat_id,
            candidate.candidate_id,
            candidate.title,
            candidate.due_date,
            candidate.assignee_id,
        )
        return ActionTrace(
            triggered=True,
            action="task",
            reply_preview=reply[:120],
            reason="task-like text",
        )

    async def handle_card_action(self, payload: CardActionPayload) -> ActionTrace:
        if payload.action_type == "reject":
            _pop_candidate(payload.candidate_type, payload.candidate_id)
            reply = "已忽略这条执行事项。"
            logger.info(
                "Realtime card action rejected | type=%s candidate=%s operator=%s",
                payload.candidate_type,
                payload.candidate_id,
                payload.operator_id,
            )
            return ActionTrace(True, "reject", reply, "card rejected")

        candidate = _get_candidate(payload.candidate_type, payload.candidate_id)
        if not candidate:
            reply = "这张确认卡已失效，请重新发送原消息触发。"
            logger.warning(
                "Realtime card action candidate missing | type=%s candidate=%s operator=%s",
                payload.candidate_type,
                payload.candidate_id,
                payload.operator_id,
            )
            return ActionTrace(True, payload.action_type, reply, "candidate missing")

        if payload.action_type == "confirm_schedule":
            if not candidate.start_time:
                reply = "没有解析到可创建的日程时间，请补充更明确的时间后重试。"
                return ActionTrace(True, payload.action_type, reply, "missing schedule time")
            result = await self.api_client.create_calendar_event(candidate, payload.operator_id)
            if result.get("ok"):
                _pop_candidate(payload.candidate_type, payload.candidate_id)
            reply = (
                f"已创建日程：{candidate.title}（{_format_time(candidate.start_time)}）"
                if result.get("ok")
                else f"创建日程失败：{result.get('message', 'unknown error')}"
            )
            if result.get("ok") and result.get("warning"):
                reply = f"{reply}\n{result['warning']}"
        elif payload.action_type == "confirm_task":
            result = await self.api_client.create_task(candidate, payload.operator_id)
            if result.get("ok"):
                _pop_candidate(payload.candidate_type, payload.candidate_id)
            if result.get("ok"):
                location = result.get("url") or result.get("task_guid") or ""
                reply = f"已创建待办：{candidate.title}"
                if result.get("url"):
                    reply = f"{reply}\n打开链接：{result['url']}"
                elif location:
                    reply = f"{reply}\n待办ID：{location}"
            else:
                reply = f"创建待办失败：{result.get('message', 'unknown error')}"
        else:
            reply = f"未识别的卡片动作：{payload.action_type}"

        if self.send_text:
            await self.send_text(payload.chat_id, reply)
        logger.info(
            "Realtime card action handled | action=%s type=%s candidate=%s operator=%s reply=%s",
            payload.action_type,
            payload.candidate_type,
            payload.candidate_id,
            payload.operator_id,
            reply,
        )
        return ActionTrace(True, payload.action_type, reply[:120], "card action")
