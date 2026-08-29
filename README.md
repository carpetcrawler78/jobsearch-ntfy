# jobsearch-ntfy

External notification bridge for the private job-search pipeline.

## Architecture

1. ChatGPT schedulers write sanitized notifications to `Jobs_Masterliste → NTFY_OUTBOX!A:F`.
2. GitHub Actions polls the outbox every five minutes.
3. `src/main.py` treats a populated row with blank `G` as pending and also processes explicit `PENDING` or `RETRY` states.
4. The worker writes `SENT`, `RETRY`, `ERROR`, attempt count, HTTP status and timestamp to `G:K`.

ChatGPT never calls `ntfy.sh` directly. Producer and worker ownership do not overlap.

## Outbox schema

`NTFY_OUTBOX!A:K`

| Column | Field | Owner |
|---|---|---|
| A | message_id | Producer |
| B | created_at | Producer |
| C | source | Producer |
| D | title | Producer |
| E | body | Producer |
| F | priority | Producer |
| G | status | Worker |
| H | attempts | Worker |
| I | http_status | Worker |
| J | sent_at | Worker |
| K | error | Worker |

Blank `G` is the normal initial state. `SKIPPED_STALE` is a terminal migration state for legacy notifications that must remain in history but must not be delivered later.

## Required GitHub Actions secrets

Repository → **Settings → Secrets and variables → Actions**:

- `NTFY_TOPIC`: preferably a new long random topic.
- `GOOGLE_SERVICE_ACCOUNT_JSON`: complete JSON key for a dedicated Google service account.

Do not store either value in the repository.

## Google setup

1. Create or select a Google Cloud project.
2. Enable the Google Sheets API.
3. Create a dedicated service account and JSON key.
4. Add the JSON as `GOOGLE_SERVICE_ACCOUNT_JSON`.
5. Share `Jobs_Masterliste` with the service-account email as **Editor**.

The spreadsheet ID is already configured in the workflow.

## First test

1. Open **Actions → Ntfy Outbox → Run workflow**.
2. Select `smoke`.
3. The workflow sends exactly one safe test and fails visibly for DNS, timeout, missing secret, or non-2xx HTTP status.
4. Then run `outbox` once. With an empty queue it should finish successfully with `No pending ntfy messages found.`

Scheduled runs process the outbox automatically.

## Retry and idempotency behavior

- Eligible states: blank, `PENDING`, `RETRY`.
- Blank grid rows and terminal states are ignored.
- Maximum ntfy delivery attempts: 3.
- Google Sheets reads and status writes retry retryable transport/HTTP failures up to 5 times with exponential backoff.
- Any ntfy HTTP 2xx is success.
- Network errors and non-2xx responses become `RETRY`, then `ERROR` after the final attempt.
- A failed delivery or exhausted Sheets operation makes GitHub Actions fail visibly.
- A deterministic ntfy sequence ID derived from `message_id` prevents client-side duplicate notifications when delivery succeeds but the following Sheet write has to be retried.
- `concurrency` prevents overlapping scheduled/manual runs in this repository.

## Security

The previous topic name was committed in public workflow history and should be considered discoverable. Create a new long random topic, subscribe the ntfy app to it, and save it only as the `NTFY_TOPIC` secret.

Only category counts and generic workflow states belong in `NTFY_OUTBOX`. Do not write names, senders, subjects, companies, job titles, links, amounts, contract information, deadlines, or application-file details.

Official references:

- https://docs.ntfy.sh/publish/
- https://docs.github.com/actions/using-workflows/events-that-trigger-workflows#schedule
- https://developers.google.com/sheets/api
