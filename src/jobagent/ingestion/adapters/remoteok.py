"""RemoteOK sense — free public JSON feed (https://remoteok.com/api).

The feed is a JSON array whose FIRST element is a legal/metadata notice, not a job;
we skip it. RemoteOK blocks requests without a User-Agent, so we always send one.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime, timezone

import httpx

from jobagent.core.schemas import ApplyMethod, JobPosting, Source
from jobagent.ingestion.base import BaseAdapter
from jobagent.ingestion.util import get_with_retry

_TAG_RE = re.compile(r"<[^>]+>")
_USER_AGENT = "personal-job-agent/0.1 (+personal use)"


class RemoteOKAdapter(BaseAdapter):
    source = Source.remoteok
    API_URL = "https://remoteok.com/api"

    def __init__(self, client: httpx.Client | None = None):
        # An injected client lets tests drive a MockTransport; prod builds its own.
        self._client = client

    def fetch(self) -> Iterable[JobPosting]:
        client = self._client or httpx.Client(
            timeout=30, headers={"User-Agent": _USER_AGENT}
        )
        try:
            data = get_with_retry(client, self.API_URL).json()
        finally:
            if self._client is None:
                client.close()

        for item in data:
            # Skip the legal-notice header and any non-job element defensively.
            if not isinstance(item, dict) or "id" not in item or item.get("legal"):
                continue
            yield self._normalize(item)

    def _normalize(self, item: dict) -> JobPosting:
        apply_url = item.get("apply_url") or item.get("url")
        apply_method = ApplyMethod.external_link
        apply_email = None
        if isinstance(apply_url, str) and apply_url.lower().startswith("mailto:"):
            apply_method = ApplyMethod.email
            apply_email = apply_url.split(":", 1)[1].split("?", 1)[0]

        return JobPosting(
            source=Source.remoteok,
            source_job_id=str(item.get("id")),
            title=item.get("position") or item.get("title") or "(untitled)",
            company=item.get("company"),
            location=item.get("location") or "Remote",
            is_remote=True,
            description=_strip_html(item.get("description", "")),
            salary_text=_salary(item),
            apply_method=apply_method,
            apply_url=apply_url,
            apply_email=apply_email,
            url=item.get("url"),
            posted_at=_epoch(item.get("epoch")),
            tags=[t for t in (item.get("tags") or []) if isinstance(t, str)],
            raw=item,
        )


def _strip_html(text: str) -> str:
    return _TAG_RE.sub("", text or "").strip()


def _salary(item: dict) -> str | None:
    lo, hi = item.get("salary_min"), item.get("salary_max")
    if lo and hi:
        return f"${lo:,}–${hi:,}"
    return None


def _epoch(epoch) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc) if epoch else None
    except (ValueError, TypeError, OSError):
        return None
