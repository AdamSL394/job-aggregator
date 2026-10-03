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

A single run can take 20-30+ minutes (ATS fetch delays + LLM rate
limiting), with long stretches of zero DB traffic in between (walking
through "board not found" skips touches no DB row at all). Neon's free
tier fronts Postgres with a pooler that silently closes idle sockets --
long enough stretches of inactivity and the next query fails with
OperationalError/InterfaceError, not a normal response. _ReconnectingConn
below reconnects once and retries transparently so that's invisible in
the common case, instead of crashing the whole run over one dropped
connection.
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

-- Simple key/value store for resumable-run state (Lambda's 900s hard
-- timeout means one pass through the company list takes many
-- invocations; this is how one invocation tells the next where it
-- left off, and how we know a given day's full pass is already done).
CREATE TABLE IF NOT EXISTS run_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class _ReconnectingConn:
    """Duck-types a psycopg2 connection (.cursor() / .commit() / .close())
    but transparently reconnects once if the underlying socket was
    silently dropped. Keeps has_seen()/insert_posting()/etc. completely
    unchanged -- they just call conn.cursor()/conn.commit() as before."""

    def __init__(self, dsn: str):
        self._dsn = dsn
        self._conn = self._connect()

    def _connect(self):
        return psycopg2.connect(
            self._dsn,
            keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=5,
        )

    def cursor(self):
        return self._conn.cursor()

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()

    def _hard_reconnect(self):
        try:
            self._conn.close()
        except Exception:
            pass
        self._conn = self._connect()


def _retrying(conn: "_ReconnectingConn", work):
    """Run work() (a closure doing cursor work against conn), reconnecting
    once and retrying if the connection was silently dropped."""
    try:
        return work()
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        conn._hard_reconnect()
        return work()


@contextmanager
def get_conn(dsn: str | None = None):
    conn = _ReconnectingConn(dsn or os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute(SCHEMA)
        conn.commit()
        yield conn
        conn.commit()
    finally:
        conn.close()


def has_seen(conn, company_slug: str, external_id: str, profile_id: str) -> bool:
    def work():
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM seen_postings WHERE company_slug=%s AND external_id=%s AND profile_id=%s",
                (company_slug, external_id, profile_id),
            )
            return cur.fetchone() is not None
    return _retrying(conn, work)


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
    def work():
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
    _retrying(conn, work)


def save_tailored_bullets(conn, company_slug: str, external_id: str, profile_id: str, bullets: str):
    def work():
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE seen_postings
                SET tailored_bullets = %s, tailored_at = NOW()
                WHERE company_slug=%s AND external_id=%s AND profile_id=%s
                """,
                (bullets, company_slug, external_id, profile_id),
            )
    _retrying(conn, work)


def mark_written_to_sheet(conn, company_slug: str, external_id: str, profile_id: str):
    def work():
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE seen_postings SET written_to_sheet = 1
                WHERE company_slug=%s AND external_id=%s AND profile_id=%s
                """,
                (company_slug, external_id, profile_id),
            )
    _retrying(conn, work)


def get_state(conn, key: str) -> str | None:
    def work():
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM run_state WHERE key=%s", (key,))
            row = cur.fetchone()
            return row[0] if row else None
    return _retrying(conn, work)


def set_state(conn, key: str, value: str):
    def work():
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO run_state (key, value) VALUES (%s, %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                """,
                (key, value),
            )
        conn.commit()  # commit immediately -- this is our resume point,
                        # it must survive even if the process is killed
                        # (e.g. hitting the Lambda timeout) right after this
    _retrying(conn, work)


LOCK_KEY = "lock_expires_at"


def try_acquire_lock(conn, ttl_seconds: int) -> bool:
    """Claim the single-run lock, atomically. Returns True if this call got
    it, False if another invocation already holds an unexpired lock.

    Deliberately NOT a Postgres advisory lock (pg_advisory_lock) -- those
    are tied to the current session/backend connection, which breaks under
    connection-pooling modes that can hand out a different backend
    connection per statement (exactly the kind of pooling Neon's free tier
    fronts Postgres with). This uses a single atomic UPDATE/INSERT
    instead, which only depends on row contents, not which physical
    connection ran it -- safe under any pooling mode.

    TTL-based rather than released-on-connection-close: if an invocation
    gets hard-killed (crash, Lambda force-terminating past our own
    time-budget check) and never calls release_lock(), the lock still
    self-expires after ttl_seconds instead of deadlocking every future
    invocation forever.
    """
    now = datetime.utcnow()
    now_iso = now.isoformat()
    expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat()

    def work():
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO run_state (key, value) VALUES (%s, %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                WHERE run_state.value < %s
                """,
                (LOCK_KEY, expires_at, now_iso),
            )
            acquired = cur.rowcount == 1
        conn.commit()
        return acquired
    return _retrying(conn, work)


def release_lock(conn):
    """Free the lock immediately so the next invocation doesn't have to
    wait out the TTL. Safe to call even if this invocation never held it."""
    def work():
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE run_state SET value = %s WHERE key = %s",
                ("1970-01-01T00:00:00", LOCK_KEY),
            )
        conn.commit()
    _retrying(conn, work)


def prune_old(conn, days: int = PRUNE_AFTER_DAYS):
    cutoff = datetime.utcnow() - timedelta(days=days)
    def work():
        with conn.cursor() as cur:
            cur.execute("DELETE FROM seen_postings WHERE first_seen_at < %s", (cutoff,))
    _retrying(conn, work)
