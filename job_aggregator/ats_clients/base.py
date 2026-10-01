"""
Every ATS client returns a list of NormalizedPosting so run_poll.py
never needs to know which ATS it's talking to.
"""

from dataclasses import dataclass


@dataclass
class NormalizedPosting:
    external_id: str
    title: str
    location: str | None
    url: str
    description: str = ""  # raw HTML or plain text, ATS-specific extraction happens in each client
    raw: dict = None  # keep the original payload around for debugging, not stored long-term
