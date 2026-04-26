import json
import logging
import os
from typing import Optional

import httpx

from memory.schemas import ExtractedMemory, MemoryType

logger = logging.getLogger(__name__)

# ── Model config (edit here to switch models) ────────────────────────────────
OPENCLAW_URL: str = os.getenv("OPENCLAW_URL", "").rstrip("/")
OLLAMA_URL: str   = os.getenv("OLLAMA_URL", "http://localhost:11434")
EXTRACT_MODEL: str = os.getenv("LOCAL_MODEL", "qwen2.5:7b")   # extraction
# To use a different model just for extraction, override EXTRACT_MODEL here:
# EXTRACT_MODEL = "qwen2.5:14b"
# ─────────────────────────────────────────────────────────────────────────────

_EXTRACTION_PROMPT = """\
你是一个群聊决策记忆提取助手。分析以下飞书群聊消息，判断是否包含值得长期记忆的决策信息。

群聊消息：
{messages}

历史记忆摘要（供参考）：
{memory_hints}

【判断标准】包含以下任意一项则视为有记忆价值：
- 方案取舍（为什么选A不选B）
- 明确决策结论（决定/确定/就这样/按这个来）
- 协作规则或约束边界
- 版本变更或旧方案覆盖

请以JSON格式返回（只返回JSON，不要有其他内容）：
若有记忆价值：
{{
  "has_memory": true,
  "title": "一句话标题",
  "decision": "决策内容",
  "reason": "决策理由",
  "memory_type": "从以下选项中选一个：decision / tradeoff / rule / constraint / version_update / risk",
  "participants": ["参与者open_id列表"]
}}
若无记忆价值：
{{"has_memory": false}}
"""


class OpenClawClient:
    """
    Middleware layer connecting to OpenClaw server or Ollama local model.
    Priority: OPENCLAW_URL (if set) → Ollama fallback.
    """

    def __init__(self):
        self.openclaw_url = OPENCLAW_URL
        self.ollama_url = OLLAMA_URL
        self.model = EXTRACT_MODEL

    async def extract_memory(self, context: dict) -> Optional[ExtractedMemory]:
        messages_text = "\n".join(
            f"{m.get('sender', '?')}: {m.get('text', '')}"
            for m in context.get("messages", [])
        )
        hints_text = "\n".join(context.get("memory_hints", [])) or "（暂无）"
        prompt = _EXTRACTION_PROMPT.format(messages=messages_text, memory_hints=hints_text)

        raw: Optional[dict] = None
        if self.openclaw_url:
            raw = await self._call_openclaw(prompt, context)
        if raw is None:
            raw = await self._call_ollama(prompt)

        if raw and raw.get("has_memory"):
            try:
                raw_type = raw.get("memory_type", "decision")
                # Guard against model returning combined values like "decision|tradeoff"
                if raw_type not in MemoryType._value2member_map_:
                    raw_type = "decision"
                return ExtractedMemory(
                    title=raw.get("title", ""),
                    decision=raw.get("decision", ""),
                    reason=raw.get("reason", ""),
                    memory_type=MemoryType(raw_type),
                    participants=raw.get("participants", []),
                )
            except Exception as e:
                logger.error("Failed to parse ExtractedMemory: %s | raw=%s", e, raw)
        return None

    async def _call_openclaw(self, prompt: str, context: dict) -> Optional[dict]:
        """
        POST to OpenClaw server's chat endpoint.
        Adjust the request schema to match your OpenClaw version.
        """
        try:
            async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
                resp = await client.post(
                    f"{self.openclaw_url}/v1/chat/completions",
                    json={
                        "model": self.model,
                        "messages": [{"role": "user", "content": prompt}],
                        "response_format": {"type": "json_object"},
                    },
                )
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]
                return json.loads(content)
        except Exception as e:
            logger.warning("OpenClaw call failed, falling back to Ollama: %s", e)
            return None

    async def _call_ollama(self, prompt: str) -> Optional[dict]:
        try:
            async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
                resp = await client.post(
                    f"{self.ollama_url}/api/generate",
                    json={"model": self.model, "prompt": prompt, "stream": False, "format": "json"},
                )
                resp.raise_for_status()
                return json.loads(resp.json().get("response", "{}"))
        except Exception as e:
            logger.error("Ollama call failed: %s", e)
            return None
