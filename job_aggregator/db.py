"""
Minimal dedup + working-storage layer, backed by hosted Postgres
(Neon/Supabase free tier) so Lambda runs can share state across
invocations without a persistent local disk.

One table. Its only jobs:
  1. Answer "have we already seen this posting?" so a poll doesn't
     re-write duplicate rows to the Sheet.
  2. Hold the relevance score + tailored bullets for a posting long
     enough for the Sheet write to happen.
  3. Get pruned after ~45 days -- we are not building a history.

Connection string comes from the DATABASE_URL env var (set in .env
locally, set as a Lambda environment variable in prod).
"""

import os
from contextlib import contextmanager
from datetime import datetime, timedelta

import psycopg2

PRUNE_AFTER_DAYS = 45

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_postings (
    company_slug     TEXT NOT NULL,
    external_id      TEXT NOT NULL,
    profile_id       TEXT NOT NULL,
    title            TEXT NOT NULL,
    location         TEXT,
    url              TEXT NOT NULL,
    first_seen_at    TIMESTAMP NOT NULL DEFAULT NOW(),
    relevance_score  REAL,
    tailored_bullets TEXT,
    tailored_at      TIMESTAMP,
    written_to_sheet INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (company_slug, external_id, profile_id)
);
"""


@contextmanager
def get_conn(dsn: str | None = None):
    conn = psycopg2.connect(dsn or os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute(SCHEMA)
        conn.commit()
        yield conn
        conn.commit()
    finally:
        conn.close()


def has_seen(conn, company_slug: str, external_id: str, profile_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM seen_postings WHERE company_slug=%s AND external_id=%s AND profile_id=%s",
            (company_slug, external_id, profile_id),
        )
        return cur.fetchone() is not None


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
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO seen_postings
                (company_slug, external_id, profile_id, title, location, url, relevance_score)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (company_slug, external_id, profile_id) DO NOTHING
            """,
            (company_slug, external_id, profile_id, title, location, url, relevance_score),
        )


def save_tailored_bullets(conn, company_slug: str, external_id: str, profile_id: str, bullets: str):
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE seen_postings
            SET tailored_bullets = %s, tailored_at = NOW()
            WHERE company_slug=%s AND external_id=%s AND profile_id=%s
            """,
            (bullets, company_slug, external_id, profile_id),
        )


def mark_written_to_sheet(conn, company_slug: str, external_id: str, profile_id: str):
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE seen_postings SET written_to_sheet = 1
            WHERE company_slug=%s AND external_id=%s AND profile_id=%s
            """,
            (company_slug, external_id, profile_id),
        )


def prune_old(conn, days: int = PRUNE_AFTER_DAYS):
    cutoff = datetime.utcnow() - timedelta(days=days)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM seen_postings WHERE first_seen_at < %s", (cutoff,))
