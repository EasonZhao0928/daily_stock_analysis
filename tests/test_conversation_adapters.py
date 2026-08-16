"""Characterization tests for stream adapters using only local fakes."""

from __future__ import annotations

import threading
import time
import unittest

from bot.models import BotMessage, BotResponse, ChatType
from bot.platforms.dingtalk_stream import DingtalkStreamHandler
from bot.platforms.feishu_stream import FeishuStreamHandler


class _Reply:
    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()

    def reply_text(self, **kwargs):
        with self.lock:
            self.calls.append(kwargs)
        return True


def _message(platform: str, message_id: str, chat_type: ChatType = ChatType.PRIVATE) -> BotMessage:
    return BotMessage(
        platform=platform,
        message_id=message_id,
        user_id="owner",
        user_name="Owner",
        chat_id="chat-1",
        chat_type=chat_type,
        content="/status",
        raw_content="/status",
        mentioned=True,
    )


class ConversationAdapterCharacterizationTest(unittest.TestCase):
    def test_feishu_lifecycle_health_and_reply_context(self):
        replies = _Reply()
        handler = FeishuStreamHandler(
            lambda message: BotResponse.text_response(message.message_id),
            replies,
        )
        try:
            self.assertTrue(handler.health().running)
            handler._enqueue_message(_message("feishu", "f-1"))
            deadline = time.time() + 1
            while len(replies.calls) < 1 and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(replies.calls[0]["message_id"], "f-1")
            self.assertEqual(handler.health().accepted, 1)
        finally:
            handler.shutdown(wait=True)
        self.assertFalse(handler.health().running)

    def test_dingtalk_adapter_keeps_opaque_reply_context(self):
        replies = []
        handler = DingtalkStreamHandler(
            lambda _message: BotResponse.text_response("ok"),
        )
        handler._reply_sender = lambda incoming, response: replies.append((incoming, response.text)) or True
        incoming = object()
        try:
            self.assertTrue(handler._enqueue_message(_message("dingtalk", "d-1"), incoming))
            deadline = time.time() + 1
            while not replies and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(replies, [(incoming, "ok")])
            self.assertTrue(handler.health().running)
        finally:
            handler.shutdown(wait=True)
        self.assertFalse(handler.health().running)


if __name__ == "__main__":
    unittest.main()
