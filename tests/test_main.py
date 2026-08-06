from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from src.main import parse_outbox_row, send_notification


class OutboxParsingTests(unittest.TestCase):
    def test_parse_outbox_row_applies_defaults(self) -> None:
        message = parse_outbox_row(
            2,
            [
                "mailcheck-1",
                "2026-08-06T07:00:00+02:00",
                "Important Email Check",
                "Title",
                "Body",
                "",
                "pending",
                "",
            ],
        )

        self.assertEqual(message.row_number, 2)
        self.assertEqual(message.priority, "default")
        self.assertEqual(message.status, "PENDING")
        self.assertEqual(message.attempts, 0)


class NtfyRequestTests(unittest.TestCase):
    @patch("src.main.requests.post")
    def test_any_2xx_is_successful(self, post: Mock) -> None:
        post.return_value = Mock(status_code=204, text="")

        result = send_notification(
            title="chatgpt: Test",
            body="Safe body",
            priority="default",
            topic="example-topic",
        )

        self.assertTrue(result.successful)
        post.assert_called_once()
        _, kwargs = post.call_args
        self.assertEqual(kwargs["timeout"], (5, 20))
        self.assertEqual(kwargs["headers"]["Title"], "chatgpt: Test")

    @patch("src.main.requests.post")
    def test_non_2xx_is_failure(self, post: Mock) -> None:
        post.return_value = Mock(status_code=503, text="temporarily unavailable")

        result = send_notification(
            title="chatgpt: Test",
            body="Safe body",
            priority="high",
            topic="example-topic",
        )

        self.assertFalse(result.successful)
        self.assertEqual(result.status_code, 503)


if __name__ == "__main__":
    unittest.main()
