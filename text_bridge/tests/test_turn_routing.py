import unittest

from text_bridge.bridge.turn_router import route_user_text


class TurnRoutingTests(unittest.TestCase):
    def test_only_reviewed_short_phrases_are_actions(self):
        self.assertEqual(route_user_text("吃了").action, "CONFIRM_TAKEN")
        self.assertEqual(route_user_text("晚点").action, "DELAY")
        self.assertEqual(route_user_text("30分钟后").delay_minutes, 30)
        self.assertEqual(route_user_text("跳过").action, "SKIP")
        self.assertEqual(route_user_text("再说一遍").action, "REPEAT")

    def test_negative_and_batch_phrases_are_clarified(self):
        for text in ("不吃了", "没吃", "不知道吃没吃", "都吃了"):
            decision = route_user_text(text)
            self.assertIsNone(decision.action, text)
            self.assertTrue(decision.clarification)

    def test_arbitrary_chat_is_not_passed_through(self):
        decision = route_user_text("今天天气怎么样")
        self.assertIsNone(decision.action)
        self.assertTrue(decision.clarification)


if __name__ == "__main__":
    unittest.main()
