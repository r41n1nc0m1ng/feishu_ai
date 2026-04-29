import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import unittest
from datetime import datetime
from types import SimpleNamespace

from realtime.query_handler import (
    RealtimeQueryHandler,
    render_evidence_reply,
    render_query_reply,
)


class FakeRetriever:
    def __init__(self, results, evidence=None):
        self.results = results
        self.evidence = evidence or {}
        self.calls = []
        self.evidence_calls = []

    async def retrieve(self, chat_id: str, query: str, limit: int = 3):
        self.calls.append((chat_id, query, limit))
        return self.results

    async def expand_evidence(self, block_id: str):
        self.evidence_calls.append(block_id)
        return self.evidence.get(block_id)


class FakeSender:
    def __init__(self):
        self.calls = []

    async def __call__(self, chat_id: str, text: str):
        self.calls.append((chat_id, text))


def _msg(text: str):
    return SimpleNamespace(
        message_id="m1",
        sender_id="u1",
        chat_id="c1",
        chat_type="group",
        text=text,
        mentions=["bot"],
        is_at_bot=True,
    )


def _card(decision: str, reason: str = "", source_block_ids=None):
    return SimpleNamespace(
        chat_id="c1",
        decision_object="企业级记忆",
        title="企业级记忆决策",
        decision=decision,
        reason=reason,
        status="active",
        source_block_ids=source_block_ids or [],
    )


def _block():
    return SimpleNamespace(
        messages=[
            SimpleNamespace(
                sender_name="小王",
                sender_id="u1",
                timestamp=datetime(2026, 4, 28, 10, 0),
                text="我觉得这次不要做企业级记忆了，权限太复杂。",
            ),
            SimpleNamespace(
                sender_name="小李",
                sender_id="u2",
                timestamp=datetime(2026, 4, 28, 10, 2),
                text="同意，先聚焦群聊决策记忆。",
            ),
        ]
    )


class RealtimeQueryHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_handle_query_message_with_hits(self):
        retriever = FakeRetriever([_card("MVP 阶段不做企业级记忆", "权限太复杂")])
        sender = FakeSender()
        handler = RealtimeQueryHandler(retriever=retriever, send_text=sender)

        trace = await handler.handle_query_message(_msg("@机器人 之前怎么定的"))

        self.assertEqual(trace.action, "query")
        self.assertEqual(trace.retrieved_count, 1)
        self.assertEqual(retriever.calls[0][0], "c1")
        self.assertEqual(sender.calls[0][0], "c1")
        self.assertIn("MVP 阶段不做企业级记忆", sender.calls[0][1])

    async def test_handle_query_message_with_no_hits(self):
        retriever = FakeRetriever([])
        sender = FakeSender()
        handler = RealtimeQueryHandler(retriever=retriever, send_text=sender)

        trace = await handler.handle_query_message(_msg("@机器人 之前怎么定的"))

        self.assertEqual(trace.retrieved_count, 0)
        self.assertIn("没有查到", sender.calls[0][1])

    async def test_handle_source_query_expands_evidence(self):
        retriever = FakeRetriever(
            [_card("MVP 阶段不做企业级记忆", "权限太复杂", ["block_1"])],
            {"block_1": _block()},
        )
        sender = FakeSender()
        handler = RealtimeQueryHandler(retriever=retriever, send_text=sender)

        trace = await handler.handle_query_message(_msg("@机器人 原话在哪"))

        self.assertEqual(trace.action, "source")
        self.assertEqual(retriever.evidence_calls, ["block_1"])
        self.assertIn("来源记录", sender.calls[0][1])
        self.assertIn("权限太复杂", sender.calls[0][1])


class RenderReplyTests(unittest.TestCase):
    def test_render_query_reply(self):
        reply = render_query_reply("企业级记忆", [_card("不做企业级记忆", "权限复杂")])
        self.assertIn("不做企业级记忆", reply)
        self.assertIn("权限复杂", reply)

    def test_render_query_reply_includes_source_hint(self):
        reply = render_query_reply("企业级记忆", [_card("不做企业级记忆", source_block_ids=["b1"])])
        self.assertIn("依据", reply)

    def test_render_evidence_reply(self):
        reply = render_evidence_reply("原话在哪", _card("不做企业级记忆"), _block())
        self.assertIn("来源记录", reply)
        self.assertIn("小王", reply)
