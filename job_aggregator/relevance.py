"""
Real fit judgment, not keyword counting. Only called for postings that
already passed scoring.py's cheap keyword gate (title matches a target
role, doesn't hit an exclusion) -- this is the second, heavier pass that
decides whether it's actually a good match.
"""

import json
import re

from .llm import get_client

PROMPT = """You are judging how well a candidate's resume fits a specific \
job posting. Be strict -- most postings that pass a basic title keyword \
filter are NOT actually a strong fit once you read the real requirements.

Check these specifically, even if the title alone looked fine:
1. Years of experience required vs. the resume's actual years. If the \
posting requires meaningfully more experience than the resume shows \
(e.g. posting wants 7+ years, resume shows ~4), this is NOT a strong fit \
-- cap the score at 49, regardless of title match.
2. Seniority level implied by the DESCRIPTION, not just the title. A \
posting titled generically but describing Staff/Principal/Lead-level \
scope (owns architecture for an org, sets technical direction across \
teams, etc.) should be scored as that seniority, not the title's level.
3. Core required technology the resume does NOT show. If the posting's \
primary required stack (e.g. "strong Go/Golang required") is absent from \
the resume's actual listed languages/tools, that is a meaningful gap, \
not a minor one -- do not treat "the candidate could probably pick it up" \
as sufficient for a strong-fit score.

Score 0-100:
- 80-100: strong fit. Core responsibilities, required skills, AND \
required experience level clearly match the resume's actual experience.
- 50-79: partial fit. Some overlap, but a meaningful gap in seniority, \
domain, required years, or required skills.
- 0-49: weak fit. Keyword-level overlap only (e.g. shares the words \
"software engineer" but is a different discipline, seniority, required \
experience level, or core tech stack).

Resume:
{resume_text}

Job title: {title}

Job description:
{description}

Respond with ONLY valid JSON, no markdown fences, no other text:
{{"score": <integer 0-100>, "reasoning": "<one sentence, specific>"}}
"""


def score_relevance(resume_text: str, title: str, description: str) -> tuple[float, str]:
    """Returns (score 0.0-1.0, one-sentence reasoning). On any failure to
    get a usable judgment, fails closed: returns (0.0, reason) rather than
    letting a parse error silently pass a posting through."""
    prompt = PROMPT.format(resume_text=resume_text, title=title, description=description)

    try:
        response = get_client().models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=prompt,
        )
        raw = (response.text or "").strip()
        raw = re.sub(r"^```(json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
        parsed = json.loads(raw)
        score = max(0, min(100, int(parsed["score"]))) / 100.0
        reasoning = str(parsed.get("reasoning", ""))[:300]
        return score, reasoning
    except Exception as e:
        return 0.0, f"relevance scoring failed: {e}"
