import json
import logging
import os
import typing
from datetime import datetime, timezone
from typing import List, Optional

import httpx
from openai import AsyncOpenAI
from graphiti_core import Graphiti
from graphiti_core.llm_client.openai_client import OpenAIClient, LLMConfig
from graphiti_core.llm_client.config import DEFAULT_MAX_TOKENS, ModelSize
from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
from graphiti_core.cross_encoder.client import CrossEncoderClient
from graphiti_core.nodes import EpisodeType


class PassthroughReranker(CrossEncoderClient):
    """No-op reranker — returns passages unchanged.
    Avoids the OpenAI logprobs dependency that Ollama doesn't support."""

    async def rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]:
        return [(p, 1.0) for p in passages]


def _build_example(model_class) -> dict:
    """Recursively build a concrete example dict from a Pydantic model.
    Shown to the LLM so it uses exact field names including nested ones."""
    result = {}
    for fname, finfo in model_class.model_fields.items():
        ann = finfo.annotation
        origin = typing.get_origin(ann)
        args = typing.get_args(ann)

        if origin is list:
            inner = args[0] if args else str
            if hasattr(inner, "model_fields"):
                result[fname] = [_build_example(inner)]
            elif inner is int:
                result[fname] = [0, 1]
            else:
                result[fname] = ["example"]
        elif origin is dict:
            result[fname] = {}
        elif hasattr(ann, "model_fields"):
            result[fname] = _build_example(ann)
        elif ann is str:
            result[fname] = "example_value"
        elif ann is int:
            result[fname] = 0
        elif ann is bool:
            result[fname] = True
        elif ann is float:
            result[fname] = 0.0
        else:
            result[fname] = None
    return result


class OllamaLLMClient(OpenAIClient):
    """
    Forces all Graphiti LLM calls through /v1/chat/completions.
    The default OpenAIClient uses /v1/responses for structured outputs,
    which Ollama does not implement.
    """

    async def _generate_response(
        self,
        messages,
        response_model=None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        model_size: ModelSize = ModelSize.medium,
    ):
        if response_model:
            example = _build_example(response_model)
            instruction = (
                "\n\nIMPORTANT: respond with JSON in EXACTLY this format "
                "(replace example values with real ones, keep ALL field names exactly as shown, "
                "no extra keys, no 'properties' wrapper):\n"
                + json.dumps(example, ensure_ascii=False, indent=2)
            )
            messages = list(messages)
            messages[-1] = type(messages[-1])(
                role=messages[-1].role,
                content=messages[-1].content + instruction,
            )

        openai_messages = self._convert_messages_to_openai_format(messages)
        model = self._get_model_for_size(model_size)
        response = await self._create_completion(
            model=model,
            messages=openai_messages,
            temperature=self.temperature,
            max_tokens=max_tokens or self.max_tokens,
        )
        return self._handle_json_response(response)

from memory.schemas import ExtractedMemory, FeishuMessage

logger = logging.getLogger(__name__)

_graphiti: Optional[Graphiti] = None


class GraphitiClient:
    def __init__(self):
        self.g = _graphiti

    @classmethod
    async def initialize(cls):
        global _graphiti

        ollama_url = os.getenv("OLLAMA_URL", "http://localhost:11434")
        llm_model = os.getenv("LOCAL_MODEL", "qwen2.5:7b")
        embed_model = os.getenv("EMBED_MODEL", "nomic-embed-text")

        # Ollama exposes an OpenAI-compatible API at /v1
        base_url = f"{ollama_url}/v1"

        # Pass a proxy-free httpx client into the openai SDK so that
        # Windows system proxy does not intercept localhost:11434 requests.
        no_proxy_http = httpx.AsyncClient(trust_env=False)

        llm_client = OllamaLLMClient(
            config=LLMConfig(
                api_key="ollama",
                model=llm_model,
                small_model=llm_model,  # prevent fallback to gpt-4.1-nano
                base_url=base_url,
            ),
            client=AsyncOpenAI(
                api_key="ollama",
                base_url=base_url,
                http_client=no_proxy_http,
            ),
        )

        embedder = OpenAIEmbedder(
            config=OpenAIEmbedderConfig(
                api_key="ollama",
                embedding_model=embed_model,
                base_url=base_url,
            ),
            client=AsyncOpenAI(
                api_key="ollama",
                base_url=base_url,
                http_client=httpx.AsyncClient(trust_env=False),
            ),
        )

        _graphiti = Graphiti(
            uri=os.getenv("NEO4J_URI", "bolt://localhost:7687"),
            user=os.getenv("NEO4J_USER", "neo4j"),
            password=os.getenv("NEO4J_PASSWORD", "password"),
            llm_client=llm_client,
            embedder=embedder,
            cross_encoder=PassthroughReranker(),
        )
        await _graphiti.build_indices_and_constraints()
        logger.info("Graphiti ready (LLM=%s, embed=%s)", llm_model, embed_model)

    async def add_memory_episode(
        self,
        chat_id: str,
        extracted: ExtractedMemory,
        message: FeishuMessage,
    ) -> Optional[str]:
        if not self.g:
            logger.error("Graphiti not initialized")
            return None

        episode_body = (
            f"标题：{extracted.title}\n"
            f"决策：{extracted.decision}\n"
            f"理由：{extracted.reason}\n"
            f"类型：{extracted.memory_type.value}\n"
            f"参与者：{', '.join(extracted.participants)}"
        )

        ref_time = message.timestamp
        if ref_time.tzinfo is None:
            ref_time = ref_time.replace(tzinfo=timezone.utc)

        try:
            result = await self.g.add_episode(
                name=f"{extracted.title}::{message.message_id}",
                episode_body=episode_body,
                source=EpisodeType.message,
                source_description=f"飞书群聊 {chat_id}",
                reference_time=ref_time,
                group_id=chat_id,
            )
            logger.info("Graphiti episode added | chat=%s title=%s", chat_id, extracted.title)
            return str(result) if result else None
        except Exception:
            logger.exception("Failed to add Graphiti episode for chat=%s", chat_id)
            return None

    async def search_memories(
        self, chat_id: str, query: str, limit: int = 5
    ) -> List[dict]:
        if not self.g:
            return []
        try:
            results = await self.g.search(
                query=query,
                group_ids=[chat_id],
                num_results=limit,
            )
            return [
                {
                    "fact": r.fact,
                    "uuid": str(r.uuid),
                    "valid_at": str(getattr(r, "valid_at", "")),
                }
                for r in results
            ]
        except Exception:
            logger.exception("Graphiti search failed for chat=%s", chat_id)
            return []

    async def deprecate_episode(self, episode_uuid: str):
        logger.info("Deprecating Graphiti episode %s", episode_uuid)
