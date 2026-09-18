"""The one ingest → match → summary forage.

Three things used to run "a forage": `scripts/pipeline.py` (with a digest), the API's
`_ingest_task` behind `POST /ingest` (no summary row at all), and `scripts/ingest.py`.
Each had its own idea of the lock, the gate and the ledger. The agent's `pull_jobs` would
have been a fourth. This module is the seam they all call, so a forage means one thing.

Concurrency contract (audit M5): one forage at a time per store, guarded by the
`pipeline` advisory lock with a 2 h TTL. A caller that already took the lock under
`run_id` — the API does, synchronously, so the client learns about a running forage
before the 202 — passes `lock_held=True`; the lock is released here either way.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from jobagent.core.schemas import Event
from jobagent.ingestion.gate import ALL_SOURCES, IngestGate
from jobagent.ingestion.registry import build_adapters
from jobagent.ingestion.runner import RunReport, run_ingestion
from jobagent.matching import run_matching
from jobagent.matching.engine import MatchReport

LOCK_NAME = "pipeline"


class UnknownSource(ValueError):
    def __init__(self, unknown):
        self.unknown = sorted(unknown)
        super().__init__(f"unknown source(s): {self.unknown}. Known: {ALL_SOURCES}")


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class PassReport:
    run_id: str
    skipped: str = ""                       # non-empty → nothing ran (lock held elsewhere)
    ingest: RunReport | None = None
    match: MatchReport | None = None
    duration_s: float = 0.0
    gap_hours_before: float | None = None
    summary: dict = field(default_factory=dict)      # the `run` event payload as written


def _ledger(llm) -> dict | None:
    as_dict = getattr(getattr(llm, "ledger", None), "as_dict", None)
    return as_dict() if callable(as_dict) else None


def forage(store, settings, profile, *, llm=None, run_id: str | None = None,
           sources: list[str] | None = None, lock_held: bool = False,
           trigger: str = "pipeline",
           after_match: Callable[[object, PassReport], dict | None] | None = None,
           extra_summary: dict | None = None) -> PassReport:
    """Ingest through the configured gate, score everything, write the `run` row.

    `after_match(store, report)` runs between matching and the summary and may return
    keys to merge into it — the scheduled script uses it to send the digest and record
    how that went. `sources` narrows this forage to named adapters (the agent's choice);
    the enabled set from settings/preferences still applies underneath.
    """
    run_id = run_id or new_run_id()
    report = PassReport(run_id=run_id)
    if not lock_held and not store.try_acquire_lock(LOCK_NAME, run_id):
        report.skipped = "another forage holds the lock (stale locks expire after 2h)"
        return report

    started = time.monotonic()
    try:
        # Validated inside the try so a caller with lock_held=True still gets its
        # lock released by the finally below when `sources` is bad.
        if sources is not None:
            unknown = set(sources) - set(ALL_SOURCES)
            if unknown:
                raise UnknownSource(unknown)
        # Age of the previous successful ingest, measured before this run touches the
        # store — afterwards it always reads as zero.
        report.gap_hours_before = store.pipeline_health()["hours_since_ingest"]
        gate = IngestGate.from_settings(settings)
        adapters = build_adapters(settings)
        if sources is not None:
            adapters = [a for a in adapters if a.source.value in sources]
        report.ingest = run_ingestion(adapters, store, run_id=run_id, gate=gate)
        report.match = run_matching(store, profile, llm=llm, run_id=run_id)

        extra = {"digest": "not attempted", **(extra_summary or {})}
        if after_match is not None:
            extra.update(after_match(store, report) or {})

        report.duration_s = round(time.monotonic() - started, 1)
        ing, m = report.ingest, report.match
        report.summary = {
            "run_id": run_id,
            "trigger": trigger,
            "llm": _ledger(llm),
            "duration_s": report.duration_s,
            "gap_hours_before_run": (round(report.gap_hours_before, 1)
                                     if report.gap_hours_before is not None else None),
            "ingest": {"fetched": ing.total_fetched, "new": ing.total_new,
                       "dropped": ing.total_dropped, "drops": ing.drops_by_reason,
                       "gate": gate.describe(),
                       "sources": [a.source.value for a in adapters],
                       "errors": [r.source for r in ing.results if r.error]},
            "match": {"scored": m.scored, "llm_reranked": m.llm_reranked},
            **extra,
        }
        store.log_event(Event(kind="run", payload=report.summary))
        return report
    finally:
        # An exception in any stage must still free the lock — the TTL is the crash
        # backstop, not the normal path.
        store.release_lock(LOCK_NAME, run_id)
