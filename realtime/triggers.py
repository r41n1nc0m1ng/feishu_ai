from __future__ import annotations

import re

QUERY_PATTERNS = [
    re.compile(p)
    for p in [
        r"为什么",
        r"怎么定的",
        r"之前.*(怎么|为何|为什么|咋)",
        r"之前.*说",
        r"原话",
        r"谁说的",
        r"依据是什么",
        r"来着[？?]?$",
        r"到底.*(做不做|怎么定)",
    ]
]

SCHEDULE_PATTERNS = [
    re.compile(p)
    for p in [
        r"开会",
        r"会议",
        r"评审",
        r"同步",
        r"明天",
        r"[0-9一二三四五六七八九十]+点",
    ]
]

TASK_PATTERNS = [
    re.compile(p)
    for p in [
        r"负责",
        r"截止",
        r"完成",
        r"待办",
        r"周[一二三四五六日天]",
    ]
]


def is_explicit_query(text: str) -> bool:
    normalized = text.strip()
    if not normalized:
        return False
    if "?" in normalized or "？" in normalized:
        return True
    return any(pattern.search(normalized) for pattern in QUERY_PATTERNS)


def is_schedule_like(text: str) -> bool:
    return any(pattern.search(text) for pattern in SCHEDULE_PATTERNS)


def is_task_like(text: str) -> bool:
    return any(pattern.search(text) for pattern in TASK_PATTERNS)


def should_trigger_realtime(message) -> bool:
    if message.is_at_bot:
        return True
    return is_explicit_query(message.text)


def classify_realtime_action(message) -> str:
    if should_trigger_realtime(message):
        return "query"
    if is_schedule_like(message.text):
        return "schedule"
    if is_task_like(message.text):
        return "task"
    return "noop"


def build_query_text(message) -> str:
    text = message.text.strip()
    # Strip lightweight @mentions from displayed text for retrieval quality.
    text = re.sub(r"@\S+\s*", "", text).strip()
    return text or message.text.strip()
