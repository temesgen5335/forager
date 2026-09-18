"""Builds the configured set of senses from settings + watchlist.
Shared by the ingest CLI and the deployment pipeline so they never drift."""

from __future__ import annotations

from jobagent.ingestion.adapters.ashby import AshbyAdapter
from jobagent.ingestion.adapters.greenhouse import GreenhouseAdapter
from jobagent.ingestion.adapters.himalayas import HimalayasAdapter
from jobagent.ingestion.adapters.jsearch import JSearchAdapter
from jobagent.ingestion.adapters.lever import LeverAdapter
from jobagent.ingestion.adapters.remoteok import RemoteOKAdapter
from jobagent.ingestion.adapters.remotive import RemotiveAdapter
from jobagent.ingestion.adapters.telegram import TelegramAdapter
from jobagent.ingestion.base import BaseAdapter
from jobagent.ingestion.gate import resolve_sources
from jobagent.ingestion.util import split_slugs
from jobagent.preferences import load_preferences


def _merge(*lists: list[str]) -> list[str]:
    """Union, order-preserving, de-duplicated."""
    seen: set[str] = set()
    out: list[str] = []
    for lst in lists:
        for s in lst:
            if s and s not in seen:
                seen.add(s)
                out.append(s)
    return out


def build_adapters(settings) -> list[BaseAdapter]:
    """Company watchlist comes from config/preferences.toml; env vars supplement
    slugs/channels.

    Which senses run is resolved by `gate.resolve_sources`: the dashboard-editable
    `ingest_sources` wins when set, otherwise `[sources]` in preferences.toml. A sense
    excluded there is dropped here; included ones still self-gate on creds/slugs via
    their own `enabled` property, so selecting Telegram without credentials is a no-op
    rather than an error."""
    prefs = load_preferences()
    wl, src = prefs.watchlist, prefs.sources
    all_adapters = [
        RemoteOKAdapter(),
        RemotiveAdapter(),
        HimalayasAdapter(),
        GreenhouseAdapter(_merge(wl.greenhouse, split_slugs(settings.greenhouse_slugs))),
        LeverAdapter(_merge(wl.lever, split_slugs(settings.lever_slugs))),
        AshbyAdapter(_merge(wl.ashby, split_slugs(settings.ashby_slugs))),
        TelegramAdapter(
            settings.telegram_api_id,
            settings.telegram_api_hash,
            split_slugs(settings.telegram_channels),
            session=settings.telegram_session,
            limit=settings.telegram_fetch_limit,
        ),
        # Queries come from the profile's own target roles, so the aggregator searches
        # for what the operator is looking for rather than a hardcoded list. It
        # self-gates on the API key AND on having something to search for — an empty
        # query would return an arbitrary slice of every job on the internet.
        JSearchAdapter(
            settings.jsearch_api_key,
            prefs.profile.target_roles,
            location=settings.jsearch_location or prefs.profile.location,
            remote_only=(prefs.profile.work_mode or "").lower() == "remote",
        ),
    ]
    allowed = resolve_sources(settings, src)
    return [a for a in all_adapters if a.source.value in allowed]
