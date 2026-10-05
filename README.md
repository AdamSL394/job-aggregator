# job-aggregator

Pulls new job postings from company ATS boards (Greenhouse, Lever, Ashby),
scores them against your resume, tailors resume bullets for strong matches,
writes results to a Google Sheet for review, and emails a daily digest of
the day's top matches.

Built to cost $0 to run. See architecture notes below, `ARCHITECTURE.md`
for the full design writeup, and `architecture-diagram.png` for a picture
of how the pieces fit together.

## Layout

```
job-aggregator/
  job_aggregator/            # the package -- all code lives here
    ats_clients/              # one module per ATS, each returns NormalizedPosting
      base.py                 # shared NormalizedPosting type
      greenhouse.py
      lever.py
      ashby.py
    db.py                     # Postgres dedup store (single table, 45-day prune)
    scoring.py                # free, rule-based relevance scoring
    tailor.py                 # LLM resume-bullet tailoring (Gemini free tier)
    sheets.py                 # Google Sheets read/write
    profiles.py                # Profile dataclass + loads profiles_local
    profiles_local.example.py  # template -- copy to profiles_local.py
    digest.py                   # daily email digest (Gmail SMTP, $0)
    run_poll.py                 # entry point, orchestrates everything
    lambda_handler.py           # thin wrapper around run_poll.run() for Option B
  config/
    excluded_companies.json         # gitignored -- slugs you've already applied to; skipped entirely
    excluded_companies.example.json # template -- copy to excluded_companies.json
    watchlist_companies.json         # gitignored -- established/medium companies you want covered
    watchlist_companies.example.json # every run (tier: "Medium/established")
    notable_startups.json            # gitignored -- notable startups you want covered every run
    notable_startups.example.json    # (tier: "Notable startup")
    discovery_pool.json         # NOT personal, committed as-is -- ~15,800 companies from an open
                                 # Greenhouse/Lever/Ashby dataset, polled in rotating daily shards
                                 # (see run_poll.py) so you catch companies you'd never have thought
                                 # to check manually (tier: "General discovery")
  data/                        # gitignored
    resume_<name>.txt           # your resume text, read by profiles_local.py
  tests/
  requirements.txt
  Dockerfile                   # Lambda container image (Option B)
  .dockerignore
  .env.example
  .gitignore
```

**Why this shape:** `ats_clients/` is a plugin-style layer so adding a new
ATS means adding one file, not touching the orchestrator. `db.py` does one
job (dedup) on purpose -- see project notes on why the schema stayed
single-table. Anything with real personal data (resume text, Sheet IDs,
API keys, curated company lists) lives outside git entirely, either in
`data/`, `profiles_local.py`, `.env`, or the three personal `config/*.json`
files listed above -- all gitignored. Each of those has a matching
`*.example.json` template committed instead, so anyone cloning the repo
gets the shape without your data. This mirrors the existing
`profiles_local.example.py` pattern.

**Why watchlist + notable startups + discovery instead of a hand-picked
company list:** an earlier version of this tool used a short list of
well-known companies -- which is exactly backwards, since you already know
to check those manually. The discovery pool (sourced from an open dataset
of ATS company slugs, https://github.com/Feashliaa/job-board-aggregator,
CC BY-NC 4.0) is what surfaces companies you'd never have thought to add
yourself. Polling all ~15,800 companies daily isn't practical, so
`run_poll.py` shards the pool deterministically by date -- each run covers
`SHARD_SIZE` companies, cycling through the full pool over a few months.
`watchlist_companies.json` and `notable_startups.json` are companies you
specifically want checked every single run regardless of the shard (fit
matters more than volume); `excluded_companies.json` is the opposite --
companies you've already applied to, skipped entirely so you don't keep
re-surfacing the same posting. All three are tagged with a tier that shows
up as its own column in the Sheet, purely for your own review priority --
it doesn't change how often anything is polled.

## Setup

