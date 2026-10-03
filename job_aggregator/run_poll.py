"""
Entry point. Run this from local cron (Option A):
    0 8 * * * cd /path/to/job-aggregator && python -m job_aggregator.run_poll

Or invoke repeatedly from Lambda (Option B) -- see TIME_BUDGET_SECONDS below:
each invocation processes as much of today's company list as it can inside
a bounded time budget, then saves its place in Postgres (run_state table)
and exits cleanly. The next invocation picks up right where it left off.
Once a full day's list is done, later invocations that same day exit
almost instantly instead of re-scanning everything (see the
run_state / STATE_COMPLETED_DATE handling in run() below).

For each company: fetch postings -> for each profile: dedup check ->
score -> if new & above threshold, write to that profile's Sheet ->
if above the (higher) tailor threshold, also tailor resume bullets.

Company list = the daily discovery shard, minus anything in
excluded_companies.json. No company gets special "always polled"
treatment -- once you've applied somewhere, add its slug to
excluded_companies.json and it's gone from every future run, permanently.
discovery_pool.json (~15,800 companies from an open Greenhouse/Lever/Ashby
dataset) is sharded by date so the whole pool cycles over ~3-4 months
without hammering thousands of endpoints in one run.
"""

import json
import os
import time
from datetime import date

from dotenv import load_dotenv

load_dotenv()  # populates os.environ from .env -- must happen before tailor.py reads GEMINI_API_KEY

from .ats_clients import greenhouse, lever, ashby
from .db import (
    get_conn, has_seen, insert_posting, save_tailored_bullets, prune_old,
    get_state, set_state, try_acquire_lock, release_lock,
)
from .relevance import score_relevance
from .scoring import score_posting
from .sheets import get_worksheet, append_postings
from .tailor import tailor_bullets
from .text_utils import strip_html

ATS_CLIENTS = {
    "greenhouse": greenhouse,
    "lever": lever,
    "ashby": ashby,
}

REQUEST_DELAY_SECONDS = 1.5  # be polite to unauthenticated public endpoints
LLM_DELAY_SECONDS = 5         # gemini-3.5-flash-lite free tier = 15 requests/minute,
                              # 500 requests/day. 4s is the bare minimum RPM spacing;
                              # 5s leaves a margin. The daily cap (500) is generous
                              # enough for real use, unlike the non-Lite Flash models
                              # (20/day -- measured, not from docs, which Google no
                              # longer publishes for the free tier).
SHARD_SIZE = 400             # discovery-pool companies polled per run.
                              # At ~1 LLM call per 6 companies (measured), this
                              # uses roughly 65/500 daily Gemini requests -- well
                              # under quota. Raise further if runs finish quickly
                              # and you want faster full-pool coverage; the real
                              # limit is runtime (1.5s/company + 5s/LLM call),
                              # not the Gemini quota.
FLUSH_EVERY = 40             # rows buffered per profile before a batch Sheet write
                              # (Sheets write quota is 60 requests/min/user by default --
                              # batching keeps us at ~1 request per 40 rows, not per row)

TIME_BUDGET_SECONDS = 700    # Lambda's hard ceiling is 900s (15 min) and cannot be
                              # raised. 700s leaves ~200s of buffer for cold start,
                              # final flush, and commit overhead so we exit cleanly
                              # on our own terms instead of getting hard-killed
                              # mid-posting with the batch-not-yet-flushed. Only
                              # relevant to Option B (Lambda); a local cron run
                              # ignores this and just runs until it reaches the end
                              # of the list, same as before.

STATE_CURSOR_DATE = "cursor_date"     # run_state key: date (ISO) the saved cursor applies to
STATE_CURSOR_INDEX = "cursor_index"   # run_state key: index into today's company list to resume at
STATE_COMPLETED_DATE = "completed_date"  # run_state key: most recent date fully processed

LOCK_TTL_SECONDS = 800       # must be longer than TIME_BUDGET_SECONDS + flush/commit
                              # overhead, or a normally-running invocation could have
                              # its own lock expire out from under it. Longer than
                              # TIME_BUDGET_SECONDS on purpose -- this is the ceiling
                              # for a crashed/hard-killed invocation that never got to
                              # release its own lock, not the expected run length.


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXCLUDED_PATH = os.path.join(BASE_DIR, "config", "excluded_companies.json")
DISCOVERY_PATH = os.path.join(BASE_DIR, "config", "discovery_pool.json")
WATCHLIST_PATH = os.path.join(BASE_DIR, "config", "watchlist_companies.json")
NOTABLE_STARTUPS_PATH = os.path.join(BASE_DIR, "config", "notable_startups.json")


