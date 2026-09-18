"""Shared sense helpers — HTML stripping, client construction, slug parsing,
and the bounded-retry GET every sense uses to satisfy R8 (back off, don't hammer).
"""

from __future__ import annotations

import random
import re
import time

import httpx

_TAG_RE = re.compile(r"<[^>]+>")
USER_AGENT = "personal-job-agent/0.1 (+personal use)"


def strip_html(text: str | None) -> str:
    return _TAG_RE.sub("", text or "").strip()


def make_client(client: httpx.Client | None) -> tuple[httpx.Client, bool]:
    """Return (client, owns). If we created it, caller must close it."""
    if client is not None:
        return client, False
    return httpx.Client(timeout=30, headers={"User-Agent": USER_AGENT}), True


def split_slugs(raw: str) -> list[str]:
    """'acme, globex ,' -> ['acme', 'globex']"""
    return [s.strip() for s in (raw or "").split(",") if s.strip()]


# Worth retrying: rate limits and server-side faults. Everything else (404 for a
# wrong company slug, 401, 400) is permanent — retrying only wastes the run.
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
_RETRY_AFTER_CAP = 60.0


def _backoff(attempt: int, base: float, cap: float, rng) -> float:
    """Full jitter: uniform in [0, ceiling]. Spreads retries so several senses
    failing at the same moment don't come back in lockstep."""
    return min(cap, base * (2**attempt)) * rng()


def _retry_after(resp: httpx.Response) -> float | None:
    """Honor a numeric Retry-After, capped so a hostile value can't wedge the run.
    The HTTP-date form returns None and we fall back to our own backoff."""
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, min(_RETRY_AFTER_CAP, float(raw)))
    except ValueError:
        return None


def get_with_retry(
    client: httpx.Client,
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    sleep=time.sleep,
    rng=random.random,
) -> httpx.Response:
    """GET with bounded exponential backoff + jitter, raising on final failure.

    Returns a 2xx response, or raises the last error once attempts are exhausted —
    so callers keep their existing `except (httpx.HTTPError, ValueError)` handling and
    a dead sense still degrades to "skip this sense" rather than killing the run.

    `params` and `headers` exist because an authenticated sense (JSearch sends a
    RapidAPI key) still has to come through here — R21 forbids calling `client.get`
    directly, and a sense that needed headers would otherwise have no legitimate
    route and would quietly lose its retry/backoff.

    `sleep` and `rng` are injectable so tests are instant and deterministic.
    """
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            resp = client.get(url, params=params, headers=headers)
            if resp.status_code in _RETRY_STATUS and attempt < attempts - 1:
                sleep(_retry_after(resp) or _backoff(attempt, base_delay, max_delay, rng))
                continue
            resp.raise_for_status()
            return resp
        except httpx.TransportError as exc:   # timeouts, connection resets, DNS
            last = exc
            if attempt >= attempts - 1:
                break
            sleep(_backoff(attempt, base_delay, max_delay, rng))
    raise last if last is not None else httpx.HTTPError(f"no attempts made for {url}")
