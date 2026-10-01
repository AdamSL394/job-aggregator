# job-aggregator

Pulls new job postings from company ATS boards (Greenhouse, Lever, Ashby),
scores them against your resume, tailors resume bullets for strong matches,
and writes results to a Google Sheet for review.

Built to cost $0 to run. See architecture notes below.

## Layout

```
job-aggregator/
  job_aggregator/            # the package -- all code lives here
    ats_clients/              # one module per ATS, each returns NormalizedPosting
      base.py                 # shared NormalizedPosting type
      greenhouse.py
      lever.py
      ashby.py
    db.py                     # SQLite dedup store (single table, 45-day prune)
    scoring.py                # free, rule-based relevance scoring
    tailor.py                 # LLM resume-bullet tailoring (Gemini free tier)
    sheets.py                 # Google Sheets read/write
    profiles.py                # Profile dataclass + loads profiles_local
    profiles_local.example.py  # template -- copy to profiles_local.py
    run_poll.py                 # entry point, orchestrates everything
  config/
    priority_companies.json    # companies you're actually interviewing with -- polled every run
    discovery_pool.json        # ~15,800 companies from an open Greenhouse/Lever/Ashby dataset,
                                # polled in rotating daily shards (see run_poll.py) so you catch
                                # companies you'd never have thought to check manually
  data/                        # gitignored -- generated at runtime
    jobs.db
    resume_<name>.txt
  tests/
  requirements.txt
  .env.example
  .gitignore
```

**Why this shape:** `ats_clients/` is a plugin-style layer so adding a new
ATS means adding one file, not touching the orchestrator. `db.py` does one
job (dedup) on purpose -- see project notes on why the schema stayed
single-table. Anything with real personal data (resume text, Sheet IDs,
API keys) lives outside git entirely, either in `data/`, `profiles_local.py`,
or `.env` -- all gitignored.

**Why priority + discovery instead of a hand-picked company list:** an
earlier version of this tool used a short list of well-known companies --
which is exactly backwards, since you already know to check those
manually. The discovery pool (sourced from an open dataset of ATS company
slugs, https://github.com/Feashliaa/job-board-aggregator, CC BY-NC 4.0)
is what surfaces companies like Watershed or Astranis that you'd never
have thought to add yourself. Polling all ~15,800 companies daily isn't
practical, so `run_poll.py` shards the pool deterministically by date --
each run covers `SHARD_SIZE` companies (150 by default), cycling through
the full pool over ~3-4 months. Bump `SHARD_SIZE` in `run_poll.py` for
faster coverage at the cost of longer runs.

## Setup

1. `pip install -r requirements.txt`
2. Copy `.env.example` to `.env`, fill in `GEMINI_API_KEY`
3. Copy `job_aggregator/profiles_local.example.py` to
   `job_aggregator/profiles_local.py`, fill in real resume text and Sheet IDs
4. Put resume text files in `data/resume_<name>.txt`
5. Get a Google service account JSON (Sheets API enabled), save as
   `service_account.json` in the project root, share each target Sheet
   with the service account's email
6. `config/priority_companies.json` already has real companies from your
   active pipeline (Watershed, Astranis, Prelim, Descript, BJAK) -- add
   more any time. `config/discovery_pool.json` needs no editing; it's
   the pre-built ~15,800-company pool that `run_poll.py` shards through
   automatically.

## Run

```
python -m job_aggregator.run_poll
```

## Schedule it (Option A -- local, $0 forever)

```
crontab -e
# 0 8 * * * cd /path/to/job-aggregator && /path/to/venv/bin/python -m job_aggregator.run_poll
```

## Later: Option B (still $0, runs when your laptop's off)

Swap the cron trigger for AWS Lambda + EventBridge, and the SQLite file
for a permanent free-tier hosted Postgres (Neon or Supabase -- not AWS
RDS, which is credit-based and expires). Code changes needed: none in
`ats_clients/`, `scoring.py`, or `tailor.py` -- only `db.py`'s connection
and how `run_poll.py` gets triggered.