def load_companies() -> list[dict]:
    with open(EXCLUDED_PATH) as f:
        excluded = set(json.load(f))
    with open(DISCOVERY_PATH) as f:
        pool = json.load(f)
    with open(WATCHLIST_PATH) as f:
        watchlist = [
            {**c, "tier": "Medium/established"}
            for c in json.load(f) if c["slug"] not in excluded
        ]
    with open(NOTABLE_STARTUPS_PATH) as f:
        notable = [
            {**c, "tier": "Notable startup"}
            for c in json.load(f) if c["slug"] not in excluded
        ]

    num_shards = max(1, -(-len(pool) // SHARD_SIZE))  # ceil division

    override = os.environ.get("SHARD_INDEX")
    if override is not None:
        shard_index = int(override) % num_shards
    else:
        shard_index = date.today().toordinal() % num_shards
    start = shard_index * SHARD_SIZE
    shard = [
        {**c, "tier": "General discovery"}
        for c in pool[start:start + SHARD_SIZE] if c["slug"] not in excluded
    ]

    print(f"discovery shard {shard_index}/{num_shards - 1} ({len(shard)} companies, "
          f"{len(excluded)} excluded) + {len(watchlist)} medium/established + "
          f"{len(notable)} notable startups")
    return watchlist + notable + shard


def load_profiles() -> dict:
    try:
        from .profiles_local import PROFILES  # real config, not committed
    except ImportError:
        raise ImportError(
            "job_aggregator/profiles_local.py not found. "
            "Copy profiles_local.example.py to profiles_local.py and fill in "
            "real resume text + Sheet IDs (that file is gitignored)."
        )
    return PROFILES


def _flush(conn, worksheets, profiles, pending, profile_id):
    """Write this profile's buffered rows to its Sheet in one batch call.
    Only marks them 'seen' in the DB after the write succeeds -- if the
    Sheet write fails (quota, network, auth), these postings stay
    un-dedup'd and get retried on the next run instead of silently lost."""
    rows = pending[profile_id]
    if not rows:
        return

    profile = profiles[profile_id]
    if profile_id not in worksheets:
        worksheets[profile_id] = get_worksheet(profile.sheet_id, profile.worksheet_name)

    sheet_rows = [
        [item["title"], item["slug"], item["tier"], date.today().isoformat(),
         round(item["score"], 2), "New", item["reasoning"], item["tailored"], item["url"]]
        for item in rows
    ]
    append_postings(worksheets[profile_id], sheet_rows)
    print(f"{profile_id}: flushed {len(rows)} row(s) to Sheet")
    pending[profile_id] = []  # clear immediately -- the Sheet write already
                                # succeeded, so nothing past this point should
                                # be able to cause it to be re-sent

    for item in rows:
        try:
            insert_posting(
                conn, item["slug"], item["external_id"], profile_id,
                item["title"], item["location"], item["url"], item["score"],
            )
            if item["tailored"]:
                save_tailored_bullets(conn, item["slug"], item["external_id"], profile_id, item["tailored"])
        except Exception as e:
            # the Sheet row already exists -- a DB failure here just means
            # this one posting might get rescored (and possibly rewritten)
            # on a future run. Not ideal, but far better than the guaranteed
            # duplicate the old ordering produced on every such failure.
            print(f"WARNING: DB write failed for {item['slug']} after successful Sheet write: {e}")

    try:
        conn.commit()  # durably save this batch now, not just at the very end
                        # of the whole run -- a run can take 20-30+ minutes, and
                        # without this a crash anywhere loses every DB write made
                        # so far, not just the current batch
    except Exception as e:
        print(f"WARNING: commit failed after flushing {profile_id}: {e}")


def run():
    today = date.today().isoformat()
    start_time = time.monotonic()

    def time_left() -> float:
        return TIME_BUDGET_SECONDS - (time.monotonic() - start_time)

    companies = load_companies()
    profiles = load_profiles()
    worksheets = {}  # profile_id -> gspread worksheet, opened lazily
    pending = {profile_id: [] for profile_id in profiles}  # profile_id -> buffered sheet rows

    stats = {
        "companies_fetched_ok": 0, "companies_skipped": 0,
        "postings_seen_total": 0, "postings_already_known": 0,
        "postings_keyword_rejected": 0, "postings_llm_scored": 0,
        "postings_llm_failed": 0, "postings_below_min_score": 0,
        "postings_db_errors": 0,
        "postings_written": 0,
    }

    with get_conn() as conn:
        # Single-flight lock: EventBridge fires every 15 minutes, and one
        # invocation's own time budget (700s) is close enough to that
        # window that a slow invocation could still be running when the
        # next one starts -- on top of the ordinary risk of a manual test
        # invoke overlapping a scheduled one. Without this, two concurrent
        # invocations each read the same starting cursor, and whichever
        # finishes last overwrites the other's progress. If the lock is
        # already held, exit immediately and let the next scheduled tick
        # try again -- no partial work, no state touched.
        if not try_acquire_lock(conn, LOCK_TTL_SECONDS):
            print("another invocation is already running (lock held) -- exiting without doing any work")
            return

        prune_old(conn)

        # Daily-completion check: today's company list (watchlist + notable +
        # the date-derived discovery shard) is identical across every
        # invocation made on the same calendar day. Once one full pass has
        # completed, later same-day invocations should do ~nothing -- the
        # mandatory REQUEST_DELAY_SECONDS alone makes a "nothing new" pass
        # take ~18 minutes for 720 companies, and running that every 15-20
        # minutes all day for zero benefit is pure wasted compute (and, past
        # the free tier, pure wasted money).
        if get_state(conn, STATE_COMPLETED_DATE) == today:
            print(f"{today} already fully processed -- nothing to do until tomorrow's shard rotates in.")
            return

        # Resume cursor: where today's previous invocation (if any) left off.
        # A cursor saved for a different date is stale (yesterday's shard is
        # a different list) and is ignored -- start over from 0.
        start_index = 0
        if get_state(conn, STATE_CURSOR_DATE) == today:
            saved_index = get_state(conn, STATE_CURSOR_INDEX)
            if saved_index is not None:
                start_index = int(saved_index)
        if start_index:
            print(f"resuming {today}'s run at company {start_index}/{len(companies)}")

        ran_out_of_time = False
        try:
            for i in range(start_index, len(companies)):
                if time_left() <= 0:
                    ran_out_of_time = True
                    break

                company = companies[i]
                slug, ats_name = company["slug"], company["ats"]
                client = ATS_CLIENTS.get(ats_name)
                if client is None:
                    print(f"skip {slug}: unknown ATS '{ats_name}'")
                    stats["companies_skipped"] += 1
                    continue

                try:
                    postings = client.fetch_postings(slug)
                    stats["companies_fetched_ok"] += 1
                except ValueError as e:
                    print(f"skip {slug}: {e}")
                    stats["companies_skipped"] += 1
                    continue
                except Exception as e:
                    print(f"error fetching {slug} after retries: {e}")
                    stats["companies_skipped"] += 1
                    continue

                for profile_id, profile in profiles.items():
                    for posting in postings:
                        stats["postings_seen_total"] += 1
                        try:
                            already_known = has_seen(conn, slug, posting.external_id, profile_id)
                        except Exception as e:
                            # db.py already retries a dropped connection once --
                            # if it still failed, skip this posting rather than
                            # crash the whole run. It'll get a clean dedup check
                            # next run instead.
                            stats["postings_db_errors"] += 1
                            print(f"WARNING: dedup check failed for {slug} / {posting.title}: {e}")
                            continue
                        if already_known:
                            stats["postings_already_known"] += 1
                            continue

                        # cheap pre-filter: title keyword/exclusion match only.
                        # this does NOT decide the final score -- it just avoids
                        # spending an LLM call on an obvious non-match (wrong
                        # domain, wrong seniority, etc).
                        keyword_score = score_posting(posting, profile)
                        if keyword_score == 0.0:
                            stats["postings_keyword_rejected"] += 1
                            try:
                                insert_posting(
                                    conn, slug, posting.external_id, profile_id,
                                    posting.title, posting.location, posting.url, 0.0,
                                )
                            except Exception as e:
                                stats["postings_db_errors"] += 1
                                print(f"WARNING: DB write failed for {slug} / {posting.title}: {e}")
                            continue

                        # real fit judgment: resume + actual posting description
                        description = strip_html(posting.description)
                        score, reasoning = score_relevance(profile.resume_text, posting.title, description)
                        stats["postings_llm_scored"] += 1
                        if reasoning.startswith("relevance scoring failed"):
                            stats["postings_llm_failed"] += 1
                            print(f"WARNING: {slug} / {posting.title}: {reasoning}")
                            time.sleep(LLM_DELAY_SECONDS)
                            # do NOT mark as seen -- a failed judgment isn't a real
                            # judgment. If we marked it seen here, a systemic issue
                            # (bad key, deprecated model, quota) would permanently
                            # poison the dedup record for every posting it touched,
                            # exactly like this run just found happened last time.
                            continue
                        time.sleep(LLM_DELAY_SECONDS)

                        if score < profile.min_score:
                            stats["postings_below_min_score"] += 1
                            try:
                                insert_posting(
                                    conn, slug, posting.external_id, profile_id,
                                    posting.title, posting.location, posting.url, score,
                                )
                            except Exception as e:
                                stats["postings_db_errors"] += 1
                                print(f"WARNING: DB write failed for {slug} / {posting.title}: {e}")
                            continue

                        tailored = ""
                        if score >= profile.min_tailor_score:
                            tailored = tailor_bullets(
                                profile.resume_text, posting.title, description,
                            ) or ""
                            time.sleep(LLM_DELAY_SECONDS)

                        stats["postings_written"] += 1
                        pending[profile_id].append({
                            "slug": slug, "external_id": posting.external_id,
                            "title": posting.title, "location": posting.location,
                            "url": posting.url, "score": score, "reasoning": reasoning,
                            "tailored": tailored, "tier": company["tier"],
                        })

                        if len(pending[profile_id]) >= FLUSH_EVERY:
                            _flush(conn, worksheets, profiles, pending, profile_id)

                time.sleep(REQUEST_DELAY_SECONDS)
        finally:
            # always flush whatever's buffered, even if the loop above crashed --
            # this is what prevents a mid-run failure from silently losing matches
            for profile_id in profiles:
                _flush(conn, worksheets, profiles, pending, profile_id)

            if ran_out_of_time:
                # save our place -- next invocation resumes at company `i`
                # (not i+1: it may have fetched the board but not finished
                # scoring every posting/profile for it; re-fetching one
                # company is cheap, and has_seen() dedup means no posting
                # gets double-scored or double-written either way)
                try:
                    set_state(conn, STATE_CURSOR_DATE, today)
                    set_state(conn, STATE_CURSOR_INDEX, str(i))
                    print(f"\ntime budget reached -- saved cursor at company {i}/{len(companies)}, "
                          f"resuming there next invocation")
                except Exception as e:
                    print(f"WARNING: failed to save resume cursor: {e}")
            else:
                # reached the end of the list without running out of time --
                # today's full pass is done. Mark it so later same-day
                # invocations exit instantly instead of re-scanning, and
                # clear the cursor so tomorrow's (different) shard starts at 0.
                try:
                    set_state(conn, STATE_COMPLETED_DATE, today)
                    set_state(conn, STATE_CURSOR_INDEX, "0")
                    print(f"\n{today}'s full company list processed -- marked complete.")
                except Exception as e:
                    print(f"WARNING: failed to save completion state: {e}")

            print("\n--- run summary ---")
            print(f"companies: {stats['companies_fetched_ok']} fetched ok, "
                  f"{stats['companies_skipped']} skipped (bad slug / error)")
            print(f"postings seen: {stats['postings_seen_total']} total, "
                  f"{stats['postings_already_known']} already known (deduped)")
            print(f"postings rejected by keyword pre-filter: {stats['postings_keyword_rejected']}")
            print(f"postings sent to LLM relevance scoring: {stats['postings_llm_scored']} "
                  f"({stats['postings_llm_failed']} failed)")
            print(f"postings below min_score after LLM scoring: {stats['postings_below_min_score']}")
            print(f"postings written to Sheet: {stats['postings_written']}")
            print(f"postings skipped after DB error (even after reconnect retry): {stats['postings_db_errors']}")

            # release last -- only after the cursor/completion state above
            # is durably saved, so whichever invocation picks up next sees
            # accurate state rather than racing a half-written run
            try:
                release_lock(conn)
            except Exception as e:
                print(f"WARNING: failed to release run lock (will self-expire in {LOCK_TTL_SECONDS}s): {e}")


if __name__ == "__main__":
    run()
