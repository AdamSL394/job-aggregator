"""
Sheet is the review surface. One sheet per profile. Columns:
Title | Company | Tier | Date Added | Score | Status | Why | Tailored bullets | URL

Status is a dropdown (New / Applied / Not Applicable / Skipped), set up once
when the sheet is first created. It's read back on each run so applied/
skipped postings don't get re-surfaced as if new -- the Sheet becomes the
durable "what did I already decide about this" record, on top of the DB's
dedup role.
"""

import gspread
from gspread.utils import ValidationConditionType
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
HEADER = ["Title", "Company", "Tier", "Date Added", "Score", "Status", "Why", "Tailored bullets", "URL"]
STATUS_OPTIONS = ["New", "Applied", "Not Applicable", "Skipped"]
STATUS_COLUMN_LETTER = "F"  # Title=A, Company=B, Tier=C, Date Added=D, Score=E, Status=F
URL_COLUMN_INDEX = 9         # 1-indexed: last column


def get_worksheet(sheet_id: str, worksheet_name: str, creds_path: str = "service_account.json"):
    creds = Credentials.from_service_account_file(creds_path, scopes=SCOPES)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(sheet_id)
    try:
        ws = sheet.worksheet(worksheet_name)
    except gspread.WorksheetNotFound:
        # sheet has other tabs already (tracker, applications, etc) --
        # add a dedicated one for new postings rather than touching those
        ws = sheet.add_worksheet(title=worksheet_name, rows=1000, cols=len(HEADER))

    if ws.row_values(1) != HEADER:
        ws.update("A1", [HEADER])
        # one-time setup: give Status a real dropdown instead of free text.
        # Covers rows 2-1000 up front so future appended rows are already
        # validated without re-running this on every call.
        ws.add_validation(
            f"{STATUS_COLUMN_LETTER}2:{STATUS_COLUMN_LETTER}1000",
            ValidationConditionType.one_of_list,
            STATUS_OPTIONS,
            showCustomUi=True,
        )
    return ws


def append_postings(ws, rows: list[list]):
    """rows: list of [title, company, tier, date_added, score, status, why, tailored_bullets, url].
    One API call for many rows -- avoids the per-row write-quota error
    you'd hit calling append_row in a loop over a large shard."""
    if rows:
        ws.append_rows(rows, value_input_option="RAW")


def get_existing_urls(ws) -> set[str]:
    # cheap guard against double-appends if a run is interrupted mid-write
    return set(ws.col_values(URL_COLUMN_INDEX)[1:])  # skip header
