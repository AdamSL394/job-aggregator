"""
Entry point. Run this from local cron (Option A):
    0 8 * * * cd /path/to/job-aggregator && python -m job_aggregator.run_poll

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
from .db import get_conn, has_seen, insert_posting, save_tailored_bullets, prune_old
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


def run():
    companies = load_companies()
    profiles = load_profiles()
    worksheets = {}  # profile_id -> gspread worksheet, opened lazily
    pending = {profile_id: [] for profile_id in profiles}  # profile_id -> buffered sheet rows

    stats = {
        "companies_fetched_ok": 0, "companies_skipped": 0,
        "postings_seen_total": 0, "postings_already_known": 0,
        "postings_keyword_rejected": 0, "postings_llm_scored": 0,
        "postings_llm_failed": 0, "postings_below_min_score": 0,
        "postings_written": 0,
    }

    with get_conn() as conn:
        prune_old(conn)

        try:
            for company in companies:
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
                        if has_seen(conn, slug, posting.external_id, profile_id):
                            stats["postings_already_known"] += 1
                            continue

                        # cheap pre-filter: title keyword/exclusion match only.
                        # this does NOT decide the final score -- it just avoids
                        # spending an LLM call on an obvious non-match (wrong
                        # domain, wrong seniority, etc).
                        keyword_score = score_posting(posting, profile)
                        if keyword_score == 0.0:
                            stats["postings_keyword_rejected"] += 1
                            insert_posting(
                                conn, slug, posting.external_id, profile_id,
                                posting.title, posting.location, posting.url, 0.0,
                            )
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
                            insert_posting(
                                conn, slug, posting.external_id, profile_id,
                                posting.title, posting.location, posting.url, score,
                            )
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


if __name__ == "__main__":
    run()