1. `pip install -r requirements.txt`
2. Copy `.env.example` to `.env`, fill in `GEMINI_API_KEY` and
   `DATABASE_URL` (a free Neon or Supabase Postgres connection string --
   see Option B below; the dedup store needs this even for local runs now)
3. Copy `job_aggregator/profiles_local.example.py` to
   `job_aggregator/profiles_local.py`, fill in real resume text and Sheet IDs
4. Put resume text files in `data/resume_<name>.txt`
5. Get a Google service account JSON (Sheets API enabled), save as
   `service_account.json` in the project root, share each target Sheet
   with the service account's email
6. Copy each of the three example config files and fill in your own
   companies:
   - `config/excluded_companies.example.json` -> `config/excluded_companies.json`
   - `config/watchlist_companies.example.json` -> `config/watchlist_companies.json`
   - `config/notable_startups.example.json` -> `config/notable_startups.json`

   `config/discovery_pool.json` needs no editing; it's the pre-built
   ~15,800-company pool that `run_poll.py` shards through automatically.

## Run

```
python -m job_aggregator.run_poll
```

## Schedule it (Option A -- local, $0 forever)

```
crontab -e
# 0 8 * * * cd /path/to/job-aggregator && /path/to/venv/bin/python -m job_aggregator.run_poll
```

## Option B -- AWS Lambda + EventBridge (still $0, runs without your laptop)

`db.py` talks to Postgres instead of SQLite, and
`job_aggregator/lambda_handler.py` wraps `run()` for a Lambda container
image. Nothing in `ats_clients/`, `scoring.py`, `relevance.py`, or
`tailor.py` changed.

**The 15-minute problem.** AWS Lambda has a hard, non-configurable
maximum execution time of 900 seconds (15 minutes) -- there is no setting
or request that raises it. A full pass through the company list
(hundreds of companies, each with a `REQUEST_DELAY_SECONDS` pause plus a
`LLM_DELAY_SECONDS` pause per LLM call) takes well over an hour, so one
invocation can never finish one pass. `run_poll.py` handles this with a
resumable design instead of a single long-running job:

- Each invocation tracks its own elapsed time and stops with `time_left()
  <= 0` at `TIME_BUDGET_SECONDS` (700s -- well under the 900s hard
  ceiling, leaving room for cold start and the final flush/commit).
- Before exiting, it saves how far it got into a `run_state` table in
  Postgres (`cursor_date` / `cursor_index`). The next invocation reads
  that back and resumes at that company instead of starting over at 0.
- Once an invocation reaches the *end* of the list without running out of
  time, it writes `completed_date = today`. Any later invocation that
  same calendar day sees that and returns almost immediately instead of
  re-scanning everything -- important because even a "nothing new to
  find" pass still pays every `REQUEST_DELAY_SECONDS`/`LLM_DELAY_SECONDS`
  sleep, so without this check a frequently-scheduled Lambda would
  re-burn most of its budget all day for zero benefit.
- `today`'s company list (the watchlist + notable startups + that day's
  discovery shard) is deterministic from the date, so the cursor and
  completion flag are always being checked against the same list within
  one calendar day; a stale cursor from a previous day is ignored and the
  run starts over at 0 (correct, since the shard rotated).

This means EventBridge needs to invoke the function *repeatedly* through
the day (see step 5), not just once -- most of those invocations will be
fast no-ops once the day's pass is done.

