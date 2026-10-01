import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from .base import NormalizedPosting

BASE_URL = "https://api.lever.co/v0/postings/{slug}?mode=json"


@retry(
    retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.TransportError)),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    stop=stop_after_attempt(4),
)
def _fetch(slug: str) -> list[dict]:
    resp = httpx.get(BASE_URL.format(slug=slug), timeout=10)
    if resp.status_code == 404:
        raise ValueError(f"Lever board not found for slug '{slug}'")
    resp.raise_for_status()
    return resp.json()


def _build_description(job: dict) -> str:
    parts = [job.get("description", "")]
    for section in job.get("lists", []):
        header = section.get("text", "")
        content = section.get("content", "")
        parts.append(f"<h3>{header}</h3>{content}")
    parts.append(job.get("additional", ""))
    return "\n".join(p for p in parts if p)


def fetch_postings(slug: str) -> list[NormalizedPosting]:
    jobs = _fetch(slug)
    return [
        NormalizedPosting(
            external_id=str(job["id"]),
            title=job["text"],
            location=(job.get("categories") or {}).get("location"),
            url=job["hostedUrl"],
            description=_build_description(job),
            raw=job,
        )
        for job in jobs
    ]
