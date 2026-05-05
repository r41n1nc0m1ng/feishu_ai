"""
飞书输入模拟器：从 full_demo_case.json 加载测试数据，提供逐 batch 消息访问。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_FIXTURE = Path(__file__).with_name("full_demo_case.json")


class CaseLoader:
    """加载 benchmark JSON fixture，提供结构化访问接口。"""

    def __init__(self, fixture_path: str | Path = DEFAULT_FIXTURE):
        with open(fixture_path, encoding="utf-8") as f:
            self._data: dict[str, Any] = json.load(f)

    @property
    def case_id(self) -> str:
        return self._data.get("case_id", "")

    @property
    def chat_id(self) -> str:
        return self._data.get("chat_id", "")

    @property
    def batches(self) -> list[dict[str, Any]]:
        return self._data.get("batches", [])

    @property
    def expected(self) -> dict[str, Any]:
        return self._data.get("expected", {})

    # ── 消息解析工具 ──────────────────────────────────────────────────────────

    def message_text(self, raw_msg: dict[str, Any]) -> str:
        raw = raw_msg.get("content", "{}")
        if isinstance(raw, dict):
            return raw.get("text", "")
        try:
            return json.loads(raw or "{}").get("text", "")
        except (json.JSONDecodeError, AttributeError):
            return str(raw or "")

    def is_at_bot(self, raw_msg: dict[str, Any]) -> bool:
        text = self.message_text(raw_msg)
        return "@机器人" in text or "@long_term_memory" in text

    def is_bot_sender(self, raw_msg: dict[str, Any]) -> bool:
        return raw_msg.get("sender", {}).get("sender_type", "user") == "app"

    # ── batch 消息过滤 ────────────────────────────────────────────────────────

    def realtime_messages(self, batch: dict[str, Any]) -> list[dict[str, Any]]:
        """实时层消息：全部 text 消息（含 @bot）。"""
        return [
            m for m in batch.get("messages", [])
            if m.get("msg_type", "text") == "text" and self.message_text(m)
        ]

    def write_messages(self, batch: dict[str, Any]) -> list[dict[str, Any]]:
        """写入层消息：过滤掉 @bot 消息和 bot 发送的消息。"""
        return [
            m for m in self.realtime_messages(batch)
            if not self.is_at_bot(m) and not self.is_bot_sender(m)
        ]
