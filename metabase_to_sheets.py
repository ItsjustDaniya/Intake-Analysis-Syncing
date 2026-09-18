#!/usr/bin/env python3
"""
Sync two Metabase saved questions (Attendance + Assignments) into a
Google Sheet, on a schedule (run from GitHub Actions cron or any other
scheduler).

Every run does a FULL OVERWRITE of each target tab: it clears the tab
and rewrites it with the question's current full result set (no Batch /
Date / etc. filters are sent, so Metabase returns every row — this
mirrors the "no filter" behavior of the report's own Field Filter
widgets). This is intentional: both questions are cumulative snapshots
("attendance so far", "assignment completion so far"), not event logs,
so overwriting avoids duplicate/stale rows.

Required environment variables (set these as GitHub Actions secrets):

    METABASE_URL              e.g. https://metabase-lierhfgoeiwhr.newtonschool.co
    METABASE_API_KEY          a Metabase API key (Admin > Settings > Authentication > API Keys)
    GOOGLE_SERVICE_ACCOUNT_JSON   the FULL contents of the service account JSON key file
    SPREADSHEET_ID             the Google Sheet ID
                               (from the sheet URL: /d/<SPREADSHEET_ID>/edit)

Optional:
    ATTENDANCE_QUESTION_ID     defaults to 3608
    ASSIGNMENTS_QUESTION_ID    defaults to 7939
    ATTENDANCE_SHEET_NAME      defaults to "Attendance"
    ASSIGNMENTS_SHEET_NAME     defaults to "Assignment"

Remember to share the target Google Sheet with the service account's
client_email (found inside the JSON key) as an Editor.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any

import gspread
import requests
from google.oauth2.service_account import Credentials

# ---------------------------------------------------------------------------
# Config (env-driven — nothing sensitive is hard-coded)
# ---------------------------------------------------------------------------

METABASE_URL = os.environ["METABASE_URL"].rstrip("/")
METABASE_API_KEY = os.environ["METABASE_API_KEY"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]

ATTENDANCE_QUESTION_ID = int(os.environ.get("ATTENDANCE_QUESTION_ID", "3608"))
ASSIGNMENTS_QUESTION_ID = int(os.environ.get("ASSIGNMENTS_QUESTION_ID", "7939"))

ATTENDANCE_SHEET_NAME = os.environ.get("ATTENDANCE_SHEET_NAME", "Attendance")
ASSIGNMENTS_SHEET_NAME = os.environ.get("ASSIGNMENTS_SHEET_NAME", "Assignment")

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
]

REQUEST_TIMEOUT = 120  # seconds — these queries scan a lot of rows
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 10


# ---------------------------------------------------------------------------
# Metabase
# ---------------------------------------------------------------------------

def fetch_metabase_question(question_id: int) -> tuple[list[str], list[list[Any]]]:
    """Run a saved Metabase question via the REST API and return
    (column_names, rows). No parameters are sent, so every Field Filter
    in the question (Batch, Date, UserID, ...) is left unset and
    Metabase returns the full, unfiltered result set."""

    url = f"{METABASE_URL}/api/card/{question_id}/query"
    headers = {
        "x-api-key": METABASE_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {"parameters": []}

    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            body = resp.json()
            break
        except Exception as exc:  # noqa: BLE001 - we want to retry on anything and re-raise at the end
            last_error = exc
            print(f"[metabase] question {question_id}: attempt {attempt} failed: {exc}", file=sys.stderr)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    else:
        raise RuntimeError(f"Metabase query for question {question_id} failed after {MAX_RETRIES} attempts") from last_error

    if body.get("status") == "failed":
        raise RuntimeError(f"Metabase question {question_id} returned an error: {body.get('error')}")

    data = body["data"]
    columns = [col.get("display_name") or col.get("name") for col in data["cols"]]
    rows = data["rows"]
    return columns, rows


def stringify_row(row: list[Any]) -> list[Any]:
    """Make a row safe to hand to gspread: keep numbers/bools as-is,
    stringify everything else (dates, None, nested objects)."""
    out = []
    for value in row:
        if value is None:
            out.append("")
        elif isinstance(value, (int, float, bool)):
            out.append(value)
        else:
            out.append(str(value))
    return out


# ---------------------------------------------------------------------------
# Google Sheets
# ---------------------------------------------------------------------------

def get_sheets_client() -> gspread.Client:
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(info, scopes=GOOGLE_SCOPES)
    return gspread.authorize(creds)


def get_or_create_worksheet(spreadsheet: gspread.Spreadsheet, title: str) -> gspread.Worksheet:
    try:
        return spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        print(f"[sheets] worksheet '{title}' not found — creating it")
        return spreadsheet.add_worksheet(title=title, rows=100, cols=26)


def overwrite_worksheet(worksheet: gspread.Worksheet, columns: list[str], rows: list[list[Any]]) -> None:
    worksheet.clear()
    values = [columns] + [stringify_row(r) for r in rows]
    worksheet.update(values, value_input_option="RAW")
    # Freeze the header row for readability.
    worksheet.freeze(rows=1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def sync_question_to_sheet(spreadsheet: gspread.Spreadsheet, question_id: int, sheet_name: str, label: str) -> int:
    print(f"[{label}] fetching Metabase question {question_id} ...")
    columns, rows = fetch_metabase_question(question_id)
    print(f"[{label}] got {len(rows)} rows, {len(columns)} columns")

    worksheet = get_or_create_worksheet(spreadsheet, sheet_name)
    overwrite_worksheet(worksheet, columns, rows)
    print(f"[{label}] wrote {len(rows)} rows to tab '{sheet_name}'")
    return len(rows)


def main() -> None:
    started_at = datetime.now(timezone.utc).isoformat()
    print(f"[sync] starting run at {started_at}")

    client = get_sheets_client()
    spreadsheet = client.open_by_key(SPREADSHEET_ID)

    attendance_rows = sync_question_to_sheet(
        spreadsheet, ATTENDANCE_QUESTION_ID, ATTENDANCE_SHEET_NAME, "attendance"
    )
    assignment_rows = sync_question_to_sheet(
        spreadsheet, ASSIGNMENTS_QUESTION_ID, ASSIGNMENTS_SHEET_NAME, "assignments"
    )

    print(
        f"[sync] done. attendance_rows={attendance_rows} assignment_rows={assignment_rows} "
        f"finished_at={datetime.now(timezone.utc).isoformat()}"
    )


if __name__ == "__main__":
    main()
