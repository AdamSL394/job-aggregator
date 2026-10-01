import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from job_aggregator.db import get_conn, has_seen, insert_posting, prune_old
from job_aggregator.ats_clients.base import NormalizedPosting
from job_aggregator.profiles import Profile
from job_aggregator.scoring import score_posting


def test_dedup_roundtrip():
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        with get_conn(tmp.name) as conn:
            assert not has_seen(conn, "acme", "1", "adam")
            insert_posting(conn, "acme", "1", "adam", "Backend Engineer", "Remote", "http://x.com")
            assert has_seen(conn, "acme", "1", "adam")


def test_prune_removes_old_rows_only():
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        with get_conn(tmp.name) as conn:
            insert_posting(conn, "acme", "old", "adam", "Old Job", "Remote", "http://x.com")
            insert_posting(conn, "acme", "new", "adam", "New Job", "Remote", "http://y.com")
            conn.execute(
                "UPDATE seen_postings SET first_seen_at = datetime('now', '-60 days') WHERE external_id='old'"
            )
            prune_old(conn, days=45)
            assert not has_seen(conn, "acme", "old", "adam")
            assert has_seen(conn, "acme", "new", "adam")


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
