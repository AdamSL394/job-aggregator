import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from job_aggregator.db import get_conn, has_seen, insert_posting, prune_old
from job_aggregator.ats_clients.base import NormalizedPosting
from job_aggregator.profiles import Profile
from job_aggregator.scoring import score_posting

HAS_DB = bool(os.environ.get("DATABASE_URL"))


def test_dedup_roundtrip():
    if not HAS_DB:
        print("skip test_dedup_roundtrip: DATABASE_URL not set")
        return
    with get_conn() as conn:
        try:
            assert not has_seen(conn, "test-fixture", "1", "test-fixture")
            insert_posting(conn, "test-fixture", "1", "test-fixture", "Backend Engineer", "Remote", "http://x.com")
            assert has_seen(conn, "test-fixture", "1", "test-fixture")
        finally:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM seen_postings WHERE company_slug='test-fixture'")


def test_prune_removes_old_rows_only():
    if not HAS_DB:
        print("skip test_prune_removes_old_rows_only: DATABASE_URL not set")
        return
    with get_conn() as conn:
        try:
            insert_posting(conn, "test-fixture", "old", "test-fixture", "Old Job", "Remote", "http://x.com")
            insert_posting(conn, "test-fixture", "new", "test-fixture", "New Job", "Remote", "http://y.com")
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE seen_postings SET first_seen_at = NOW() - INTERVAL '60 days' "
                    "WHERE company_slug='test-fixture' AND external_id='old'"
                )
            prune_old(conn, days=45)
            assert not has_seen(conn, "test-fixture", "old", "test-fixture")
            assert has_seen(conn, "test-fixture", "new", "test-fixture")
        finally:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM seen_postings WHERE company_slug='test-fixture'")


def test_scoring_matches_target_title():
    posting = NormalizedPosting("1", "Backend Software Engineer", "Remote", "http://x.com", {})
    profile = Profile(
        profile_id="test",
        resume_text="...",
        target_titles=["backend engineer", "software engineer"],
        sheet_id="x",
    )
    assert score_posting(posting, profile) > 0


def test_scoring_excludes_unwanted_titles():
    posting = NormalizedPosting("1", "Demo Engineer", "Remote", "http://x.com", {})
    profile = Profile(profile_id="test", resume_text="...", target_titles=["engineer"], sheet_id="x")
    assert score_posting(posting, profile) == 0.0


if __name__ == "__main__":
    test_dedup_roundtrip()
    test_prune_removes_old_rows_only()
    test_scoring_matches_target_title()
    test_scoring_excludes_unwanted_titles()
    print("all tests passed")
