"""
Copy this file to job_aggregator/profiles_local.py and fill in real values.
profiles_local.py is gitignored -- this example file is the only one committed.
"""

from .profiles import Profile

PROFILES: dict[str, Profile] = {
    "adam": Profile(
        profile_id="adam",
        resume_text=open("data/resume_adam.txt").read(),
        target_titles=["backend engineer", "platform engineer", "software engineer"],
        sheet_id="paste-adams-google-sheet-id-here",
        worksheet_name="New postings",  # change to match an existing tab if you'd rather reuse one
    ),
    "partner": Profile(
        profile_id="partner",
        resume_text=open("data/resume_partner.txt").read(),
        target_titles=[],  # fill in partner's target roles
        sheet_id="paste-partners-google-sheet-id-here",
    ),
}
