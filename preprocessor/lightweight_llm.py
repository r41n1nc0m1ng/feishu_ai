import logging
import os
from typing import List

import httpx

logger = logging.getLogger(__name__)

_GATE_PROMPT = """\
判断以下群聊消息中是否包含值得长期记录的决策、规则、方案取舍或版本变更。
仅回答 yes 或 no，不要其他内容。

消息：
{messages}"""


async def should_extract(messages: List[dict]) -> bool:
    """
    Lightweight LLM gate: returns True if the batch likely contains decision-worthy content.
    Fails open (returns True) if the LLM call fails, so extraction is not silently skipped.
    """
    ollama_url = os.getenv("OLLAMA_URL", "http://localhost:11434")
    model = os.getenv("LOCAL_MODEL", "qwen2.5:7b")
    messages_text = "\n".join(
        f"{m.get('sender', '?')}: {m.get('text', '')}" for m in messages
    )
    prompt = _GATE_PROMPT.format(messages=messages_text)

    try:
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            resp = await client.post(
                f"{ollama_url}/api/generate",
                json={"model": model, "prompt": prompt, "stream": False},
            )
            resp.raise_for_status()
            answer = resp.json().get("response", "").strip().lower()
            return answer.startswith("yes") or answer.startswith("是")
    except Exception as e:
        logger.warning("LLM gate failed, defaulting to pass-through: %s", e)
        return True
