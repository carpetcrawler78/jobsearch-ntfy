from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from src.main import (
    execute_google_request,
    is_eligible,
    ntfy_sequence_id,
    parse_outbox_row,
    send_notification,
)


class FakeGoogleError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.resp = Mock(status=status)


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

    def test_blank_worker_status_is_implicit_pending(self) -> None:
        message = parse_outbox_row(
            34,
            ["msg-34", "2026-08-29T10:00:00+02:00", "Builder", "Title", "Body", "min"],
        )

        self.assertTrue(is_eligible(message, max_attempts=3))

    def test_empty_sheet_row_is_not_eligible(self) -> None:
        self.assertFalse(is_eligible(parse_outbox_row(999, []), max_attempts=3))

    def test_terminal_and_exhausted_rows_are_not_eligible(self) -> None:
        sent = parse_outbox_row(2, ["id", "date", "source", "title", "body", "min", "SENT", 1])
        skipped = parse_outbox_row(3, ["id", "date", "source", "title", "body", "min", "SKIPPED_STALE", 0])
        exhausted = parse_outbox_row(4, ["id", "date", "source", "title", "body", "min", "RETRY", 3])

        self.assertFalse(is_eligible(sent, max_attempts=3))
        self.assertFalse(is_eligible(skipped, max_attempts=3))
        self.assertFalse(is_eligible(exhausted, max_attempts=3))


class GoogleRetryTests(unittest.TestCase):
    @patch("src.main.time.sleep")
    def test_retryable_503_recovers(self, sleep: Mock) -> None:
        first = Mock()
        first.execute.side_effect = FakeGoogleError(503)
        second = Mock()
        second.execute.return_value = {"values": [["ok"]]}
        factory = Mock(side_effect=[first, second])

        result = execute_google_request(
            factory,
            operation="test read",
            max_attempts=5,
        )

        self.assertEqual(result, {"values": [["ok"]]})
        self.assertEqual(factory.call_count, 2)
        sleep.assert_called_once_with(1)

    @patch("src.main.time.sleep")
    def test_non_retryable_403_fails_immediately(self, sleep: Mock) -> None:
        request = Mock()
        request.execute.side_effect = FakeGoogleError(403)

        with self.assertRaises(FakeGoogleError):
            execute_google_request(
                Mock(return_value=request),
                operation="test write",
                max_attempts=5,
            )

        sleep.assert_not_called()


class NtfyRequestTests(unittest.TestCase):
    @patch("src.main.requests.post")
    def test_any_2xx_is_successful_and_sequence_is_stable(self, post: Mock) -> None:
        post.return_value = Mock(status_code=204, text="")
        sequence_id = ntfy_sequence_id("message-1")

        result = send_notification(
            title="chatgpt: Test",
            body="Safe body",
            priority="default",
            topic="example-topic",
            sequence_id=sequence_id,
        )

        self.assertTrue(result.successful)
        post.assert_called_once()
        _, kwargs = post.call_args
        self.assertEqual(kwargs["timeout"], (5, 20))
        self.assertEqual(kwargs["headers"]["Title"], "chatgpt: Test")
        self.assertEqual(kwargs["headers"]["X-Sequence-ID"], sequence_id)
        self.assertEqual(len(sequence_id), 32)

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
