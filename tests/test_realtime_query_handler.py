import unittest
from types import SimpleNamespace

from realtime.query_handler import RealtimeQueryHandler, render_query_reply


class FakeRetriever:
    def __init__(self, results):
        self.results = results
        self.calls = []

    async def retrieve(self, chat_id: str, query: str, limit: int = 3):
        self.calls.append((chat_id, query, limit))
        return self.results


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


def _card(decision: str, reason: str = ""):
    return SimpleNamespace(
        chat_id="c1",
        decision_object="企业级记忆",
        title="企业级记忆决策",
        decision=decision,
        reason=reason,
        status="active",
        source_block_ids=[],
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


class RenderReplyTests(unittest.TestCase):
    def test_render_query_reply(self):
        reply = render_query_reply("企业级记忆", [_card("不做企业级记忆", "权限复杂")])
        self.assertIn("不做企业级记忆", reply)
        self.assertIn("权限复杂", reply)
