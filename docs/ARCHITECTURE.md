# Architecture

How job-aggregator actually runs in production (AWS Lambda / Option B),
and why each piece exists. README.md covers setup commands; this covers
the *design* -- the problems that forced it and how each one is solved.

## The core constraint: Lambda's 900-second ceiling

AWS Lambda has a hard maximum execution time of 900 seconds (15 minutes).
It is not configurable, not raisable by support request, not negotiable.

A full pass through one day's company list (watchlist + notable startups
+ that day's discovery shard -- currently ~720 companies) takes well over
an hour at the deliberately polite rate limits (`REQUEST_DELAY_SECONDS`
between companies, `LLM_DELAY_SECONDS` between LLM calls). One Lambda
invocation can never finish one pass.

Every other design decision in `run_poll.py` and `db.py` exists to make
that fact survivable: break the work into chunks small enough to fit in
900 seconds, remember where each chunk left off, and coordinate safely
across many invocations that have no memory of each other and no shared
process.

## The invocation lifecycle

```
EventBridge (rate(15 minutes))
        |
        v
  Lambda invocation starts (lambda_handler.handler)
        |
        v
  run_poll.run()
        |
        1. try_acquire_lock()  ----> already held? ----> exit immediately
        |                              (another invocation is still running)
        v
  2. prune_old()  (drop seen_postings rows >45 days old)
        |
        v
  3. completed_date == today?  ----> yes ----> exit immediately
        |                              (today's full pass is already done)
        v no
  4. read cursor_date / cursor_index
        |         (cursor_date == today -> resume there; otherwise start at 0)
        v
  5. walk companies[start_index:], stopping when time_left() <= 0
        |         (time_left() is wall-clock since this invocation started,
        |          budget = 700s, leaving ~200s margin under the 900s ceiling)
        v
  6. finally:
        - flush any buffered Sheet rows
        - save cursor (if time ran out) OR mark completed_date (if the
          list was fully walked)
        - print run summary
        - release_lock()
```

Nothing in this lifecycle assumes it's the only invocation that will ever
run, or that the previous invocation succeeded cleanly. Every step reads
its starting state from Postgres and writes its ending state back to
Postgres -- the Lambda container itself holds nothing durable.

## `run_state`: the only thing bridging invocations

Lambda gives you no persistent memory, disk, or process between
invocations -- the next one starts from nothing. `db.py` adds a single
key-value table, `run_state`, that's the sole channel for one invocation
to tell the next one anything:

| key              | meaning                                                      |
|------------------|---------------------------------------------------------------|
| `cursor_date`    | the date (ISO) the saved `cursor_index` applies to            |
| `cursor_index`   | index into *that date's* company list to resume at            |
| `completed_date` | the most recent date whose full company list finished         |
| `lock_expires_at`| ISO timestamp; the single-flight lock (see below)              |

A cursor from a different date is ignored on purpose -- each day's
company list is a different list (the discovery shard rotates by
`date.today().toordinal() % num_shards`), so a cursor into yesterday's
list means nothing today.

## The single-flight lock

**The race it prevents:** two invocations running at the same time both
read the same starting cursor, both process some companies, and both
write an ending cursor back -- whichever one's `set_state` call lands
last wins, silently discarding the other's progress. This isn't a
hypothetical: it happened during development (two manual test invokes
overlapped, cursors ended up at 20 and 1 respectively instead of
advancing). It can also happen for real: `TIME_BUDGET_SECONDS` (700s) is
close enough to the EventBridge interval (15 min = 900s) that a slow
invocation could still be running when the next scheduled tick fires.

**Why not a Postgres advisory lock (`pg_advisory_lock`):** those are
tied to the specific backend session/connection that took them. Neon's
free tier fronts Postgres with connection pooling that can hand different
statements to different physical backend connections -- an advisory lock
taken on one connection can be silently unheld by the time the next
statement runs on a different one. That makes advisory locks unreliable
under this specific hosting setup.

