from __future__ import annotations

import argparse
import asyncio
import os
import json
import logging
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from benchmark.input_simulator import CaseLoader
from benchmark.replay_adapter import DualChannelReplayAdapter
from feishu.api_client import FeishuAPIClient
from memory import store
from memory.batch_processor import BatchProcessor
from memory.card_generator import clear_cache as clear_card_cache
from memory.evidence_store import clear_cache as clear_block_cache
from memory.graphiti_client import GraphitiClient
from realtime.action_handler import RealtimeActionHandler
from realtime.dispatcher import dispatch_message
from realtime.query_handler import RealtimeQueryHandler

logger = logging.getLogger(__name__)

SENDER_NAMES = {
    "ou_pm_001": "PM",
    "ou_dev_001": "Dev",
    "ou_hr_001": "HR",
    "ou_algo_001": "Algo",
    "ou_design_001": "Design",
    "ou_qa_001": "QA",
    "ou_ops_001": "Ops",
}

SENDER_WEBHOOK_ENV = {
    "ou_pm_001": "DEMO_WEBHOOK_PM",
    "ou_dev_001": "DEMO_WEBHOOK_DEV",
    "ou_hr_001": "DEMO_WEBHOOK_HR",
    "ou_algo_001": "DEMO_WEBHOOK_ALGO",
    "ou_design_001": "DEMO_WEBHOOK_DESIGN",
    "ou_qa_001": "DEMO_WEBHOOK_QA",
    "ou_ops_001": "DEMO_WEBHOOK_OPS",
}


def _message_text(raw_msg: dict[str, Any]) -> str:
    raw = raw_msg.get("content", "{}")
    if isinstance(raw, dict):
        return str(raw.get("text", ""))
    try:
        parsed = json.loads(raw or "{}")
        if isinstance(parsed, dict):
            return str(parsed.get("text", ""))
    except Exception:
        pass
    return str(raw or "")


def _sender_id(raw_msg: dict[str, Any]) -> str:
    sender = raw_msg.get("sender") or {}
    return str(sender.get("id") or sender.get("open_id") or raw_msg.get("sender_id") or "")


def _sender_label(raw_msg: dict[str, Any]) -> str:
    sender_id = _sender_id(raw_msg)
    return SENDER_NAMES.get(sender_id, sender_id or "User")


def _time_label(raw_msg: dict[str, Any]) -> str:
    create_time = str(raw_msg.get("create_time") or raw_msg.get("timestamp") or "")
    if len(create_time) >= 16:
        return create_time[5:16]
    return create_time


def _display_text(raw_msg: dict[str, Any], *, include_role_label: bool) -> str:
    text = _message_text(raw_msg)
    if include_role_label:
        return f"[{_time_label(raw_msg)}] {_sender_label(raw_msg)}: {text}"
    return f"[{_time_label(raw_msg)}] {text}"


def _load_role_profiles(path: str) -> dict[str, str]:
    if not path:
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("role profile mapping must be a JSON object")
    return {str(k): str(v) for k, v in data.items() if v}


def _load_env_role_webhooks() -> dict[str, str]:
    webhooks: dict[str, str] = {}
    for sender_id, env_name in SENDER_WEBHOOK_ENV.items():
        value = os.getenv(env_name, "").strip().strip('"').rstrip(",")
        if value:
            webhooks[sender_id] = value
    return webhooks


