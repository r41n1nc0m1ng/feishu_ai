"""
运行时 LLM/Graphiti 行为开关。

所有 OpenAI-compatible 的 /chat/completions 调用站点应通过 `apply_thinking_payload(...)`
统一处理 enable_thinking 字段；所有 Graphiti 卡片写入入口应在开头检查
`should_skip_graphiti_card_write()`。

通过环境变量配置（与 .env.example 字段保持一致）：
  LLM_ENABLE_THINKING        true / false。留空 → 不注入 enable_thinking 字段
                             （保留 LLM 服务端默认行为，对未实现该字段的模型也安全）
  SKIP_GRAPHITI_CARD_WRITE   true → CardGenerator 跳过 Graphiti 卡片写入（仅写 SQLite + 内存缓存）
"""
import os
from typing import Any


def _env_bool(name: str) -> bool | None:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return None
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def apply_thinking_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """如果 LLM_ENABLE_THINKING 已设置，则向 OpenAI-compatible /chat/completions
    payload 注入 `enable_thinking` 字段（true/false 都注入）。setdefault 保证
    调用方显式传入的字段优先级更高。仅适用于支持该字段的服务端（如 DeepSeek）。
    """
    enable = _env_bool("LLM_ENABLE_THINKING")
    if enable is None:
        return payload
    payload.setdefault("enable_thinking", enable)
    return payload


def is_graphiti_disabled() -> bool:
    """DISABLE_GRAPHITI=true → 整条 Graphiti 链路完全停用：
    GraphitiClient.initialize() 直接 no-op（不连 Neo4j、不建索引、不持有客户端实例），
    所有依赖 g.g 的下游（_write_card_to_graphiti / search_memories / clear_group /
    evidence_store.save 的 graphiti 分支）会因为 g.g is None 自然兜底。
    """
    return _env_bool("DISABLE_GRAPHITI") is True


def should_skip_graphiti_card_write() -> bool:
    """SKIP_GRAPHITI_CARD_WRITE=true → CardGenerator 跳过 Graphiti 卡片写入。
    同时被 DISABLE_GRAPHITI 隐含包含（整条停用必然包含跳过卡片写入）。
    """
    return is_graphiti_disabled() or _env_bool("SKIP_GRAPHITI_CARD_WRITE") is True