**What it does instead:** a single atomic SQL statement --

```sql
INSERT INTO run_state (key, value) VALUES ('lock_expires_at', :new_expiry)
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
WHERE run_state.value < :now
```

This only updates the row (and only succeeds) if the existing lock has
already expired. `cur.rowcount == 1` means this call won the lock;
`== 0` means someone else holds it. This depends only on row contents,
not on which physical connection executes it -- safe under any pooling
mode.

**TTL instead of release-on-disconnect:** the lock isn't released when a
connection closes (no such guarantee exists under pooling either) -- it
expires after `LOCK_TTL_SECONDS` (800s, intentionally longer than the
700s work budget). A crashed or hard-killed invocation that never calls
`release_lock()` can't deadlock every future invocation forever; the next
one just waits out the TTL. A clean invocation calls `release_lock()` in
its `finally` block, immediately, so the *next* scheduled invocation
doesn't have to wait out the TTL at all -- release happens last, after
the cursor/completion state is durably saved, so whatever runs next sees
accurate state rather than racing a half-written run.

## Daily completion tracking

Even with the lock, hammering the full company list every 15 minutes all
day would be wasteful once nothing is left to find: `REQUEST_DELAY_SECONDS`
alone makes a "nothing new" pass take ~18 minutes for 720 companies.
Once one invocation walks all the way to the end of the list without
running out of time, it writes `completed_date = today`. Every later
invocation that same day checks this first and exits in a few seconds
(see the `03:58`/`04:13`/`04:28` entries in a typical day's CloudWatch
log -- each billed ~4-11 seconds instead of a full run). The cursor is
also reset to `0` at that point, so tomorrow's (different) shard starts
clean.

## Gemini quota exhaustion

Gemini's free tier caps at 500 `generate_content` requests/day. Running
out mid-invocation used to mean: every remaining posting still attempts
the LLM call, gets a `429 RESOURCE_EXHAUSTED`, and still pays the full
`LLM_DELAY_SECONDS` sleep -- burning the rest of the time budget on calls
guaranteed to fail instead of making progress through the company list.

`llm.py` defines `QuotaExhaustedError` and `is_quota_exhausted(exc)`,
which matches on `"RESOURCE_EXHAUSTED"` in the error text (text-matched
rather than tied to a specific exception class/attribute, since the
google-genai SDK's exception shape has changed across versions -- the
API's own error body reliably names this status). `relevance.py` and
`tailor.py` re-raise this specific error instead of failing closed like
any other scoring error.

`run_poll.py` catches it once, sets an in-memory `llm_quota_exhausted`
flag, and from then on skips the LLM step entirely for every remaining
posting *in this invocation* -- no network call, no sleep. The flag is
deliberately **not** persisted to `run_state`: the quota window is
wall-clock time (resets ~24h after first being hit), not tied to any one
invocation, so the next invocation gets its own fresh attempt in case the
quota already reset.

Postings that couldn't be scored (quota exhausted, or any other scoring
failure) are never marked "seen" -- they're retried by a future
invocation once scoring is possible again. Nothing is lost, just
delayed.

## Daily email digest

`digest.py` emails each profile's matches once a day. It's called from
`run_poll.py` only inside the branch that just set `completed_date` --
the branch that runs exactly once per calendar day (every other
invocation that day exits earlier, at the `STATE_COMPLETED_DATE` check).
That placement is the whole mechanism: no separate "did we email today"
flag is needed, because the branch itself only runs once.

It deliberately carries **no threshold of its own** -- it queries
`seen_postings` for rows `written_to_sheet = 1` with `first_seen_at`
today, full stop. That set is already exactly "what the Sheet got
today," gated by `profile.min_score` in `profiles.py` (currently 0.9).
An earlier version hard-coded a second score cutoff inside `digest.py`;
that was removed on purpose -- two thresholds that are supposed to
agree is a bug waiting to happen the next time `min_score` is tuned and
only one of them gets updated. Now there's one number, in one file, and
the email and the Sheet can never disagree about what counts as a match.

