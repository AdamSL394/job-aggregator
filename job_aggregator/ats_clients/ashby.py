import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from .base import NormalizedPosting

BASE_URL = "https://api.ashbyhq.com/posting-api/job-board/{slug}"


@retry(
    retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.TransportError)),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    stop=stop_after_attempt(4),
)
def _fetch(slug: str) -> list[dict]:
    resp = httpx.get(BASE_URL.format(slug=slug), timeout=10)
    if resp.status_code == 404:
        raise ValueError(f"Ashby board not found for slug '{slug}'")
    resp.raise_for_status()
    return resp.json().get("jobs", [])


def fetch_postings(slug: str) -> list[NormalizedPosting]:
    jobs = _fetch(slug)
    return [
        NormalizedPosting(
            external_id=str(job["id"]),
            title=job["title"],
            location=job.get("location"),
            url=job["jobUrl"],
            description=job.get("descriptionPlain") or job.get("descriptionHtml", ""),
            raw=job,
        )
        for job in jobs
    ]
