from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import requests

LOGGER = logging.getLogger("jobsearch_ntfy")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
OUTBOX_RANGE = os.getenv("OUTBOX_RANGE", "NTFY_OUTBOX!A2:K1000")
DEFAULT_NTFY_BASE_URL = "https://ntfy.sh"
RETRYABLE_GOOGLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}


@dataclass(frozen=True)
class OutboxMessage:
    row_number: int
    message_id: str
    created_at: str
    source: str
    title: str
    body: str
    priority: str
    status: str
    attempts: int

    @property
    def has_producer_data(self) -> bool:
        return any(
            (self.message_id, self.created_at, self.source, self.title, self.body)
        )


@dataclass(frozen=True)
class NtfyResult:
    status_code: int
    response_text: str

    @property
    def successful(self) -> bool:
        return 200 <= self.status_code < 300


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Required environment variable is missing: {name}")
    return value


def parse_positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from exc
    if value < 1:
        raise RuntimeError(f"{name} must be at least 1")
    return value


def pad_row(row: list[Any], length: int = 11) -> list[Any]:
    return row[:length] + [""] * max(0, length - len(row))


def parse_attempts(value: Any) -> int:
    if value in (None, ""):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def parse_outbox_row(row_number: int, raw_row: list[Any]) -> OutboxMessage:
    row = pad_row(raw_row)
    return OutboxMessage(
        row_number=row_number,
        message_id=str(row[0]).strip(),
        created_at=str(row[1]).strip(),
        source=str(row[2]).strip(),
        title=str(row[3]).strip(),
        body=str(row[4]).strip(),
        priority=str(row[5]).strip().lower() or "default",
        status=str(row[6]).strip().upper(),
        attempts=parse_attempts(row[7]),
    )


def is_eligible(message: OutboxMessage, max_attempts: int) -> bool:
    return (
        message.has_producer_data
        and message.status in {"", "PENDING", "RETRY"}
        and message.attempts < max_attempts
    )


def load_google_credentials() -> Any:
    # Lazy imports keep the standalone ntfy smoke test independent of Google.
    from google.oauth2 import service_account

    raw_json = required_env("GOOGLE_SERVICE_ACCOUNT_JSON")
    try:
        service_account_info = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON") from exc

    return service_account.Credentials.from_service_account_info(
        service_account_info,
        scopes=SCOPES,
    )


def build_values_service() -> Any:
    from googleapiclient.discovery import build

    credentials = load_google_credentials()
    sheets = build("sheets", "v4", credentials=credentials, cache_discovery=False)
    return sheets.spreadsheets().values()


def google_error_status(exc: Exception) -> int | None:
    response = getattr(exc, "resp", None)
    status = getattr(response, "status", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def execute_google_request(
    request_factory: Callable[[], Any],
    *,
    operation: str,
    max_attempts: int,
) -> Any:
    for attempt in range(1, max_attempts + 1):
        try:
            return request_factory().execute()
        except Exception as exc:
            status = google_error_status(exc)
            retryable = status in RETRYABLE_GOOGLE_STATUS_CODES or isinstance(
                exc, (ConnectionError, TimeoutError, OSError)
            )
            if not retryable or attempt >= max_attempts:
                raise

            delay_seconds = min(2 ** (attempt - 1), 8)
            LOGGER.warning(
                "Google Sheets %s failed (attempt %s/%s, status=%s); retrying in %ss.",
                operation,
                attempt,
                max_attempts,
                status or "transport",
                delay_seconds,
            )
            time.sleep(delay_seconds)

    raise AssertionError("unreachable")


NTFY_TITLE_REPLACEMENTS = str.maketrans(
    {
        "ä": "ae",
        "ö": "oe",
        "ü": "ue",
        "Ä": "Ae",
        "Ö": "Oe",
        "Ü": "Ue",
        "ß": "ss",
        "–": "-",
        "—": "-",
        "„": '"',
        "“": '"',
        "”": '"',
        "’": "'",
        "\u00a0": " ",
    }
)


def safe_ntfy_title(title: str) -> str:
    return (
        title.translate(NTFY_TITLE_REPLACEMENTS)
        .encode("ascii", errors="replace")
        .decode("ascii")[:200]
    )


def ntfy_sequence_id(message_id: str) -> str:
    return hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:32]


def send_notification(
    *,
    title: str,
    body: str,
    priority: str,
    topic: str,
    base_url: str = DEFAULT_NTFY_BASE_URL,
    sequence_id: str | None = None,
) -> NtfyResult:
    url = f"{base_url.rstrip('/')}/{topic}"
    headers = {
        "Title": safe_ntfy_title(title),
        "Priority": priority,
        "Content-Type": "text/plain; charset=utf-8",
    }
    if sequence_id:
        headers["X-Sequence-ID"] = sequence_id

    response = requests.post(
        url,
        data=body.encode("utf-8"),
        headers=headers,
        timeout=(5, 20),
    )
    return NtfyResult(
        status_code=response.status_code,
        response_text=response.text[:500],
    )


def update_outbox_row(
    values_service: Any,
    spreadsheet_id: str,
    row_number: int,
    *,
    status: str,
    attempts: int,
    http_status: str,
    sent_at: str,
    error: str,
    google_max_attempts: int,
) -> None:
    execute_google_request(
        lambda: values_service.update(
            spreadsheetId=spreadsheet_id,
            range=f"NTFY_OUTBOX!G{row_number}:K{row_number}",
            valueInputOption="RAW",
            body={
                "values": [[status, attempts, http_status, sent_at, error[:500]]],
            },
        ),
        operation=f"status write row {row_number}",
        max_attempts=google_max_attempts,
    )


