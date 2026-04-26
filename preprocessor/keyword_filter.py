import re
from typing import List

from memory.schemas import FeishuMessage

_TRIGGER_RE = re.compile(
    r"决定|确定|计划|方案|不做|改成|约定|规定|按照|截止|"
    r"就这样|按这个来|定了|那就|先不|暂时|改为|换成|取消|"
    r"不用|放弃|选择|采用|否定"
)


def has_decision_signal(message: FeishuMessage) -> bool:
    """Returns True if the message contains any decision-related trigger keyword."""
    return bool(_TRIGGER_RE.search(message.text))


def filter_batch(messages: List[FeishuMessage]) -> bool:
    """Returns True if any message in the batch has a decision signal."""
    return any(has_decision_signal(m) for m in messages)
