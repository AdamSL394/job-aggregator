"""
Keyword/title scoring. Free, fast, runs on every new posting.
LLM tailoring (tailor.py) only runs on postings that score above
a profile's min_tailor_score -- this file is what decides that.
"""

import re

from .ats_clients.base import NormalizedPosting
from .profiles import Profile


def _contains_word(text: str, phrase: str) -> bool:
    # word-boundary match, not a raw substring -- "software engineer" should
    # not match inside "embedded software engineer" any differently than a
    # human reading the title would judge it, and "lead" should match the
    # word "lead", not the middle of an unrelated word.
    pattern = r"\b" + re.escape(phrase.lower()) + r"\b"
    return re.search(pattern, text) is not None


_NON_US_REMOTE_MARKERS = [
    "india", "emea", "apac", "latam", "canada", "uk", "europe", "european",
    "philippines", "poland", "germany", "france", "spain", "brazil", "mexico",
    "argentina", "australia", "singapore", "japan", "china", "africa",
]


def _location_matches(location: str | None, target_locations: list[str]) -> bool:
    if not target_locations:
        return True  # no location preference set -- don't filter on it
    if not location:
        return False  # no location data and we have a preference -- can't confirm it fits, so exclude
    location_lower = location.lower()

    if "remote" in location_lower:
        # "remote" alone is fine, but "Remote - India" contains the substring
        # "remote" too -- reject remote postings that are region-qualified to
        # somewhere outside the US.
        if any(marker in location_lower for marker in _NON_US_REMOTE_MARKERS):
            return False
        return True

    return any(target in location_lower for target in target_locations if target != "remote")


def score_posting(posting: NormalizedPosting, profile: Profile) -> float:
    if not _location_matches(posting.location, profile.target_locations):
        return 0.0

    title_lower = posting.title.lower()

    for excluded in profile.exclude_titles:
        if _contains_word(title_lower, excluded):
            return 0.0

    if not profile.target_titles:
        return 0.0

    matches = sum(1 for t in profile.target_titles if _contains_word(title_lower, t))
    if matches == 0:
        return 0.0

    # crude but effective: more target-title keyword overlap = higher score.
    # caps at 1.0 so it composes cleanly with a future LLM-based score if added.
    base = min(1.0, 0.5 + 0.25 * matches)
    return base