def read_outbox(
    values_service: Any,
    spreadsheet_id: str,
    *,
    google_max_attempts: int,
) -> list[OutboxMessage]:
    response = execute_google_request(
        lambda: values_service.get(
            spreadsheetId=spreadsheet_id,
            range=OUTBOX_RANGE,
        ),
        operation="outbox read",
        max_attempts=google_max_attempts,
    )
    rows = response.get("values", [])
    return [
        parse_outbox_row(row_number, row)
        for row_number, row in enumerate(rows, start=2)
    ]


def process_outbox() -> int:
    spreadsheet_id = required_env("SPREADSHEET_ID")
    topic = required_env("NTFY_TOPIC")
    base_url = os.getenv("NTFY_BASE_URL", DEFAULT_NTFY_BASE_URL).strip()
    max_attempts = parse_positive_int("MAX_ATTEMPTS", 3)
    max_messages = parse_positive_int("MAX_MESSAGES_PER_RUN", 10)
    google_max_attempts = parse_positive_int("GOOGLE_MAX_ATTEMPTS", 5)

    values_service = build_values_service()
    messages = read_outbox(
        values_service,
        spreadsheet_id,
        google_max_attempts=google_max_attempts,
    )
    populated = [message for message in messages if message.has_producer_data]
    eligible = [
        message for message in populated if is_eligible(message, max_attempts)
    ][:max_messages]

    LOGGER.info(
        "Outbox scan: populated=%s blank_status=%s eligible=%s.",
        len(populated),
        sum(message.status == "" for message in populated),
        len(eligible),
    )

    if not eligible:
        LOGGER.info("No pending ntfy messages found.")
        return 0

    delivered = 0
    failed = 0

    for message in eligible:
        attempts = message.attempts + 1
        now = datetime.now(timezone.utc).isoformat()

        if not message.message_id or not message.title or not message.body:
            update_outbox_row(
                values_service,
                spreadsheet_id,
                message.row_number,
                status="ERROR",
                attempts=attempts,
                http_status="",
                sent_at="",
                error="Missing mandatory field: message_id, title or body",
                google_max_attempts=google_max_attempts,
            )
            LOGGER.error("Row %s is missing mandatory fields.", message.row_number)
            failed += 1
            continue

        try:
            result = send_notification(
                title=message.title,
                body=message.body,
                priority=message.priority,
                topic=topic,
                base_url=base_url,
                sequence_id=ntfy_sequence_id(message.message_id),
            )
        except requests.RequestException as exc:
            final_status = "ERROR" if attempts >= max_attempts else "RETRY"
            update_outbox_row(
                values_service,
                spreadsheet_id,
                message.row_number,
                status=final_status,
                attempts=attempts,
                http_status="",
                sent_at="",
                error=str(exc),
                google_max_attempts=google_max_attempts,
            )
            LOGGER.exception(
                "ntfy request failed for message %s; status=%s",
                message.message_id,
                final_status,
            )
            failed += 1
            continue

        if result.successful:
            update_outbox_row(
                values_service,
                spreadsheet_id,
                message.row_number,
                status="SENT",
                attempts=attempts,
                http_status=str(result.status_code),
                sent_at=now,
                error="",
                google_max_attempts=google_max_attempts,
            )
            LOGGER.info(
                "Delivered message %s from %s with HTTP %s.",
                message.message_id,
                message.source or "unknown source",
                result.status_code,
            )
            delivered += 1
        else:
            final_status = "ERROR" if attempts >= max_attempts else "RETRY"
            update_outbox_row(
                values_service,
                spreadsheet_id,
                message.row_number,
                status=final_status,
                attempts=attempts,
                http_status=str(result.status_code),
                sent_at="",
                error=result.response_text or "ntfy returned a non-2xx response",
                google_max_attempts=google_max_attempts,
            )
            LOGGER.error(
                "ntfy rejected message %s with HTTP %s; status=%s",
                message.message_id,
                result.status_code,
                final_status,
            )
            failed += 1

    LOGGER.info(
        "Outbox run complete: eligible=%s delivered=%s failed=%s",
        len(eligible),
        delivered,
        failed,
    )
    return 1 if failed else 0


def smoke_test(title: str, body: str) -> int:
    topic = required_env("NTFY_TOPIC")
    base_url = os.getenv("NTFY_BASE_URL", DEFAULT_NTFY_BASE_URL).strip()
    try:
        result = send_notification(
            title=title,
            body=body,
            priority="default",
            topic=topic,
            base_url=base_url,
        )
    except requests.RequestException as exc:
        LOGGER.error("ntfy smoke test failed before an HTTP response: %s", exc)
        return 1

    if result.successful:
        LOGGER.info("ntfy smoke test successful: HTTP %s", result.status_code)
        return 0

    LOGGER.error(
        "ntfy smoke test failed: HTTP %s; response=%s",
        result.status_code,
        result.response_text,
    )
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deliver Jobs_Masterliste ntfy outbox messages."
    )
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="Send one static ntfy test without accessing Google Sheets.",
    )
    parser.add_argument(
        "--title",
        default="chatgpt: ntfy Test",
        help="Smoke-test title.",
    )
    parser.add_argument(
        "--body",
        default="chatgpt: ntfy-Test erfolgreich. Keine vertraulichen Inhalte.",
        help="Smoke-test body.",
    )
    return parser


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    args = build_parser().parse_args()

    try:
        if args.smoke_test:
            return smoke_test(args.title, args.body)
        return process_outbox()
    except Exception as exc:  # top-level guard so GitHub Actions fails visibly
        LOGGER.exception("Fatal ntfy bridge error: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