class DemoFrontendSender:
    def __init__(
        self,
        *,
        api: FeishuAPIClient,
        chat_id: str,
        role_profiles: dict[str, str],
        role_webhooks: dict[str, str],
        lark_cli_entry: str,
        include_role_label: bool,
    ) -> None:
        self.api = api
        self.chat_id = chat_id
        self.role_profiles = role_profiles
        self.role_webhooks = role_webhooks
        self.lark_cli_entry = lark_cli_entry
        self.include_role_label = include_role_label

    async def send_system(self, text: str) -> None:
        await self._send_api(text)

    async def send_role_message(self, raw_msg: dict[str, Any]) -> None:
        text = _display_text(raw_msg, include_role_label=self.include_role_label)
        sender_id = _sender_id(raw_msg)
        webhook = self.role_webhooks.get(sender_id)
        if webhook:
            await self._send_webhook(webhook, text)
            return
        profile = self.role_profiles.get(sender_id)
        if profile:
            await self._send_cli(profile, text)
        else:
            await self._send_api(text)

    async def _send_api(self, text: str) -> None:
        try:
            await self.api.send_text(self.chat_id, text)
        except Exception:
            logger.exception("send_text failed | chat=%s text=%s", self.chat_id, text[:80])

    async def _send_webhook(self, webhook: str, text: str) -> None:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
                resp = await client.post(
                    webhook,
                    json={"msg_type": "text", "content": {"text": text}},
                )
            data = resp.json()
            if data.get("code") not in (0, None):
                logger.error("webhook send failed | code=%s msg=%s", data.get("code"), data.get("msg"))
        except Exception:
            logger.exception("webhook send failed | text=%s", text[:80])

    async def _send_cli(self, profile: str, text: str) -> None:
        cmd = [
            "node",
            self.lark_cli_entry,
            "--profile",
            profile,
            "im",
            "+messages-send",
            "--as",
            "bot",
            "--chat-id",
            self.chat_id,
            "--text",
            text,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error(
                "lark-cli send failed | profile=%s code=%s stderr=%s stdout=%s",
                profile,
                proc.returncode,
                stderr.decode("utf-8", errors="replace")[:500],
                stdout.decode("utf-8", errors="replace")[:500],
            )


async def _reset_demo_data(chat_id: str) -> None:
    store.clear_chat_data(chat_id)
    await GraphitiClient().clear_group(chat_id)
    clear_card_cache(chat_id)
    clear_block_cache(chat_id)


async def play_batch(
    *,
    batch: dict[str, Any],
    loader: CaseLoader,
    adapter: DualChannelReplayAdapter,
    bp: BatchProcessor,
    frontend: DemoFrontendSender,
    api: FeishuAPIClient,
    chat_id: str,
    message_delay: float,
    batch_pause: float,
) -> None:
    batch_id = str(batch.get("batch_id", ""))
    messages = loader.realtime_messages(batch)
    await frontend.send_system(f"--- Demo batch {batch_id} ---")

    query_handler = RealtimeQueryHandler(send_text=api.send_text)
    action_handler = RealtimeActionHandler(send_text=api.send_text, send_card=api.send_card)

    for raw_msg in messages:
        await frontend.send_role_message(raw_msg)
        runtime_msg = adapter.to_realtime_message(raw_msg, chat_id)
        try:
            await dispatch_message(runtime_msg, query_handler=query_handler, action_handler=action_handler)
        except Exception:
            logger.exception("realtime dispatch failed | batch=%s msg=%s", batch_id, runtime_msg.message_id)
        await asyncio.sleep(message_delay)

    write_messages = loader.write_messages(batch)
    if write_messages:
        fetch_batch = adapter.to_fetch_batch(write_messages, chat_id)
        blocks = await bp.process_fetch_batch(fetch_batch)
        await frontend.send_system(
            f"[backend] {batch_id} write done: {len(fetch_batch.messages)} messages -> {len(blocks)} EvidenceBlocks"
        )
    else:
        await frontend.send_system(f"[backend] {batch_id} no writable messages")

    if batch_pause > 0:
        await asyncio.sleep(batch_pause)


async def main_async(args: argparse.Namespace) -> int:
    if not args.keep_graphiti_card_write:
        os.environ["SKIP_GRAPHITI_CARD_WRITE"] = "true"

    await GraphitiClient.initialize()

    loader = CaseLoader(args.fixture)
    chat_id = args.chat_id or os.getenv("DEMO_CHAT_ID") or os.getenv("Demo_chat_id") or loader.chat_id
    adapter = DualChannelReplayAdapter()
    bp = BatchProcessor()
    api = FeishuAPIClient()
    role_profiles = _load_role_profiles(args.role_profiles)
    role_webhooks = _load_env_role_webhooks()
    frontend = DemoFrontendSender(
        api=api,
        chat_id=chat_id,
        role_profiles=role_profiles,
        role_webhooks=role_webhooks,
        lark_cli_entry=args.lark_cli_entry,
        include_role_label=not args.hide_role_label,
    )
    logger.info(
        "Demo frontend senders | env_webhooks=%d role_profiles=%d",
        len(role_webhooks),
        len(role_profiles),
    )

    if args.reset:
        await _reset_demo_data(chat_id)

    batches = loader.batches
    if args.max_batches is not None:
        batches = batches[: max(args.max_batches, 0)]

    await frontend.send_system(
        f"Demo playback start: {loader.case_id} | batches={len(batches)} | speed={args.message_delay}s/message"
    )
    for batch in batches:
        await play_batch(
            batch=batch,
            loader=loader,
            adapter=adapter,
            bp=bp,
            frontend=frontend,
            api=api,
            chat_id=chat_id,
            message_delay=args.message_delay,
            batch_pause=args.batch_pause,
        )

    await frontend.send_system("Demo playback finished. You can ask questions now.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fast-play a benchmark fixture into a Feishu chat while feeding the backend directly."
    )
    parser.add_argument("--fixture", default=str(ROOT / "benchmark" / "full_demo_case.json"))
    parser.add_argument("--chat-id", default="", help="Target Feishu chat_id. Defaults to DEMO_CHAT_ID, then fixture chat_id.")
    parser.add_argument("--message-delay", type=float, default=0.25, help="Seconds between displayed messages.")
    parser.add_argument("--batch-pause", type=float, default=1.0, help="Seconds between batches.")
    parser.add_argument("--max-batches", type=int, default=None)
    parser.add_argument("--reset", action="store_true", help="Clear backend memory for this chat before playback.")
    parser.add_argument("--keep-graphiti-card-write", action="store_true", help="Keep slow Graphiti MemoryCard writes.")
    parser.add_argument(
        "--role-profiles",
        default="",
        help="JSON mapping fixture sender_id to lark-cli profile name. When set, matching roles are sent by those bot profiles.",
    )
    parser.add_argument(
        "--lark-cli-entry",
        default=r"D:\software\nodejs\node_modules\@larksuite\cli\scripts\run.js",
        help="Path to @larksuite/cli scripts/run.js. Used to bypass PowerShell ps1 execution policy.",
    )
    parser.add_argument(
        "--hide-role-label",
        action="store_true",
        help="Hide PM/Dev/QA labels in message text. Useful when each role has its own bot avatar/name.",
    )
    return parser


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    args = build_parser().parse_args()
    raise SystemExit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
