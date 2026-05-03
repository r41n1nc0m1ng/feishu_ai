import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import unittest
from types import SimpleNamespace

from realtime.triggers import (
    build_query_text,
    classify_realtime_action,
    has_explicit_bot_mention,
    is_source_query,
    is_summary_query,
    is_topic_list_query,
    should_trigger_realtime,
)


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
        self.assertEqual(classify_realtime_action(message), "noop")

    def test_text_mention_prefix_triggers_query(self):
        message = _msg("@机器人 当前整体方案是什么")
        self.assertTrue(has_explicit_bot_mention(message.text))
        self.assertTrue(should_trigger_realtime(message))
        self.assertEqual(classify_realtime_action(message), "query")

    def test_internal_user_mention_prefix_triggers_query(self):
        message = _msg("@_user_1 之前怎么定的")
        self.assertTrue(should_trigger_realtime(message))
        self.assertEqual(classify_realtime_action(message), "query")

    def test_schedule_classification(self):
        message = _msg("明天下午3点我们开评审会")
        self.assertEqual(classify_realtime_action(message), "schedule")

    def test_task_classification(self):
        message = _msg("张三负责接口联调，周五前完成")
        self.assertEqual(classify_realtime_action(message), "task")

    def test_complete_statement_is_not_task(self):
        message = _msg("我觉得p1已完成 可存档")
        self.assertEqual(classify_realtime_action(message), "noop")

    def test_completion_degree_statement_is_not_task(self):
        message = _msg("p2细节注意完成度")
        self.assertEqual(classify_realtime_action(message), "noop")

    def test_noop_classification(self):
        message = _msg("收到，我晚点看")
        self.assertEqual(classify_realtime_action(message), "noop")

    def test_summary_query_without_at_bot_does_not_trigger(self):
        message = _msg("当前整体方案是什么")
        self.assertFalse(should_trigger_realtime(message))
        self.assertEqual(classify_realtime_action(message), "noop")

    def test_task_priority_over_schedule(self):
        message = _msg("7号中午十二点之前必须提交demo")
        self.assertEqual(classify_realtime_action(message), "task")

    def test_build_query_text_strips_simple_mentions(self):
        message = _msg("@机器人 之前怎么定的")
        self.assertEqual(build_query_text(message), "之前怎么定的")

    def test_source_query_detection(self):
        self.assertTrue(is_source_query("当时是谁说的，原话在哪？"))
        self.assertTrue(is_source_query("依据是什么"))
        self.assertFalse(is_source_query("之前怎么定的"))

    def test_summary_query_detection(self):
        self.assertTrue(is_summary_query("当前整体方案是什么"))
        self.assertTrue(is_summary_query("总结一下现在怎么定的"))
        self.assertFalse(is_summary_query("原话在哪"))

    def test_topic_list_query_detection(self):
        self.assertTrue(is_topic_list_query("当前所有topic summary"))
        self.assertTrue(is_topic_list_query("topic列表"))
        self.assertFalse(is_topic_list_query("当前整体方案是什么"))
