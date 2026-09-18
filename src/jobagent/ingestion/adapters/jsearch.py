"""JSearch sense (RapidAPI) — LinkedIn, Indeed, Glassdoor, ZipRecruiter and others
behind one endpoint.

**Why an aggregator API rather than a scraper.** These three boards are where most
postings actually are, and all three are aggressively anti-bot with no public API.
Scraping them directly would be fragile, against their terms, and a violation of R7
(prefer APIs over scraping). JSearch indexes them and sells access, which keeps the
anti-bot problem on the side of the party who signed up for it.

The trade worth knowing: aggregator results are lower quality than a direct ATS feed —
titles are rewritten, companies are inconsistent, and the same role arrives from several
underlying boards. That last one is why `cluster_key` exists; without it, turning this
sense on visibly triples the queue with duplicates.

Config: `JSEARCH_API_KEY` (a RapidAPI key) plus `[sources] aggregator = true`.
Queries come from the profile's `target_roles`, so the sense searches for what the
operator is actually looking for rather than a hardcoded list.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone

import httpx

from jobagent.core.schemas import ApplyMethod, JobPosting, Source
from jobagent.ingestion.base import BaseAdapter
from jobagent.ingestion.util import get_with_retry, make_client, strip_html

API_HOST = "jsearch.p.rapidapi.com"
API_URL = f"https://{API_HOST}/search"

# Free RapidAPI tiers are typically a few hundred calls a month, and one call is one
# page for one query. Defaults stay well inside that: a handful of roles, one page
# each, refreshed a few times a day is ~90 calls a month. Raising these is the
# operator's decision to spend their own quota.
DEFAULT_PAGES = 1
MAX_QUERIES = 5


class JSearchAdapter(BaseAdapter):
    source = Source.aggregator

    def __init__(self, api_key: str, queries: list[str] | None = None, *,
                 location: str = "", remote_only: bool = True,
                 pages: int = DEFAULT_PAGES, client: httpx.Client | None = None):
        self.api_key = api_key or ""
        # Deduplicated but order-preserving: the first roles in a profile are the ones
        # the operator cares most about, and the cap should cut from the tail.
        seen: dict[str, None] = {}
        for q in (queries or []):
            if q.strip():
                seen.setdefault(q.strip(), None)
        self.queries = list(seen)[:MAX_QUERIES]
        self.location = location
        self.remote_only = remote_only
        self.pages = max(1, pages)
        self._client = client

    @property
    def enabled(self) -> bool:
        """Gated on BOTH a key and something to search for.

        Without queries the API would be asked for "" and return an arbitrary slice of
        every job on the internet — thousands of rows with no relationship to the
        profile, which is worse than fetching nothing.
        """
        return bool(self.api_key and self.queries)

    def fetch(self) -> Iterable[JobPosting]:
        if not self.enabled:
            return
        client, owns = make_client(self._client)
        headers = {"X-RapidAPI-Key": self.api_key, "X-RapidAPI-Host": API_HOST}
        try:
            for query in self.queries:
                for page in range(1, self.pages + 1):
                    params = {
                        "query": f"{query} {self.location}".strip(),
                        "page": str(page),
                        "num_pages": "1",
                    }
                    if self.remote_only:
                        params["remote_jobs_only"] = "true"
                    payload = get_with_retry(
                        client, API_URL, params=params, headers=headers).json()
                    for item in payload.get("data") or []:
                        if isinstance(item, dict):
                            yield self._normalize(item)
        finally:
            if owns:
                client.close()

    def _normalize(self, item: dict) -> JobPosting:
        # Keys verified against a real JSearch response payload, not written from
        # memory — the surrounding project has been bitten four times by guessed key
        # names rendering None into user-visible text (R32).
        remote = bool(item.get("job_is_remote"))
        city = item.get("job_city") or ""
        country = item.get("job_country") or ""
        location = ", ".join(p for p in (city, country) if p) or (
            "Remote" if remote else "")

        return JobPosting(
            source=Source.aggregator,
            source_job_id=str(item.get("job_id") or "") or None,
            title=item.get("job_title") or "(untitled)",
            company=item.get("employer_name"),
            location=location,
            is_remote=remote,
            description=strip_html(item.get("job_description")),
            salary_text=_salary_text(item),
            apply_method=ApplyMethod.external_link,
            apply_url=item.get("job_apply_link"),
            url=item.get("job_apply_link"),
            posted_at=_posted_at(item),
            # Which underlying board this came from is the single most useful tag here:
            # it is how an operator tells a LinkedIn duplicate from a Greenhouse original.
            tags=[t for t in [item.get("job_publisher")] if isinstance(t, str)],
            raw=item,
        )


def _salary_text(item: dict) -> str | None:
    """Rebuild a human-readable salary string from JSearch's separate fields.

    The parser in `jobagent.salary` reads text, so composing one here keeps a single
    code path for every sense rather than a special case per sense.
    """
    low, high = item.get("job_min_salary"), item.get("job_max_salary")
    if low is None and high is None:
        return None
    currency = item.get("job_salary_currency") or ""
    period = (item.get("job_salary_period") or "").lower()
    suffix = {"year": "per year", "month": "per month", "week": "per week",
              "day": "per day", "hour": "per hour"}.get(period, "")
    amount = f"{low:,.0f} - {high:,.0f}" if low is not None and high is not None \
        else f"{(low if low is not None else high):,.0f}"
    return " ".join(p for p in (currency, amount, suffix) if p)


def _posted_at(item: dict) -> datetime | None:
    """JSearch gives a unix timestamp; some rows give an ISO string instead."""
    stamp = item.get("job_posted_at_timestamp")
    if isinstance(stamp, (int, float)) and stamp > 0:
        try:
            return datetime.fromtimestamp(stamp, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = item.get("job_posted_at_datetime_utc")
    if isinstance(text, str) and text:
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None
