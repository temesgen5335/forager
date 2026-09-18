"""M1: pipeline health — staleness, error counts, per-source freshness, digest banner.

The bug being prevented: a pipeline that has been failing for three days renders
identically to a healthy one, because the store simply stops growing and nothing
says so.
"""

from datetime import datetime, timedelta, timezone

from jobagent.core.schemas import Event
from jobagent.digest import health_banner
from jobagent.ingestion.runner import AdapterResult, RunReport
from jobagent.store import Store


def _store(tmp_path):
    s = Store(str(tmp_path / "h.db"))
    s.init_schema()
    return s


def _ingest_event(store, source, *, hours_ago=0.0, fetched=10, new=3):
    """Insert an ingest event with a backdated timestamp."""
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    store.conn.execute(
        "INSERT INTO events (kind, job_id, payload, created_at) VALUES ('ingest', NULL, ?, ?)",
        (f'{{"source": "{source}", "fetched": {fetched}, "new": {new}}}', ts),
    )
    store.conn.commit()


def test_fresh_install_reads_as_stale(tmp_path):
    """No ingest ever recorded and a three-day-dead pipeline should both read as
    'not currently working' — never as healthy."""
    s = _store(tmp_path)
    h = s.pipeline_health()
    assert h["is_stale"] is True
    assert h["last_ingest"] is None and h["hours_since_ingest"] is None
    assert h["sources"] == [] and h["recent_errors"] == 0
    s.close()


def test_recent_ingest_is_not_stale(tmp_path):
    s = _store(tmp_path)
    _ingest_event(s, "remoteok", hours_ago=1)
    h = s.pipeline_health()
    assert h["is_stale"] is False
    assert 0.5 < h["hours_since_ingest"] < 1.5
    s.close()


def test_old_ingest_is_stale(tmp_path):
    s = _store(tmp_path)
    _ingest_event(s, "remoteok", hours_ago=72)
    h = s.pipeline_health(stale_after_hours=24)
    assert h["is_stale"] is True
    assert h["hours_since_ingest"] > 24
    s.close()


def test_per_source_freshness_takes_the_latest_row_per_source(tmp_path):
    s = _store(tmp_path)
    _ingest_event(s, "remoteok", hours_ago=50, fetched=1, new=1)
    _ingest_event(s, "remoteok", hours_ago=2, fetched=9, new=4)   # newer wins
    _ingest_event(s, "greenhouse", hours_ago=80, fetched=5, new=0)
    by = {r["source"]: r for r in s.pipeline_health()["sources"]}
    assert set(by) == {"remoteok", "greenhouse"}
    assert by["remoteok"]["fetched"] == 9 and by["remoteok"]["new"] == 4
    assert by["remoteok"]["hours_since"] < 3
    assert by["greenhouse"]["hours_since"] > 48
    s.close()


def test_error_count_windowed_and_last_error_surfaced(tmp_path):
    s = _store(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    s.conn.execute(
        "INSERT INTO events (kind, payload, created_at) VALUES ('error', ?, ?)",
        ('{"source": "lever", "error": "ancient"}', old),
    )
    s.conn.commit()
    s.log_event(Event(kind="error", payload={"source": "ashby", "error": "HTTPStatusError: 503"}))

    h = s.pipeline_health(error_window_hours=24)
    assert h["recent_errors"] == 1                        # the 48h-old one is outside the window
    assert h["last_error"]["source"] == "ashby"
    assert "503" in h["last_error"]["error"]
    s.close()


def test_malformed_error_payload_does_not_crash_health(tmp_path):
    s = _store(tmp_path)
    s.conn.execute(
        "INSERT INTO events (kind, payload, created_at) VALUES ('error', 'not-json', ?)",
        (datetime.now(timezone.utc).isoformat(),),
    )
    s.conn.commit()
    h = s.pipeline_health()
    assert h["last_error"]["source"] is None              # degraded, not exploded
    s.close()


def test_stats_embeds_health(tmp_path):
    s = _store(tmp_path)
    assert "health" in s.stats() and s.stats()["health"]["is_stale"] is True
    s.close()


# --- digest heartbeat banner ------------------------------------------------------

def _report(*results):
    return RunReport(results=list(results))


def test_banner_is_empty_on_a_clean_run():
    r = _report(AdapterResult(source="remoteok", fetched=10, new=2))
    assert health_banner(r, {"sources": []}) == ""       # no noise when all is well


def test_banner_names_failed_sources():
    r = _report(
        AdapterResult(source="remoteok", fetched=10, new=2),
        AdapterResult(source="lever", error="ConnectTimeout: dead"),
    )
    out = health_banner(r, {"sources": []})
    assert "1 sense(s) failed" in out and "lever" in out and "ConnectTimeout" in out
    assert out.endswith("\n\n")                          # separates from the digest body


def test_banner_flags_a_totally_empty_fetch():
    r = _report(AdapterResult(source="remoteok", fetched=0, new=0))
    assert "No postings fetched" in health_banner(r, {"sources": []})


def test_banner_flags_sources_gone_quiet():
    r = _report(AdapterResult(source="remoteok", fetched=5, new=1))
    health = {"sources": [{"source": "telegram", "hours_since": 99.0},
                          {"source": "remoteok", "hours_since": 1.0}]}
    out = health_banner(r, health)
    assert "telegram" in out and "remoteok" not in out.split("48h+")[-1]


# --- Phase 0b: the silence alarm ---------------------------------------------------

def test_banner_flags_a_skipped_schedule():
    """A daily job whose previous run was 90 hours ago means the scheduler stopped.
    This is the only signal that survives 'nothing ran at all' — it fires the first
    time the pipeline comes back."""
    r = _report(AdapterResult(source="remoteok", fetched=10, new=2))
    out = health_banner(r, {"sources": []}, gap_hours=90, expected_every_hours=24)
    assert "First successful run in 3.8 days" in out
    assert "scheduler was not running" in out


def test_banner_silent_on_a_normal_gap():
    """A daily job running ~daily must not warn — routine noise gets ignored."""
    r = _report(AdapterResult(source="remoteok", fetched=10, new=2))
    assert health_banner(r, {"sources": []}, gap_hours=25, expected_every_hours=24) == ""


def test_first_ever_run_is_not_a_missed_schedule():
    """No previous ingest → gap is None. A fresh install must not be reported as a
    broken scheduler."""
    r = _report(AdapterResult(source="remoteok", fetched=10, new=2))
    assert health_banner(r, {"sources": []}, gap_hours=None) == ""


def test_gap_warning_is_opt_in():
    """Callers that don't pass gap_hours keep the old behavior exactly."""
    r = _report(AdapterResult(source="remoteok", fetched=10, new=2))
    assert health_banner(r, {"sources": []}) == ""


def test_gap_warning_combines_with_other_warnings():
    r = _report(AdapterResult(source="lever", error="ConnectTimeout: dead"))
    out = health_banner(r, {"sources": []}, gap_hours=100)
    assert "scheduler was not running" in out and "lever" in out
