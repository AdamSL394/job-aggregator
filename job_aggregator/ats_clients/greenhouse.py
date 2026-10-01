import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from .base import NormalizedPosting

BASE_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"


@retry(
    retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.TransportError)),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    stop=stop_after_attempt(4),
)
def _fetch(slug: str) -> list[dict]:
    resp = httpx.get(BASE_URL.format(slug=slug), params={"content": "true"}, timeout=10)
    if resp.status_code == 404:
        # bad slug -- config error, not transient. Don't retry, let caller skip it.
        raise ValueError(f"Greenhouse board not found for slug '{slug}'")
    resp.raise_for_status()
    return resp.json().get("jobs", [])


def fetch_postings(slug: str) -> list[NormalizedPosting]:
    jobs = _fetch(slug)
    return [
        NormalizedPosting(
            external_id=str(job["id"]),
            title=job["title"],
            location=(job.get("location") or {}).get("name"),
            url=job["absolute_url"],
            description=job.get("content", ""),  # HTML, requires content=true above
            raw=job,
        )
        for job in jobs
    ]
