"""Ingestion runner — drives adapters into the store.

For each enabled adapter: fetch postings, upsert (dedup by hash), count new vs.
re-seen, and log one `ingest` event per adapter run. Resilient: a failing adapter
logs an `error` event and the run continues with the rest.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from jobagent.core.schemas import Event
from jobagent.ingestion.base import BaseAdapter
from jobagent.ingestion.gate import IngestGate
from jobagent.store import Store


@dataclass
class AdapterResult:
    source: str
    fetched: int = 0          # postings the adapter yielded
    new: int = 0              # stored and not seen before
    dropped: int = 0          # rejected by the ingest gate, never stored
    drops: dict[str, int] = field(default_factory=dict)   # reason -> count
    error: str | None = None

    @property
    def kept(self) -> int:
        return self.fetched - self.dropped


@dataclass
class RunReport:
    results: list[AdapterResult] = field(default_factory=list)

    @property
    def total_new(self) -> int:
        return sum(r.new for r in self.results)

    @property
    def total_fetched(self) -> int:
        return sum(r.fetched for r in self.results)

    @property
    def total_dropped(self) -> int:
        return sum(r.dropped for r in self.results)

    @property
    def drops_by_reason(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.results:
            for reason, n in r.drops.items():
                out[reason] = out.get(reason, 0) + n
        return out


def run_ingestion(adapters: list[BaseAdapter], store: Store, *, run_id: str | None = None,
                  gate: IngestGate | None = None) -> RunReport:
    """Drive every enabled adapter once.

    `run_id` is the observability spine: the same id rides every event this forage
    emits (here, matching, and the summary row), so one slow or failing run can
    be reconstructed from the events table instead of guessed at from timestamps.

    `gate` rejects postings before they are stored. Drops are counted per reason and
    logged, because a gate that silently ate a source is indistinguishable from a
    source that stopped answering.
    """
    report = RunReport()
    for adapter in adapters:
        src = adapter.source.value
        if not adapter.enabled:
            continue
        result = AdapterResult(source=src)
        try:
            for job in adapter.fetch():
                result.fetched += 1
                reason = gate.reject(job) if gate is not None else None
                if reason:
                    result.dropped += 1
                    result.drops[reason] = result.drops.get(reason, 0) + 1
                    continue
                is_new = store.is_new_job(job)
                store.upsert_job(job)
                if is_new:
                    result.new += 1
            store.log_event(Event(kind="ingest", payload={
                "source": src, "fetched": result.fetched, "new": result.new,
                "kept": result.kept, "dropped": result.dropped, "drops": result.drops,
                "run_id": run_id,
            }))
        except Exception as exc:  # noqa: BLE001 — one bad source must not kill the run
            result.error = f"{type(exc).__name__}: {exc}"
            store.log_event(Event(kind="error", payload={
                "source": src, "error": result.error, "run_id": run_id,
            }))
        report.results.append(result)
    return report
