import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import unittest
from types import SimpleNamespace

from realtime.triggers import build_query_text, classify_realtime_action, should_trigger_realtime


def _msg(text: str, *, is_at_bot: bool = False):
    return SimpleNamespace(
        text=text,
        is_at_bot=is_at_bot,
        mentions=["bot"] if is_at_bot else [],
    )


class RealtimeTriggerTests(unittest.TestCase):
    def test_at_bot_triggers_query(self):
        message = _msg("@机器人 企业级记忆这个事情", is_at_bot=True)
        self.assertTrue(should_trigger_realtime(message))
        self.assertEqual(classify_realtime_action(message), "query")

    def test_explicit_question_triggers_query(self):
        message = _msg("我们之前为什么不做企业级记忆来着？")
        self.assertEqual(classify_realtime_action(message), "query")

    def test_schedule_classification(self):
        message = _msg("明天下午3点我们开评审会")
        self.assertEqual(classify_realtime_action(message), "schedule")

    def test_task_classification(self):
        message = _msg("张三负责接口联调，周五前完成")
        self.assertEqual(classify_realtime_action(message), "task")

    def test_noop_classification(self):
        message = _msg("收到，我晚点看")
        self.assertEqual(classify_realtime_action(message), "noop")

    def test_build_query_text_strips_simple_mentions(self):
        message = _msg("@机器人 之前怎么定的")
        self.assertEqual(build_query_text(message), "之前怎么定的")
