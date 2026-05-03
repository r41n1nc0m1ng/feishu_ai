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

SOURCE_PATTERNS = [
    re.compile(p)
    for p in [
        r"原话",
        r"谁说的",
        r"依据(是什么|是啥|呢)?",
        r"来源",
        r"证据",
        r"聊天记录",
        r"当时.*(怎么说|说了什么)",
    ]
]

VERSION_PATTERNS = [
    re.compile(p)
    for p in [
        r"后来.*改了",
        r"改了吗",
        r"有没有.*(变|改|更新)",
        r"之前.*版本",
        r"历史版本",
        r"旧版本",
        r"最新版本",
        r"还是.*原来",
        r"更新了吗",
    ]
]

SUMMARY_PATTERNS = [
    re.compile(p)
    for p in [
        r"整体",
        r"总结",
        r"当前.*方案",
        r"当前.*边界",
        r"现在.*怎么定",
        r"整体.*怎么定",
        r"方向.*怎么定",
    ]
]

TOPIC_LIST_PATTERNS = [
    re.compile(p)
    for p in [
        r"当前.*topic",
        r"所有.*topic",
        r"全部.*topic",
        r"topic\s*summary",
        r"topicsummary",
        r"topic列表",
        r"topic总览",
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
        r"提交",
        r"必须",
        r"待办",
        r"周[一二三四五六日天]",
        r"\d{1,2}号",
    ]
]


MENTION_PREFIX_PATTERNS = [
    re.compile(p)
    for p in [
        r"^@机器人(?:\s|$)",
        r"^@_user_[A-Za-z0-9_]+(?:\s|$)",
        r"^<at\b[^>]*>.*?</at>(?:\s|$)",
    ]
]


TASK_INTENT_PATTERNS = [
    re.compile(p)
    for p in [
        r"负责",
        r"提交",
        r"待办",
        r"截止",
        r"必须.{0,8}(提交|完成|处理|交付|同步|反馈|发送)",
        r"(提交|完成|处理|交付|同步|反馈|发送).{0,8}(前|之前|截止)",
        r"(今天|明天|后天|本周|下周|周[一二三四五六日天]|\d{1,2}号).{0,12}(提交|完成|处理|交付|同步|反馈|发送)",
    ]
]


def has_explicit_bot_mention(text: str) -> bool:
    normalized = text.strip()
    if not normalized:
        return False
    return any(pattern.search(normalized) for pattern in MENTION_PREFIX_PATTERNS)


def is_explicit_query(text: str) -> bool:
    normalized = text.strip()
    if not normalized:
        return False
    if "?" in normalized or "？" in normalized:
        return True
    return any(pattern.search(normalized) for pattern in QUERY_PATTERNS)


def is_source_query(text: str) -> bool:
    normalized = text.strip()
    if not normalized:
        return False
    return any(pattern.search(normalized) for pattern in SOURCE_PATTERNS)


def is_version_query(text: str) -> bool:
    normalized = text.strip()
    if not normalized:
        return False
    return any(pattern.search(normalized) for pattern in VERSION_PATTERNS)


def is_summary_query(text: str) -> bool:
    normalized = text.strip()
    if not normalized:
        return False
    return any(pattern.search(normalized) for pattern in SUMMARY_PATTERNS)


def is_topic_list_query(text: str) -> bool:
    normalized = text.strip().lower()
    if not normalized:
        return False
    return any(pattern.search(normalized) for pattern in TOPIC_LIST_PATTERNS)


def is_schedule_like(text: str) -> bool:
    return any(pattern.search(text) for pattern in SCHEDULE_PATTERNS)


def is_task_like(text: str) -> bool:
    normalized = text.strip()
    if not normalized:
        return False
    has_anchor = any(pattern.search(normalized) for pattern in TASK_PATTERNS)
    has_intent = any(pattern.search(normalized) for pattern in TASK_INTENT_PATTERNS)
    return has_anchor and has_intent


def should_trigger_realtime(message) -> bool:
    if bool(getattr(message, "is_at_bot", False)):
        return True
    return has_explicit_bot_mention(getattr(message, "text", ""))


def classify_realtime_action(message) -> str:
    if should_trigger_realtime(message):
        return "query"
    if is_task_like(message.text):
        return "task"
    if is_schedule_like(message.text):
        return "schedule"
    return "noop"


def build_query_text(message) -> str:
    text = message.text.strip()
    # Strip lightweight @mentions from displayed text for retrieval quality.
    text = re.sub(r"@\S+\s*", "", text).strip()
    return text or message.text.strip()
