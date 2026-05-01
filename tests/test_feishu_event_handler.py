import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import unittest
from unittest.mock import AsyncMock, patch

from feishu.event_handler import handle_raw_event


def _card_action_event():
    return {
        "header": {"event_type": "p2.card.action.trigger"},
        "event": {
            "operator": {"open_id": "ou_operator"},
            "action": {
                "value": {
                    "action_type": "confirm_task",
                    "candidate_id": "cand_1",
                    "candidate_type": "task",
                    "chat_id": "oc_chat_1",
                }
            },
            "context": {"open_chat_id": "oc_chat_1"},
        },
    }


class FeishuEventHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_handle_raw_card_action_routes_to_action_handler(self):
        with patch("feishu.event_handler.RealtimeActionHandler") as MockHandler:
            MockHandler.return_value.handle_card_action = AsyncMock()
            await handle_raw_event(_card_action_event())

            MockHandler.return_value.handle_card_action.assert_awaited_once()
            payload = MockHandler.return_value.handle_card_action.await_args.args[0]
            self.assertEqual(payload.action_type, "confirm_task")
            self.assertEqual(payload.candidate_id, "cand_1")
            self.assertEqual(payload.chat_id, "oc_chat_1")
