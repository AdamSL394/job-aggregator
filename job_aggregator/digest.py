"""
One email per profile, summarizing that day's highest-scoring new matches.

Called from run_poll.py only from the branch where today's full company-list
pass just finished (not on every 15-minute invocation) -- that branch only
runs once per calendar day, so this naturally sends at most one digest per
day per profile with no extra dedup bookkeeping needed.

Uses stdlib smtplib against Gmail's SMTP (or any SMTP server) -- no paid
service, consistent with the rest of this project's $0 budget. Requires a
Gmail "app password" (not your real Gmail password) if using Gmail; set via
SMTP_USER / SMTP_PASS env vars (Lambda environment variables in prod).

No separate score threshold here on purpose -- it reuses the profile's own
min_score (profiles.py), the same number that gates the Sheet write.
Don't filter on seen_postings.written_to_sheet instead -- nothing ever
sets that column, so it's always 0.
"""

import os
import smtplib
from email.mime.text import MIMEText

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASS = os.environ.get("SMTP_PASS")


def send_daily_digest(conn, profiles, today):
    """For each profile with a digest_email set, email that profile's
    seen_postings rows written to the Sheet today. Never raises -- a
    digest failure should not affect run_state at all, since run_poll.py
    calls this after completion is already saved."""
    if not SMTP_USER or not SMTP_PASS:
        print("WARNING: SMTP_USER/SMTP_PASS not set -- skipping email digest")
        return

    for profile_id, profile in profiles.items():
        to_addr = getattr(profile, "digest_email", "")
        if not to_addr:
            continue
        try:
            _send_one(conn, profile_id, to_addr, today, profile.min_score)
        except Exception as e:
            print(f"WARNING: digest email failed for {profile_id}: {e}")


def _send_one(conn, profile_id, to_addr, today, min_score):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT title, company_slug, url, relevance_score
            FROM seen_postings
            WHERE profile_id = %s
              AND first_seen_at::date = %s
              AND relevance_score >= %s - 0.0001
            ORDER BY relevance_score DESC
            """,
            (profile_id, today, min_score),
        )
        rows = cur.fetchall()

    if not rows:
        print(f"{profile_id}: no matches written today -- no digest email sent")
        return

    lines = [
        f"{round(score * 100)} -- {title} ({slug})\n{url}"
        for title, slug, url, score in rows
    ]
    body = f"{len(rows)} new match(es) today:\n\n" + "\n\n".join(lines)

    msg = MIMEText(body)
    msg["Subject"] = f"job-aggregator: {len(rows)} match(es) today ({today})"
    msg["From"] = SMTP_USER
    msg["To"] = to_addr

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
        smtp.starttls()
        smtp.login(SMTP_USER, SMTP_PASS)
        smtp.send_message(msg)
    print(f"{profile_id}: sent digest email to {to_addr} ({len(rows)} match(es))")
