"""
One Profile per person using this tool. Each gets its own Sheet,
its own resume, its own thresholds.

This file only defines the Profile shape -- it has no dependency on
real data, so tests and other modules can import it freely. The actual
PROFILES dict (real resume text, real Sheet IDs) is loaded separately
by run_poll.py from job_aggregator/profiles_local.py, which is gitignored.
"""

from dataclasses import dataclass, field


@dataclass
class Profile:
    profile_id: str
    resume_text: str
    target_titles: list[str]
    target_locations: list[str] = field(default_factory=lambda: [
        "remote", "san francisco", "bay area", "sf,", "sf -", "oakland",
        "san jose", "palo alto", "mountain view", "sunnyvale", "redwood city",
    ])
    exclude_titles: list[str] = field(default_factory=lambda: [
        "forward deployed", "implementation engineer", "demo engineer",
        "solutions engineer", "sales engineer",
        # domain -- these can legitimately contain "software engineer" as a
        # substring while being a different discipline entirely
        "embedded", "firmware", "hardware", "flight software",
        # seniority / role-type -- above or outside the level being targeted
        "staff", "lead", "principal", "founding",
    ])
    stretch_mode: str = "narrow"  # "narrow" | "broad"
    min_score: float = 0.6         # write to Sheet above this
    min_tailor_score: float = 0.85  # also tailor resume bullets above this
    sheet_id: str = ""             # Google Sheet ID, one per profile
    worksheet_name: str = "New postings"  # tab within that sheet -- won't touch your other tabs