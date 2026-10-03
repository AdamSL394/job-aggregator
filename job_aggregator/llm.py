"""
Single shared Gemini client, lazily initialized so importing this module
(or anything that imports it) doesn't require GEMINI_API_KEY to be set
until an LLM call is actually made.
"""

import os
from google import genai

_client = None


def get_client():
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    return _client


class QuotaExhaustedError(Exception):
    """The Gemini free-tier daily quota (500 requests/day) is used up.
    Distinct from a generic API failure so callers can stop making
    further LLM calls for the rest of this run, rather than retrying
    each posting and paying a full LLM_DELAY_SECONDS sleep for a call
    that is guaranteed to fail again until the quota resets (~24h, per
    Google's own retryDelay in the 429 response)."""


def is_quota_exhausted(exc: Exception) -> bool:
    # Matching on the error text rather than a specific exception class/
    # attribute -- the google-genai SDK's exception shape has changed
    # across versions, but the API's own error body always names this
    # status for a quota (as opposed to a transient/rate) failure.
    return "RESOURCE_EXHAUSTED" in str(exc)
