import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import unittest
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from realtime.action_handler import (
    RealtimeActionHandler,
    _SCHEDULE_CANDIDATES,
    _TASK_CANDIDATES,
    extract_schedule_candidate,
    extract_task_candidate,
    render_schedule_reply,
    render_task_reply,
)
from realtime.schemas import CardActionPayload, ScheduleCandidate, TaskCandidate


class FakeAPIClient:
    def __init__(self):
        self.text_calls = []
        self.card_calls = []
        self.calendar_calls = []
        self.task_calls = []

    async def send_text(self, chat_id: str, text: str):
        self.text_calls.append((chat_id, text))

    async def send_card(self, chat_id: str, card: dict):
        self.card_calls.append((chat_id, card))

    async def create_calendar_event(self, candidate, operator_open_id: str):
        self.calendar_calls.append((candidate, operator_open_id))
        return {"ok": True, "event_id": "evt_1"}

    async def create_task(self, candidate, operator_open_id: str):
        self.task_calls.append((candidate, operator_open_id))
        return {"ok": True, "task_guid": "task_1"}


def _msg(text: str, *, mentions=None):
    return SimpleNamespace(
        chat_id="c1",
        text=text,
        mentions=mentions or [],
        sender_id="u_sender",
    )


class RealtimeActionHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _SCHEDULE_CANDIDATES.clear()
        _TASK_CANDIDATES.clear()

    async def test_handle_schedule_message_sends_card(self):
        api = FakeAPIClient()
        handler = RealtimeActionHandler(api_client=api)

        trace = await handler.handle_schedule_message(_msg("明天下午3点我们开评审会"))

        self.assertEqual(trace.action, "schedule")
        self.assertEqual(len(api.card_calls), 1)
        card = api.card_calls[0][1]
        self.assertEqual(card["header"]["title"]["content"], "日程确认")
        self.assertIn("评审会", card["elements"][0]["content"])
        self.assertEqual(len(_SCHEDULE_CANDIDATES), 1)

    async def test_handle_task_message_sends_card(self):
        api = FakeAPIClient()
        handler = RealtimeActionHandler(api_client=api)

        trace = await handler.handle_task_message(_msg("张三负责接口联调，周五前完成"))

        self.assertEqual(trace.action, "task")
        self.assertEqual(len(api.card_calls), 1)
        card = api.card_calls[0][1]
        self.assertEqual(card["header"]["title"]["content"], "待办确认")
        self.assertIn("接口联调", card["elements"][0]["content"])
        self.assertEqual(len(_TASK_CANDIDATES), 1)

    async def test_confirm_schedule_card_action(self):
        api = FakeAPIClient()
        handler = RealtimeActionHandler(api_client=api)
        await handler.handle_schedule_message(_msg("明天下午3点我们开评审会"))
        candidate_id = next(iter(_SCHEDULE_CANDIDATES.keys()))

        trace = await handler.handle_card_action(
            CardActionPayload(
                action_type="confirm_schedule",
                candidate_id=candidate_id,
                candidate_type="schedule",
                operator_id="ou_1",
                chat_id="c1",
            )
        )

        self.assertEqual(trace.action, "confirm_schedule")
        self.assertEqual(len(api.calendar_calls), 1)
        self.assertEqual(len(api.text_calls), 1)
        self.assertIn("已创建日程", api.text_calls[0][1])
        self.assertEqual(len(_SCHEDULE_CANDIDATES), 0)

    async def test_confirm_task_card_action(self):
        api = FakeAPIClient()
        handler = RealtimeActionHandler(api_client=api)
        await handler.handle_task_message(_msg("张三负责接口联调，周五前完成"))
        candidate_id = next(iter(_TASK_CANDIDATES.keys()))

        trace = await handler.handle_card_action(
            CardActionPayload(
                action_type="confirm_task",
                candidate_id=candidate_id,
                candidate_type="task",
                operator_id="ou_2",
                chat_id="c1",
            )
        )

        self.assertEqual(trace.action, "confirm_task")
        self.assertEqual(len(api.task_calls), 1)
        self.assertEqual(len(api.text_calls), 1)
        self.assertIn("已创建待办", api.text_calls[0][1])
        self.assertEqual(len(_TASK_CANDIDATES), 0)

    async def test_reject_card_action(self):
        api = FakeAPIClient()
        handler = RealtimeActionHandler(api_client=api)
        await handler.handle_task_message(_msg("张三负责接口联调，周五前完成"))
        candidate_id = next(iter(_TASK_CANDIDATES.keys()))

        trace = await handler.handle_card_action(
            CardActionPayload(
                action_type="reject",
                candidate_id=candidate_id,
                candidate_type="task",
                operator_id="ou_2",
                chat_id="c1",
            )
        )

        self.assertEqual(trace.action, "reject")
        self.assertEqual(len(api.task_calls), 0)
        self.assertEqual(len(_TASK_CANDIDATES), 0)


class ExtractCandidateTests(unittest.TestCase):
    def test_extract_schedule_candidate(self):
        candidate = extract_schedule_candidate(_msg("明天下午3点我们开 Demo 评审会"))
        self.assertEqual(candidate.chat_id, "c1")
        self.assertIsNotNone(candidate.start_time)
        self.assertEqual(candidate.duration_minutes, 60)
        self.assertIn("Demo", candidate.title)

    def test_extract_task_candidate(self):
        candidate = extract_task_candidate(_msg("张三负责接口联调，周五前完成", mentions=["ou_zhangsan"]))
        self.assertEqual(candidate.assignee_id, "ou_zhangsan")
        self.assertEqual(candidate.assignee_name, "张三")
        self.assertIsNotNone(candidate.due_date)
        self.assertIn("接口联调", candidate.title)


class RenderActionReplyTests(unittest.TestCase):
    def test_render_schedule_reply(self):
        candidate = ScheduleCandidate(
            chat_id="c1",
            title="Demo 评审会",
            start_time=datetime(2026, 5, 2, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
            duration_minutes=60,
            raw_text="明天下午3点开 Demo 评审会",
        )
        self.assertIn("Demo 评审会", render_schedule_reply(candidate))

    def test_render_task_reply(self):
        candidate = TaskCandidate(
            chat_id="c1",
            title="接口联调",
            assignee_id="ou_1",
            assignee_name="张三",
            raw_text="张三负责接口联调",
        )
        self.assertIn("接口联调", render_task_reply(candidate))
