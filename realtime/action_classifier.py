from __future__ import annotations

import json
import logging
import os
from typing import Optional

import httpx

from memory.llm_runtime import apply_thinking_payload

logger = logging.getLogger(__name__)

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
ACTION_MODEL = os.getenv("LOCAL_MODEL", "qwen2.5:7b")

_PROMPT = """\
你是一个飞书群聊实时动作分类器。请判断下面这条消息是否需要机器人立即发交互卡片。

可选 action:
- schedule: 用户明确要求创建/安排/约会议/开会，并且消息中有具体或可推断的时间。
- task: 用户明确要求某人负责、提交、完成、跟进待办，并且有明确执行对象。
- noop: 只是讨论、确认、追问、复述、记录、表达同意，或者信息不足，不应该发卡片。

重要规则:
- “确认一下日程是不是明天下午三点”“明天下午三点对吧”“日程不变吗”这类只是确认/追问，返回 noop。
- “明天下午三点创建一个 Demo 评审会”“帮我约明天下午三点评审会”这类才是 schedule。
- 如果不确定，返回 noop，避免打扰群聊。
- 只返回 JSON，不要解释。

消息:
{text}

规则初判 action: {rule_action}

输出 JSON:
{{"action":"schedule|task|noop","confidence":0.0到1.0,"reason":"一句话原因"}}
"""


def realtime_llm_action_enabled() -> bool:
    return os.getenv("REALTIME_LLM_ACTION_CLASSIFIER", "").strip().lower() in {"1", "true", "yes", "on"}


async def refine_realtime_action(text: str, rule_action: str) -> str:
    if not realtime_llm_action_enabled() or rule_action not in {"schedule", "task"}:
        return rule_action
    raw = await _call_classifier_llm(_PROMPT.format(text=text, rule_action=rule_action))
    action = (raw or {}).get("action", "")
    confidence = float((raw or {}).get("confidence") or 0)
    if action in {"schedule", "task", "noop"} and confidence >= 0.55:
        logger.info(
            "Realtime LLM action refine | rule=%s llm=%s confidence=%.2f reason=%s",
            rule_action,
            action,
            confidence,
            (raw or {}).get("reason", ""),
        )
        return action
    logger.info("Realtime LLM action refine fallback | rule=%s raw=%s", rule_action, raw)
    return rule_action


async def _call_classifier_llm(prompt: str) -> Optional[dict]:
    if os.getenv("DEEPSEEK_API_KEY"):
        return await _call_openai_compatible(
            prompt,
            api_key=os.getenv("DEEPSEEK_API_KEY", ""),
            base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
            model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
        )
    provider = os.getenv("MODEL_PROVIDER", "ollama").strip().lower()
    if provider == "openai" or os.getenv("OPENAI_API_KEY"):
        return await _call_openai_compatible(
            prompt,
            api_key=os.getenv("OPENAI_API_KEY", ""),
            base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            model=os.getenv("OPENAI_MODEL", ACTION_MODEL),
        )
    return await _call_ollama(prompt)


async def _call_openai_compatible(prompt: str, *, api_key: str, base_url: str, model: str) -> Optional[dict]:
    if not api_key:
        return None
    seed = int(os.getenv("LLM_SEED", "42"))
    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        resp = await client.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json=apply_thinking_payload({
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
                "temperature": 0,
                "top_p": 1,
                "seed": seed,
            }),
        )
        resp.raise_for_status()
        return json.loads(resp.json()["choices"][0]["message"]["content"])


async def _call_ollama(prompt: str) -> Optional[dict]:
    seed = int(os.getenv("LLM_SEED", "42"))
    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        resp = await client.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": ACTION_MODEL,
                "prompt": prompt,
                "stream": False,
                "format": "json",
                "options": {"temperature": 0, "seed": seed},
            },
        )
        resp.raise_for_status()
        return json.loads(resp.json().get("response", "{}"))
