#!/usr/bin/env python3
"""
Sync data into a Google Sheet, on a schedule (run from GitHub Actions
cron or any other scheduler). Two independent sync jobs run each time:

1. METABASE -> SHEET (Attendance + Assignments)
   Pulls two Metabase saved questions and full-overwrites their tabs.

2. SHEET -> SHEET (Student-level-MC -> Contest)
   Copies the "Student-level-MC" tab from a source spreadsheet into a
   "Contest" tab on the destination spreadsheet, using the Sheets API
   directly (gspread) instead of IMPORTRANGE. IMPORTRANGE fails with
   "Result too large" once the pulled range gets big enough (~10M
   cells across the whole formula, but big/wide sheets hit it well
   before that); reading the values via the API and writing them with
   the same batched approach used for the Metabase sync sidesteps that
   limit completely — the API has no such formula-result cap.

Every run does a FULL OVERWRITE of each target tab: it clears the tab
and rewrites it with the source's current full data (cumulative
snapshots, not event logs, so overwriting avoids duplicate/stale rows).

Required environment variables (set these as GitHub Actions secrets):

    METABASE_URL              e.g. https://metabase-lierhfgoeiwhr.newtonschool.co
    METABASE_API_KEY          a Metabase API key (Admin > Settings > Authentication > API Keys)
    GOOGLE_SERVICE_ACCOUNT_JSON   the FULL contents of the service account JSON key file
    SPREADSHEET_ID             the DESTINATION Google Sheet ID
                               (from the sheet URL: /d/<SPREADSHEET_ID>/edit)

Optional:
    ATTENDANCE_QUESTION_ID     defaults to 3608
    ASSIGNMENTS_QUESTION_ID    defaults to 7939
    ATTENDANCE_SHEET_NAME      defaults to "Attendance"
    ASSIGNMENTS_SHEET_NAME     defaults to "Assignment"

    SOURCE_SPREADSHEET_ID      defaults to 1I4HAAkbZl2Zr6IblLRh1AasfBj1LbZCj0bQ-X1wLGFM
    SOURCE_SHEET_NAME          defaults to "Student-level-MC"
    CONTEST_SHEET_NAME         defaults to "Contest"

Remember to share BOTH the destination Google Sheet and the source
Google Sheet with the service account's client_email (found inside the
JSON key) as an Editor (the source only strictly needs Viewer, but
Editor avoids surprises).
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

# Sheet -> Sheet sync (replaces the IMPORTRANGE formula)
SOURCE_SPREADSHEET_ID = os.environ.get(
    "SOURCE_SPREADSHEET_ID", "1I4HAAkbZl2Zr6IblLRh1AasfBj1LbZCj0bQ-X1wLGFM"
)
SOURCE_SHEET_NAME = os.environ.get("SOURCE_SHEET_NAME", "Student-level-MC")
CONTEST_SHEET_NAME = os.environ.get("CONTEST_SHEET_NAME", "Contest")

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
    Metabase returns the full, unfiltered result set.

    IMPORTANT: this deliberately calls the `/query/json` *export*
    endpoint, not the plain `/query` endpoint. The plain endpoint is
    Metabase's interactive query path (the one the "run this question"
    button in the UI hits) and it silently truncates results to a low
    default row cap (commonly ~2000 rows) with no error — rows past
    that cap (e.g. alphabetically later students, once a question has
    thousands of rows) just never come back. The `/query/json` export
    endpoint is meant for full downloads and is not subject to that
    interactive cap (Metabase's export row limit is far higher, ~1M+),
    so it's the one to use for anything that needs the complete result
    set, like this sync.
    """

    url = f"{METABASE_URL}/api/card/{question_id}/query/json"
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
            records = resp.json()
            break
        except Exception as exc:  # noqa: BLE001 - we want to retry on anything and re-raise at the end
            last_error = exc
            print(f"[metabase] question {question_id}: attempt {attempt} failed: {exc}", file=sys.stderr)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    else:
        raise RuntimeError(f"Metabase query for question {question_id} failed after {MAX_RETRIES} attempts") from last_error

    if isinstance(records, dict) and records.get("status") == "failed":
        raise RuntimeError(f"Metabase question {question_id} returned an error: {records.get('error')}")

    if not records:
        return [], []

    # The JSON export returns a flat list of row objects, e.g.
    # [{"course_id": 123, "user_id": 197114, ...}, ...] with every
    # column present (as null where empty) on every row, in column
    # order — so the first row's keys give us the column order.
    columns = list(records[0].keys())
    rows = [[record.get(col) for col in columns] for record in records]
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


def overwrite_worksheet(worksheet: gspread.Worksheet, values: list[list[Any]]) -> None:
    """Clear the tab and write `values` (header row included) back to
    it in as few API calls as gspread's batching allows, then freeze
    the header row."""
    worksheet.clear()
    if values:
        worksheet.update(values, value_input_option="RAW")
    worksheet.freeze(rows=1)


# ---------------------------------------------------------------------------
# Sync jobs
# ---------------------------------------------------------------------------

def sync_question_to_sheet(spreadsheet: gspread.Spreadsheet, question_id: int, sheet_name: str, label: str) -> int:
    print(f"[{label}] fetching Metabase question {question_id} ...")
    columns, rows = fetch_metabase_question(question_id)
    print(f"[{label}] got {len(rows)} rows, {len(columns)} columns")

    worksheet = get_or_create_worksheet(spreadsheet, sheet_name)
    values = [columns] + [stringify_row(r) for r in rows]
    overwrite_worksheet(worksheet, values)
    print(f"[{label}] wrote {len(rows)} rows to tab '{sheet_name}'")
    return len(rows)


def sync_sheet_to_sheet(
    client: gspread.Client,
    source_spreadsheet_id: str,
    source_sheet_name: str,
    destination_spreadsheet: gspread.Spreadsheet,
    destination_sheet_name: str,
) -> int:
    """Read every populated cell of `source_sheet_name` (from the
    source spreadsheet) and full-overwrite `destination_sheet_name` on
    `destination_spreadsheet` with it — this is the sheet-to-sheet
    equivalent of an IMPORTRANGE, but pulled via the Sheets API so it
    never hits IMPORTRANGE's "Result too large" ceiling."""

    label = "contest-sync"
    print(f"[{label}] opening source spreadsheet {source_spreadsheet_id} ...")
    source_spreadsheet = client.open_by_key(source_spreadsheet_id)
    source_worksheet = source_spreadsheet.worksheet(source_sheet_name)

    print(f"[{label}] reading '{source_sheet_name}' ...")
    # get_all_values() returns only the used range (no trailing empty
    # columns/rows padded out to the sheet's full grid size), which
    # keeps this well clear of any size limits.
    values = source_worksheet.get_all_values()
    row_count = max(len(values) - 1, 0)  # minus header
    col_count = len(values[0]) if values else 0
    print(f"[{label}] got {row_count} data rows, {col_count} columns")

    destination_worksheet = get_or_create_worksheet(destination_spreadsheet, destination_sheet_name)
    overwrite_worksheet(destination_worksheet, values)
    print(f"[{label}] wrote {row_count} rows to tab '{destination_sheet_name}'")
    return row_count


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

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
    contest_rows = sync_sheet_to_sheet(
        client, SOURCE_SPREADSHEET_ID, SOURCE_SHEET_NAME, spreadsheet, CONTEST_SHEET_NAME
    )

    print(
        f"[sync] done. attendance_rows={attendance_rows} assignment_rows={assignment_rows} "
        f"contest_rows={contest_rows} finished_at={datetime.now(timezone.utc).isoformat()}"
    )


if __name__ == "__main__":
    main()
