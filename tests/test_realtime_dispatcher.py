import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import unittest
from types import SimpleNamespace

from realtime.action_handler import RealtimeActionHandler
from realtime.dispatcher import dispatch_message
from realtime.query_handler import RealtimeQueryHandler


class FakeRetriever:
    def __init__(self, results=None):
        self.results = results or []
        self.calls = []

    async def retrieve(self, chat_id: str, query: str, limit: int = 3):
        self.calls.append((chat_id, query, limit))
        return self.results


class FakeSender:
    def __init__(self):
        self.calls = []

    async def __call__(self, chat_id: str, text: str):
        self.calls.append((chat_id, text))


class FakeLegacyIngest:
    def __init__(self):
        self.calls = []

    async def __call__(self, message):
        self.calls.append(message)


def _msg(text: str, *, is_at_bot: bool = False):
    return SimpleNamespace(
        message_id="m1",
        sender_id="u1",
        chat_id="c1",
        chat_type="group",
        text=text,
        mentions=["bot"] if is_at_bot else [],
        is_at_bot=is_at_bot,
    )


class RealtimeDispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_query_path(self):
        sender = FakeSender()
        query_handler = RealtimeQueryHandler(retriever=FakeRetriever([]), send_text=sender)
        legacy = FakeLegacyIngest()

        trace = await dispatch_message(
            _msg("@机器人 之前怎么定的", is_at_bot=True),
            query_handler=query_handler,
            legacy_ingest=legacy,
        )

        self.assertEqual(trace.action, "query")
        self.assertFalse(trace.delegated_to_legacy)
        self.assertEqual(len(sender.calls), 1)
        self.assertEqual(len(legacy.calls), 0)

    async def test_schedule_path(self):
        sender = FakeSender()
        action_handler = RealtimeActionHandler(send_text=sender)
        legacy = FakeLegacyIngest()

        trace = await dispatch_message(
            _msg("明天下午3点我们开评审会"),
            action_handler=action_handler,
            legacy_ingest=legacy,
        )

        self.assertEqual(trace.action, "schedule")
        self.assertFalse(trace.delegated_to_legacy)
        self.assertEqual(len(sender.calls), 1)
        self.assertEqual(len(legacy.calls), 0)

    async def test_task_path(self):
        sender = FakeSender()
        action_handler = RealtimeActionHandler(send_text=sender)
        legacy = FakeLegacyIngest()

        trace = await dispatch_message(
            _msg("张三负责接口联调，周五前完成"),
            action_handler=action_handler,
            legacy_ingest=legacy,
        )

        self.assertEqual(trace.action, "task")
        self.assertFalse(trace.delegated_to_legacy)
        self.assertEqual(len(sender.calls), 1)
        self.assertEqual(len(legacy.calls), 0)

    async def test_noop_goes_to_legacy(self):
        legacy = FakeLegacyIngest()

        trace = await dispatch_message(
            _msg("收到，我晚点看"),
            legacy_ingest=legacy,
        )

        self.assertEqual(trace.action, "noop")
        self.assertTrue(trace.delegated_to_legacy)
        self.assertEqual(len(legacy.calls), 1)
