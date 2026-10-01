"""
Minimal dedup + working-storage layer.

One table. Its only jobs:
  1. Answer "have we already seen this posting?" so a poll doesn't
     re-write duplicate rows to the Sheet.
  2. Hold the relevance score + tailored bullets for a posting long
     enough for the Sheet write to happen.
  3. Get pruned after ~45 days — we are not building a history.
"""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(_BASE_DIR, "data", "jobs.db")
PRUNE_AFTER_DAYS = 45

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_postings (
    company_slug     TEXT NOT NULL,
    external_id      TEXT NOT NULL,
    profile_id       TEXT NOT NULL,
    title            TEXT NOT NULL,
    location         TEXT,
    url              TEXT NOT NULL,
    first_seen_at    TIMESTAMP NOT NULL DEFAULT (datetime('now')),
    relevance_score  REAL,
    tailored_bullets TEXT,
    tailored_at      TIMESTAMP,
    written_to_sheet INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (company_slug, external_id, profile_id)
);
"""


@contextmanager
def get_conn(db_path: str = DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def has_seen(conn, company_slug: str, external_id: str, profile_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM seen_postings WHERE company_slug=? AND external_id=? AND profile_id=?",
        (company_slug, external_id, profile_id),
    ).fetchone()
    return row is not None


def insert_posting(
    conn,
    company_slug: str,
    external_id: str,
    profile_id: str,
    title: str,
    location: str | None,
    url: str,
    relevance_score: float | None = None,
):
    conn.execute(
        """
        INSERT OR IGNORE INTO seen_postings
            (company_slug, external_id, profile_id, title, location, url, relevance_score)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (company_slug, external_id, profile_id, title, location, url, relevance_score),
    )


def save_tailored_bullets(conn, company_slug: str, external_id: str, profile_id: str, bullets: str):
    conn.execute(
        """
        UPDATE seen_postings
        SET tailored_bullets = ?, tailored_at = datetime('now')
        WHERE company_slug=? AND external_id=? AND profile_id=?
        """,
        (bullets, company_slug, external_id, profile_id),
    )


def mark_written_to_sheet(conn, company_slug: str, external_id: str, profile_id: str):
    conn.execute(
        """
        UPDATE seen_postings SET written_to_sheet = 1
        WHERE company_slug=? AND external_id=? AND profile_id=?
        """,
        (company_slug, external_id, profile_id),
    )


def prune_old(conn, days: int = PRUNE_AFTER_DAYS):
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("DELETE FROM seen_postings WHERE first_seen_at < ?", (cutoff,))
