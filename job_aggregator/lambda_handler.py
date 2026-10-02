"""
Lambda entry point, invoked on a schedule by EventBridge (Option B).

Secrets arrive as Lambda environment variables (encrypted at rest with
the default AWS-managed KMS key -- no Secrets Manager needed, keeps
this at $0):
  GEMINI_API_KEY               -- same value as local .env
  DATABASE_URL                 -- hosted Postgres connection string (Neon/Supabase)
  GOOGLE_SERVICE_ACCOUNT_JSON  -- the full service account JSON as one string
                                   (Lambda env vars can't hold a file; this gets
                                   written to /tmp at cold start instead)
"""

import json
import os

CREDS_PATH = "/tmp/service_account.json"


def _write_service_account_creds():
    if os.path.exists(CREDS_PATH):
        return  # already written this cold start -- subsequent invocations reuse it
    raw = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    json.loads(raw)  # fail loudly here if the env var isn't valid JSON, not deep in gspread
    with open(CREDS_PATH, "w") as f:
        f.write(raw)


def handler(event, context):
    _write_service_account_creds()
    os.environ["GOOGLE_SERVICE_ACCOUNT_CREDS_PATH"] = CREDS_PATH

    from .run_poll import run  # imported here, not at module load, so cold-start
    run()                       # errors surface as a normal Lambda failure/log, not an import crash

    return {"statusCode": 200}
