import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
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
from feishu.api_client import (
    FeishuAPIClient,
    _build_calendar_attendees,
    _pick_writable_calendar_id,
)


class FakeAPIClient:
    def __init__(self, *, task_ok=True):
        self.text_calls = []
        self.card_calls = []
        self.calendar_calls = []
        self.task_calls = []
        self.task_ok = task_ok

    async def send_text(self, chat_id: str, text: str):
        self.text_calls.append((chat_id, text))

    async def send_card(self, chat_id: str, card: dict):
        self.card_calls.append((chat_id, card))

    async def create_calendar_event(self, candidate, operator_open_id: str):
        self.calendar_calls.append((candidate, operator_open_id))
        return {"ok": True, "event_id": "evt_1"}

    async def create_task(self, candidate, operator_open_id: str):
        self.task_calls.append((candidate, operator_open_id))
        if self.task_ok:
            return {"ok": True, "task_guid": "task_1", "url": "https://example.com/task/1"}
        return {"ok": False, "message": "task api error"}


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
        self.assertIn("https://example.com/task/1", api.text_calls[0][1])
        self.assertEqual(len(_TASK_CANDIDATES), 0)

    async def test_failed_confirm_task_keeps_candidate_for_retry(self):
        api = FakeAPIClient(task_ok=False)
        handler = RealtimeActionHandler(api_client=api)
        await handler.handle_task_message(_msg("张三周一前完成 benchmark"))
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
        self.assertIn("创建待办失败", api.text_calls[0][1])
        self.assertIn(candidate_id, _TASK_CANDIDATES)

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

    def test_extract_task_candidate_without_resolved_open_id(self):
        candidate = extract_task_candidate(_msg("张三周一前完成 benchmark"))
        self.assertIsNone(candidate.assignee_id)
        self.assertEqual(candidate.assignee_name, "张三")
        self.assertIsNotNone(candidate.due_date)
        self.assertEqual(candidate.title, "benchmark")

    def test_extract_schedule_candidate_with_chinese_time_and_duration(self):
        candidate = extract_schedule_candidate(_msg("后天下午十点半要开个半小时的碰头会"))
        self.assertIsNotNone(candidate.start_time)
        self.assertEqual(candidate.start_time.hour, 22)
        self.assertEqual(candidate.start_time.minute, 30)
        self.assertEqual(candidate.duration_minutes, 30)
        self.assertEqual(candidate.title, "碰头会")

    def test_extract_task_candidate_with_absolute_day_and_deadline_time(self):
        candidate = extract_task_candidate(_msg("7号中午十二点之前必须提交demo"))
        self.assertIsNotNone(candidate.due_date)
        self.assertEqual(candidate.due_date.day, 7)
        self.assertEqual(candidate.due_date.hour, 12)
        self.assertEqual(candidate.due_date.minute, 0)
        self.assertEqual(candidate.title, "demo")


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


class FeishuAPIClientTaskPayloadTests(unittest.TestCase):
    def test_build_task_payload_includes_member_role(self):
        payload = FeishuAPIClient()._build_task_payload(
            TaskCandidate(
                chat_id="c1",
                title="benchmark",
                assignee_id="ou_zhangsan",
                assignee_name="张三",
                raw_text="张三周一前完成 benchmark",
            ),
            "ou_operator",
        )
        self.assertEqual(payload["members"][0]["role"], "assignee")
        self.assertEqual(payload["members"][0]["id"], "ou_zhangsan")

    def test_build_task_payload_falls_back_to_operator(self):
        payload = FeishuAPIClient()._build_task_payload(
            TaskCandidate(
                chat_id="c1",
                title="benchmark",
                assignee_name="张三",
                raw_text="张三周一前完成 benchmark",
            ),
            "ou_operator",
        )
        self.assertEqual(payload["members"][0]["id"], "ou_operator")

    def test_pick_writable_calendar_prefers_primary_writer(self):
        calendar_id = _pick_writable_calendar_id([
            {
                "calendar": {
                    "calendar_id": "group_readonly",
                    "access_role": "reader",
                    "is_primary": False,
                }
            },
            {
                "calendar": {
                    "calendar_id": "primary_owner",
                    "access_role": "owner",
                    "is_primary": True,
                }
            },
        ])
        self.assertEqual(calendar_id, "primary_owner")

    def test_pick_writable_calendar_returns_empty_when_all_readonly(self):
        calendar_id = _pick_writable_calendar_id([
            {"calendar": {"calendar_id": "c1", "access_role": "reader", "is_primary": True}},
            {"calendar": {"calendar_id": "c2", "access_role": "free_busy_reader", "is_primary": False}},
        ])
        self.assertEqual(calendar_id, "")

    def test_build_calendar_payload_uses_second_timestamps(self):
        start_time = datetime(2026, 5, 2, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        payload = FeishuAPIClient()._build_calendar_event_payload(
            ScheduleCandidate(
                chat_id="c1",
                title="Demo 评审会",
                start_time=start_time,
                duration_minutes=90,
                raw_text="明天下午3点开 Demo 评审会",
            )
        )
        self.assertEqual(payload["start_time"]["timestamp"], str(int(start_time.timestamp())))
        self.assertEqual(
            payload["end_time"]["timestamp"],
            str(int(start_time.timestamp()) + 90 * 60),
        )

    def test_build_calendar_attendees_filters_empty_ids(self):
        attendees = _build_calendar_attendees(["ou_1", "", "ou_2"])
        self.assertEqual(
            attendees,
            [
                {"type": "user", "user_id": "ou_1"},
                {"type": "user", "user_id": "ou_2"},
            ],
        )


class FeishuAPIClientCalendarRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolve_calendar_target_defaults_to_app(self):
        client = FeishuAPIClient()
        with patch.dict(os.environ, {"FEISHU_CALENDAR_TARGET": "app"}, clear=False):
            with patch.object(client, "get_app_calendar_id", AsyncMock(return_value="app_cal")):
                calendar_id, target = await client._resolve_calendar_target("ou_operator")
        self.assertEqual((calendar_id, target), ("app_cal", "app"))

    async def test_resolve_calendar_target_supports_user_primary(self):
        client = FeishuAPIClient()
        with patch.dict(os.environ, {"FEISHU_CALENDAR_TARGET": "user_primary"}, clear=False):
            with patch.object(client, "get_primary_calendar_id", AsyncMock(return_value="user_cal")):
                calendar_id, target = await client._resolve_calendar_target("ou_operator")
        self.assertEqual((calendar_id, target), ("user_cal", "user_primary"))

    async def test_get_app_calendar_id_accepts_calendars_list_shape(self):
        client = FeishuAPIClient()
        FeishuAPIClient._app_calendar_id = ""
        with patch.dict(os.environ, {"FEISHU_CALENDAR_ID": ""}, clear=False):
            with patch.object(client, "_get_token", AsyncMock(return_value="token")):
                with patch("feishu.api_client.httpx.AsyncClient") as MockClient:
                    MockClient.return_value.__aenter__.return_value.post = AsyncMock(
                        return_value=SimpleNamespace(
                            json=lambda: {
                                "code": 0,
                                "data": {
                                    "calendars": [
                                        {
                                            "calendar": {
                                                "calendar_id": "feishu.cn_test@group.calendar.feishu.cn",
                                                "role": "owner",
                                                "type": "primary",
                                            },
                                            "user_id": "ou_bot",
                                        }
                                    ]
                                },
                            }
                        )
                    )
                    calendar_id = await client.get_app_calendar_id()
        self.assertEqual(calendar_id, "feishu.cn_test@group.calendar.feishu.cn")
