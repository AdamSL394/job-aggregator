"""
Reorders and rewords existing resume bullets to emphasize what's
relevant to a specific posting. Never invents new experience.

Uses Gemini's free API tier -- check current rate limits before
relying on this at any real volume, they change. Uses the current
google-genai SDK (the older google-generativeai package is deprecated).
"""

from .llm import get_client, is_quota_exhausted, QuotaExhaustedError


SYSTEM_PROMPT = """You are reordering and rewording an existing resume's \
bullet points to better match a job posting. Rules:
- Do not invent skills, tools, employers, dates, or achievements not \
already present in the source resume.
- You may reorder bullets, adjust phrasing/emphasis, and select which \
existing bullets to lead with.
- If nothing in the resume is relevant to a requirement, omit it -- \
do not fabricate a bullet to cover it.
- Output must be the same total length as the source (same bullet count, \
similar word count per bullet). Do not expand or add new bullets.

Source resume bullets:
{resume_text}

Job posting:
{posting_text}

Output: the reordered/reworded bullets only, one per line. No preamble.
"""


def tailor_bullets(resume_text: str, posting_title: str, posting_description: str) -> str | None:
    prompt = SYSTEM_PROMPT.format(
        resume_text=resume_text,
        posting_text=f"{posting_title}\n{posting_description}",
    )
    try:
        response = get_client().models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=prompt,
        )
        output = (response.text or "").strip()
    except Exception as e:
        if is_quota_exhausted(e):
            raise QuotaExhaustedError(str(e)) from e
        # fail closed, same as relevance.py -- a rate limit or transient API
        # error here should never crash the whole run
        print(f"WARNING: tailoring failed: {e}")
        return None

    if not _within_length_budget(resume_text, output):
        # model drifted long -- fail closed rather than write bloated output
        return None
    return output


def _within_length_budget(source: str, output: str, tolerance: float = 0.10) -> bool:
    source_words = len(source.split())
    output_words = len(output.split())
    if source_words == 0:
        return False
    return output_words <= source_words * (1 + tolerance)