Sends over Gmail's SMTP via stdlib `smtplib` -- a Gmail "app password"
(not the real account password), not a paid transactional email
service, keeping the project's $0 budget intact. `SMTP_USER`/`SMTP_PASS`
are Lambda env vars; if they're unset, `send_daily_digest` logs a
warning and returns instead of raising, same fail-open posture as the
rest of the run -- a broken email integration should never take down
the actual scoring/Sheet-writing work. It's also called *after*
`completed_date` is already durably saved, so a digest failure (bad
credentials, Gmail rate limit, network blip) can't roll back or block
anything about the day's run being marked complete.

## Why postings are flushed and committed mid-run, not just at the end

`_flush()` writes a batch of matches to the Google Sheet, then inserts
those rows into `seen_postings`, then commits -- every `FLUSH_EVERY`
matches, not once at the very end of the run. A run can span the full
700-second budget; without incremental commits, a crash anywhere would
lose every DB write made so far, not just the most recent batch. The
Sheet write happens *before* the DB insert on purpose too: if the DB
write fails after a successful Sheet write, the posting might get
rescored on a future run (producing a duplicate Sheet row) -- not ideal,
but far better than the reverse ordering, which would guarantee a
duplicate on every such failure.

## Connection resilience (`_ReconnectingConn`)

Neon's free tier fronts Postgres with a pooler that silently closes idle
sockets. A run with long stretches of zero DB traffic (walking through
many "board not found" skips in a row touches no DB row at all) can hit
`OperationalError`/`InterfaceError` on the next query, not a normal
response. `_ReconnectingConn` duck-types a psycopg2 connection
(`.cursor()` / `.commit()` / `.close()`) and transparently reconnects
once on either error before retrying the failed operation -- invisible
in the common case, rather than crashing the whole run over one dropped
socket.

## File map

| file                        | responsibility                                                |
|------------------------------|---------------------------------------------------------------|
| `run_poll.py`                | orchestration: the resumable loop, time budget, lock, quota handling |
| `db.py`                      | Postgres schema, dedup store, `run_state` (cursor/completion/lock), reconnect wrapper |
| `llm.py`                     | shared Gemini client, `QuotaExhaustedError` / `is_quota_exhausted` |
| `relevance.py`               | LLM fit-scoring (resume vs. posting) |
| `tailor.py`                  | LLM resume-bullet tailoring for strong matches |
| `scoring.py`                 | cheap keyword pre-filter, run before any LLM call |
| `ats_clients/`               | one module per ATS (Greenhouse/Lever/Ashby), each returns `NormalizedPosting` |
| `sheets.py`                  | Google Sheets read/write |
| `digest.py`                  | daily email digest of that day's Sheet matches, via Gmail SMTP |
| `profiles.py`                | `Profile` dataclass -- `min_score`, `min_tailor_score`, `digest_email`, etc. |
| `lambda_handler.py`          | Lambda entrypoint -- writes the service-account JSON from an env var to `/tmp`, then calls `run_poll.run()` |

## Operational notes

- **Monitor a run:**
  `aws logs tail /aws/lambda/job-aggregator --since 1h --region <region> --follow`
  Look for `time budget reached -- saved cursor at company N/M`, `<date>'s
  full company list processed -- marked complete`, `<date> already fully
  processed`, or `another invocation is already running (lock held)`.
- **Redeploy after a code change:** rebuild/push the Docker image, then
  `aws lambda update-function-code`. A `git push` alone does not update
  the running Lambda -- the image has to be rebuilt and pushed separately.
- **Cost:** with the 15-minute schedule, only the handful of invocations
  that do real work until the day's pass completes cost anything
  meaningful (~700s each); every other invocation that day exits in
  single-digit seconds. See README.md for the detailed GB-second math.
