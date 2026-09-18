"""Remotive sense — free public JSON (https://remotive.com/api/remote-jobs).

Response shape: {"job-count": N, "jobs": [ {...}, ... ]}. All jobs are remote.
Note: Remotive's API is for personal/redistribution-limited use; we only filter
for our own consumption (R7).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

import httpx

from jobagent.core.schemas import ApplyMethod, JobPosting, Source
from jobagent.ingestion.base import BaseAdapter
from jobagent.ingestion.util import get_with_retry, make_client, strip_html


class RemotiveAdapter(BaseAdapter):
    source = Source.remotive
    API_URL = "https://remotive.com/api/remote-jobs"

    def __init__(self, client: httpx.Client | None = None):
        self._client = client

    def fetch(self) -> Iterable[JobPosting]:
        client, owns = make_client(self._client)
        try:
            jobs = get_with_retry(client, self.API_URL).json().get("jobs", [])
        finally:
            if owns:
                client.close()

        for item in jobs:
            if isinstance(item, dict):
                yield self._normalize(item)

    def _normalize(self, item: dict) -> JobPosting:
        return JobPosting(
            source=Source.remotive,
            source_job_id=str(item.get("id")),
            title=item.get("title") or "(untitled)",
            company=item.get("company_name"),
            location=item.get("candidate_required_location") or "Remote",
            is_remote=True,
            description=strip_html(item.get("description")),
            salary_text=item.get("salary") or None,
            apply_method=ApplyMethod.external_link,
            apply_url=item.get("url"),
            url=item.get("url"),
            posted_at=_iso(item.get("publication_date")),
            tags=[t for t in (item.get("tags") or []) if isinstance(t, str)],
            raw=item,
        )


def _iso(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
