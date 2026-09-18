"""One forage, three callers: the scheduled script, POST /ingest, and the agent's pull_jobs."""

import pytest

from jobagent import pipeline
from jobagent.config import Settings
from jobagent.core.schemas import JobPosting, Source
from jobagent.ingestion.base import BaseAdapter
from jobagent.pipeline import LOCK_NAME, PassReport, UnknownSource, forage
from jobagent.preferences import Preferences
from jobagent.store.db import Store


class FakeAdapter(BaseAdapter):
    source = Source.remoteok

    def __init__(self, postings):
        self._postings = postings

    def fetch(self):
        return iter(self._postings)

    @property
    def enabled(self) -> bool:
        return True


def _postings(n=2):
    return [JobPosting(title=f"AI Engineer {i}", company=f"Co{i}", source="remoteok",
                       url=f"http://x/{i}", location="Remote", description="python llm")
            for i in range(n)]


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    s.init_schema()
    yield s
    s.close()


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBAGENT_DB_PATH", str(tmp_path / "t.db"))
    return Settings(_env_file=None)


@pytest.fixture
def adapters(monkeypatch):
    monkeypatch.setattr(pipeline, "build_adapters", lambda settings: [FakeAdapter(_postings())])


def test_a_forage_ingests_matches_and_writes_one_run_summary(store, settings, adapters):
    report = forage(store, settings, Preferences().profile, trigger="test")
    assert isinstance(report, PassReport) and not report.skipped
    assert report.ingest.total_new == 2 and report.match.scored == 2
    runs = store.list_runs()
    assert len(runs) == 1 and runs[0]["run_id"] == report.run_id
    assert runs[0]["trigger"] == "test" and runs[0]["ingest"]["new"] == 2
    assert runs[0]["digest"] == "not attempted"
    # Every event of the forage carries the run id (the observability spine).
    assert {e["kind"] for e in store.events_for_run(report.run_id)} >= {"ingest", "match", "run"}


def test_the_lock_is_released_after_a_forage(store, settings, adapters):
    forage(store, settings, Preferences().profile)
    assert store.try_acquire_lock(LOCK_NAME, "someone-else")
    store.release_lock(LOCK_NAME, "someone-else")


def test_a_forage_is_skipped_when_another_holds_the_lock(store, settings, adapters):
    assert store.try_acquire_lock(LOCK_NAME, "other")
    report = forage(store, settings, Preferences().profile)
    assert report.skipped and report.ingest is None
    assert store.list_runs() == []
    store.release_lock(LOCK_NAME, "other")


def test_a_caller_that_already_holds_the_lock_says_so_and_it_is_still_released(store, settings, adapters):
    assert store.try_acquire_lock(LOCK_NAME, "abc123")
    report = forage(store, settings, Preferences().profile, run_id="abc123", lock_held=True)
    assert not report.skipped and report.run_id == "abc123"
    assert store.try_acquire_lock(LOCK_NAME, "next")       # released by forage
    store.release_lock(LOCK_NAME, "next")


def test_sources_narrow_the_adapters_and_unknown_names_are_refused(store, settings, adapters):
    narrowed = forage(store, settings, Preferences().profile, sources=["remotive"])
    assert narrowed.ingest.total_fetched == 0
    with pytest.raises(UnknownSource):
        forage(store, settings, Preferences().profile, sources=["linkedin"])


def test_bad_sources_with_a_held_lock_still_releases_it(store, settings, adapters):
    assert store.try_acquire_lock(LOCK_NAME, "abc123")
    with pytest.raises(UnknownSource):
        forage(store, settings, Preferences().profile, run_id="abc123",
               lock_held=True, sources=["linkedin"])
    # forage owns release under this run_id even on the error path
    assert store.try_acquire_lock(LOCK_NAME, "next")
    store.release_lock(LOCK_NAME, "next")


def test_after_match_and_extra_summary_land_on_the_run_event(store, settings, adapters):
    seen = []

    def digest(store_, report):
        seen.append(report.match.scored)
        return {"digest": "sent (1 message)"}

    forage(store, settings, Preferences().profile, after_match=digest,
           extra_summary={"agent_session": "sess0001"})
    row = store.list_runs()[0]
    assert seen == [2] and row["digest"] == "sent (1 message)" and row["agent_session"] == "sess0001"
