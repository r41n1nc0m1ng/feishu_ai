import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import unittest
from types import SimpleNamespace

from realtime.action_handler import (
    RealtimeActionHandler,
    render_schedule_reply,
    render_task_reply,
)


class FakeSender:
    def __init__(self):
        self.calls = []

    async def __call__(self, chat_id: str, text: str):
        self.calls.append((chat_id, text))


def _msg(text: str):
    return SimpleNamespace(chat_id="c1", text=text)


class RealtimeActionHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_handle_schedule_message(self):
        sender = FakeSender()
        handler = RealtimeActionHandler(send_text=sender)

        trace = await handler.handle_schedule_message(_msg("明天下午3点我们开评审会"))

        self.assertEqual(trace.action, "schedule")
        self.assertIn("约日程", sender.calls[0][1])

    async def test_handle_task_message(self):
        sender = FakeSender()
        handler = RealtimeActionHandler(send_text=sender)

        trace = await handler.handle_task_message(_msg("张三负责接口联调，周五前完成"))

        self.assertEqual(trace.action, "task")
        self.assertIn("待办事项", sender.calls[0][1])


class RenderActionReplyTests(unittest.TestCase):
    def test_render_schedule_reply(self):
        self.assertIn("约日程", render_schedule_reply("明天下午3点"))

    def test_render_task_reply(self):
        self.assertIn("待办事项", render_task_reply("张三负责接口联调"))
