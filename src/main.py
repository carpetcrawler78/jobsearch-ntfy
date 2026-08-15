from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests

LOGGER = logging.getLogger("jobsearch_ntfy")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
OUTBOX_RANGE = os.getenv("OUTBOX_RANGE", "NTFY_OUTBOX!A2:K1000")
DEFAULT_NTFY_BASE_URL = "https://ntfy.sh"


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

NTFY_TITLE_REPLACEMENTS = str.maketrans({
    "ä": "ae", "ö": "oe", "ü": "ue",
    "Ä": "Ae", "Ö": "Oe", "Ü": "Ue",
    "ß": "ss",
    "–": "-", "—": "-",
    "„": '"', "“": '"', "”": '"',
    "’": "'", "\u00a0": " ",
})


def safe_ntfy_title(title: str) -> str:
    return (
        title.translate(NTFY_TITLE_REPLACEMENTS)
        .encode("ascii", errors="replace")
        .decode("ascii")[:200]
    )

def send_notification(
    *,
    title: str,
    body: str,
    priority: str,
    topic: str,
    base_url: str = DEFAULT_NTFY_BASE_URL,
) -> NtfyResult:
    url = f"{base_url.rstrip('/')}/{topic}"
    response = requests.post(
        url,
        data=body.encode("utf-8"),
        headers={
            "Title": safe_ntfy_title(title),
            "Priority": priority,
            "Content-Type": "text/plain; charset=utf-8",
        },
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
) -> None:
    values_service.update(
        spreadsheetId=spreadsheet_id,
        range=f"NTFY_OUTBOX!G{row_number}:K{row_number}",
        valueInputOption="RAW",
        body={
            "values": [[status, attempts, http_status, sent_at, error[:500]]],
        },
    ).execute()


def read_outbox(values_service: Any, spreadsheet_id: str) -> list[OutboxMessage]:
    response = values_service.get(
        spreadsheetId=spreadsheet_id,
        range=OUTBOX_RANGE,
    ).execute()
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

    values_service = build_values_service()
    messages = read_outbox(values_service, spreadsheet_id)
    eligible = [
        message
        for message in messages
        if message.status in {"PENDING", "RETRY"}
        and message.attempts < max_attempts
    ][:max_messages]

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
