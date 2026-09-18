"""Himalayas sense — free public JSON (https://himalayas.app/jobs/api).

Why this sense: it is remote-first AND ships an explicit `locationRestrictions`
list per posting ("Worldwide", "United States", "Europe", …). That maps straight
onto the `location` field the geo-eligibility scorer reads, so a genuinely global
role and a US-only "remote" role arrive already distinguishable — which is exactly
the signal the watchlist boards lack.

Response shape (defensive — every field is optional and fetched with a fallback):
    {"jobs": [ {"title", "companyName", "locationRestrictions": [...],
                "description", "applicationLink", "guid", "pubDate", ...}, ... ]}
All postings are remote. No API key.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone

import httpx

from jobagent.core.schemas import ApplyMethod, JobPosting, Source
from jobagent.ingestion.base import BaseAdapter
from jobagent.ingestion.util import get_with_retry, make_client, strip_html


class HimalayasAdapter(BaseAdapter):
    source = Source.himalayas
    API_URL = "https://himalayas.app/jobs/api"

    def __init__(self, client: httpx.Client | None = None, limit: int = 100):
        self._client = client
        self._limit = limit

    def fetch(self) -> Iterable[JobPosting]:
        client, owns = make_client(self._client)
        try:
            data = get_with_retry(client, self.API_URL, params={"limit": self._limit}).json()
        finally:
            if owns:
                client.close()

        jobs = data.get("jobs", []) if isinstance(data, dict) else []
        for item in jobs:
            if isinstance(item, dict):
                yield self._normalize(item)

    def _normalize(self, item: dict) -> JobPosting:
        # locationRestrictions is the load-bearing field: it becomes `location`, so the
        # geo scorer sees "Worldwide" vs "United States" without any extra parsing.
        restrictions = item.get("locationRestrictions") or item.get("locations") or []
        if isinstance(restrictions, str):
            restrictions = [restrictions]
        location = ", ".join(str(r) for r in restrictions if r) or "Remote"

        link = item.get("applicationLink") or item.get("guid") or item.get("url")
        tags = [str(t) for t in (item.get("categories") or []) if t]
        tags += [str(s) for s in (item.get("seniority") or []) if s]

        return JobPosting(
            source=Source.himalayas,
            source_job_id=str(item.get("guid") or item.get("id") or link or ""),
            title=item.get("title") or "(untitled)",
            company=item.get("companyName") or item.get("company"),
            location=location,
            is_remote=True,
            description=strip_html(item.get("description")),
            apply_method=ApplyMethod.external_link,
            apply_url=link,
            url=item.get("guid") or link,
            posted_at=_when(item.get("pubDate") or item.get("publicationDate")),
            tags=tags,
            raw=item,
        )


def _when(value) -> datetime | None:
    """Himalayas dates come as a unix timestamp (seconds); tolerate an ISO string too."""
    if value in (None, "", 0):
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        pass
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