**1. Hosted Postgres.** Create a free project on
[Neon](https://neon.tech) or [Supabase](https://supabase.com) (not AWS
RDS -- that's credit-based and expires). Copy the connection string into
`DATABASE_URL` in `.env` for local runs; `db.py` reads it the same way
either place.

**2. Build and push the image.**
```
aws ecr create-repository --repository-name job-aggregator
aws ecr get-login-password | docker login --username AWS --password-stdin <account-id>.dkr.ecr.<region>.amazonaws.com

docker build --platform linux/amd64 -t job-aggregator .
docker tag job-aggregator:latest <account-id>.dkr.ecr.<region>.amazonaws.com/job-aggregator:latest
docker push <account-id>.dkr.ecr.<region>.amazonaws.com/job-aggregator:latest
```

**3. IAM role for the function** (basic logging only -- this tool
doesn't touch any other AWS resource):
```
aws iam create-role --role-name job-aggregator-lambda \
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
aws iam attach-role-policy --role-name job-aggregator-lambda \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
```

**4. Create the function.** Secrets go in as plain environment
variables -- Lambda encrypts them at rest with the default AWS-managed
KMS key, so this needs no Secrets Manager (which isn't actually free).
```
aws lambda create-function \
  --function-name job-aggregator \
  --package-type Image \
  --code ImageUri=<account-id>.dkr.ecr.<region>.amazonaws.com/job-aggregator:latest \
  --role arn:aws:iam::<account-id>:role/job-aggregator-lambda \
  --timeout 900 --memory-size 512 \
  --environment "Variables={GEMINI_API_KEY=<key>,DATABASE_URL=<neon-connection-string>,GOOGLE_SERVICE_ACCOUNT_JSON=<service-account-json-as-one-line>}"
```
`GOOGLE_SERVICE_ACCOUNT_JSON` is the full contents of your service
account file, pasted as one JSON string -- `lambda_handler.py` writes
it to `/tmp/service_account.json` at cold start, since Lambda env vars
can't hold a file and only `/tmp` is writable. Timeout is set to the
15-minute Lambda max since a full shard run can take a while between
the 1.5s/company and 5s/LLM-call delays; raise `--memory-size` only if
you see OOM in the logs.

**5. Schedule it with EventBridge** (every 15 minutes, all day -- this
has to fire repeatedly, not once a day, because of the 15-minute problem
above; most of these invocations will be fast no-ops once the day's pass
completes):
```
aws events put-rule --name job-aggregator-frequent --schedule-expression "rate(15 minutes)"
aws lambda add-permission --function-name job-aggregator --statement-id eventbridge \
  --action lambda:InvokeFunction --principal events.amazonaws.com \
  --source-arn arn:aws:events:<region>:<account-id>:rule/job-aggregator-frequent
aws events put-targets --rule job-aggregator-frequent \
  --targets "Id=1,Arn=arn:aws:lambda:<region>:<account-id>:function:job-aggregator"
```
If you previously created a once-daily rule (`job-aggregator-daily`),
remove it so you don't double-invoke:
```
aws events remove-targets --rule job-aggregator-daily --ids 1
aws events delete-rule --rule-name job-aggregator-daily
```

**6. Test it manually** before trusting the schedule. Always pass
`--cli-read-timeout 0 --cli-connect-timeout 0` -- the AWS CLI's default
~60s client-side read timeout is shorter than a real run, and without
this flag the CLI gives up and silently fires a second overlapping
invocation:
```
aws lambda invoke --cli-read-timeout 0 --cli-connect-timeout 0 \
  --function-name job-aggregator /tmp/out.json && cat /tmp/out.json
```
Watch progress live in a second terminal instead of waiting on the
blocking `invoke` call:
```
aws logs tail /aws/lambda/job-aggregator --since 5m --follow --region <region>
```
Check for the run-summary stats block the local version prints, plus one
of:
- `time budget reached -- saved cursor at company N/M, resuming there
  next invocation` (expected on most of the first several runs through a
  new shard)
- `<date>'s full company list processed -- marked complete.` (today's
  pass is done)
- `<date> already fully processed -- nothing to do until tomorrow's shard
  rotates in.` (a no-op invocation -- this is most invocations, most
  days)

**Email digest (optional, $0).** Set `digest_email` on a profile in
`profiles_local.py` to get one email a day listing that day's matches
(whatever got written to that profile's Sheet -- gated at
`profile.min_score` in `profiles.py`). It fires once, right after
the day's full company-list pass completes, not on every 15-minute
invocation -- see `digest.py` and `ARCHITECTURE.md`. Uses Gmail's SMTP
over stdlib `smtplib`, no paid email service:

1. Turn on 2-Step Verification on the sending Gmail account if it isn't
   already, then generate an app password at
   https://myaccount.google.com/apppasswords (name it anything, e.g.
   "job-aggregator"). This is a 16-character password, *not* your real
   Gmail password.
2. Add two more Lambda environment variables: `SMTP_USER` (the Gmail
   address) and `SMTP_PASS` (the app password). Easiest done by pulling
   the current config to a file and editing it, so the existing
   `GOOGLE_SERVICE_ACCOUNT_JSON` value (multi-line, nested quotes)
   doesn't have to be retyped through the shell:
   ```
   aws lambda get-function-configuration --function-name job-aggregator \
     --region <region> --query 'Environment' --no-cli-pager > env.json
   # edit env.json, add "SMTP_USER": "...", "SMTP_PASS": "..." inside "Variables"
   aws lambda update-function-configuration --function-name job-aggregator \
     --region <region> --environment file://env.json --no-cli-pager
   rm env.json   # don't leave secrets sitting in the repo folder
   ```
3. Redeploy the image (step 2 above) so `digest.py` actually ships.

Cost: $0. Gmail's SMTP itself doesn't charge for sending, and a personal
account's ~500 messages/day limit is nowhere close to one email/day.

**Redeploying after a code change:** rebuild and push the image
(step 2), then `aws lambda update-function-code --function-name
job-aggregator --image-uri <...>:latest`.

**Cost:** with a 15-minute schedule (96 invocations/day), only the
handful of invocations needed to actually finish one pass through the
company list (roughly 10-20, each running close to the full
`TIME_BUDGET_SECONDS`=700s at 512MB) do real work; every other
invocation that day hits the `completed_date` check and returns in
under a second. Rough math: ~16 real runs x 700s x 0.5GB  ~5,600
GB-seconds, plus ~80 near-instant runs x ~1s x 0.5GB  ~40 GB-seconds,
so around 5,600-6,000 GB-seconds/day, or roughly 170,000-180,000
GB-seconds/month -- comfortably under the 400,000 GB-second free tier
(and the 2,880 invocations/month are nowhere near the 1M free-tier
request limit). This is an estimate, not a guarantee -- actual company
count, network latency, and how often postings trigger LLM scoring all
shift it; watch the Lambda console's "Billing" / CloudWatch metrics
after a week to confirm you're tracking under the free tier.

**ECR.** The free tier is 500MB-month of private-repo storage, under
your new AWS account's standard 12-month free tier (not forever-free).
One image is well under that -- but every `docker push` to the same
`:latest` tag leaves the *previous* image behind as an untagged,
still-billed-for image instead of deleting it, so repeated redeploys
(like the several done this session) quietly accumulate storage over
time. Two ways to stay safe:
- Check actual usage: `aws ecr describe-images --repository-name
  job-aggregator --region <region> --query
  'imageDetails[].imageSizeInBytes' --no-cli-pager` (sum it, divide by
  1e6 for MB).
- Add a lifecycle policy so old untagged images auto-expire instead of
  piling up:
  ```
  aws ecr put-lifecycle-policy --repository-name job-aggregator \
    --region <region> --lifecycle-policy-text '{"rules":[{"rulePriority":1,"description":"expire untagged images after 1 day","selection":{"tagStatus":"untagged","countType":"sinceImagePushed","countUnit":"days","countNumber":1},"action":{"type":"expire"}}]}'
  ```
  After this, storage stays pinned to roughly one image's size
  regardless of how often you redeploy.

Even past the free tier, ECR storage is ~$0.10/GB-month -- a few stray
untagged images is cents, not dollars. Worth the lifecycle policy
anyway so it never becomes a line item to think about.

Neon/Supabase free tier Postgres has no time limit, just a
storage/compute ceiling this table will never approach (two small
tables, one of them pruned every run).
