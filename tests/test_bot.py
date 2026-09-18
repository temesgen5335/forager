"""Bot tests: pure service helpers + notifier. No telegram lib, no network."""

import httpx
import pytest

from jobagent.bot.notify import chunk_text, send_message
from jobagent.bot.service import (
    apply_callback_data,
    apply_preview_text,
    is_owner,
    jobs_text,
    parse_callback_data,
    resolve_ranked_job,
    status_text,
    upskill_report_for,
    upskill_text,
)
from jobagent.core.schemas import JobPosting, Match, Source
from jobagent.store import Store


def test_is_owner_fails_closed():
    assert is_owner(123, 123) is True
    assert is_owner(123, 999) is False
    assert is_owner(123, None) is False   # no owner configured → deny
    assert is_owner(None, 123) is False


def test_status_text_renders_counts():
    text = status_text({
        "total_jobs": 7000, "by_source": {"greenhouse": 5000, "telegram": 222},
        "matches": 7000, "strong_matches": 120, "last_ingest": "2026-06-18T00:00:00",
    })
    assert "Total jobs: 7000" in text
    assert "greenhouse: 5000" in text
    assert "the catch ≥70%: 120" in text


def test_upskill_text_renders_gap_heatmap():
    report = {"n_jobs": 3, "min_score": 0.5,
              "gaps": [{"skill": "kubernetes", "weight": 0.9, "count": 2, "examples": []},
                       {"skill": "aws", "weight": 0.5, "count": 1, "examples": []}],
              "structural": {"seniority": 1}}
    text = upskill_text(report)
    assert "fit ≥ 50%" in text
    assert "kubernetes" in text and "2 job(s)" in text
    assert "seniority (1)" in text
    assert "make upskill" in text


def test_upskill_text_handles_no_gaps():
    text = upskill_text({"n_jobs": 0, "min_score": 0.5, "gaps": [], "structural": {}})
    assert "No recurring skill gaps" in text


def test_upskill_report_for_reads_store(tmp_path):
    store = Store(str(tmp_path / "u.db"))
    store.init_schema()
    jid = store.upsert_job(JobPosting(source=Source.remoteok, title="MLOps", company="Acme"))
    store.upsert_match(Match(job_id=jid, score=0.6, gaps=["must-have not found: kubernetes"]))
    store.close()
    report = upskill_report_for(str(tmp_path / "u.db"), min_score=0.5)
    assert report["gaps"][0]["skill"] == "kubernetes"


def test_jobs_text_from_store(tmp_path):
    store = Store(str(tmp_path / "b.db"))
    store.init_schema()
    jid = store.upsert_job(JobPosting(source=Source.remoteok, title="AI Engineer",
                                      company="Acme", is_remote=True))
    store.upsert_match(Match(job_id=jid, score=0.9, rationale="great fit"))
    text = jobs_text(store, 5)
    assert "AI Engineer" in text
    assert "90%" in text
    store.close()


def test_chunk_text_splits_on_newline_under_limit():
    body = "\n".join(f"line {i}" for i in range(1000))
    chunks = list(chunk_text(body, size=200))
    assert all(len(c) <= 200 for c in chunks)
    assert "".join(c if c.startswith("line 0") else "\n" + c for c in chunks).count("line") == 1000


def test_send_message_chunks_and_posts():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    long_text = "x" * 9000  # forces 3 chunks at 4000
    sent = send_message("tok", 123, long_text, client=client)
    assert sent == 3
    assert len(calls) == 3
    assert "/bottok/sendMessage" in str(calls[0].url)


def test_send_message_requires_creds():
    with pytest.raises(ValueError):
        send_message("", 123, "hi")
    with pytest.raises(ValueError):
        send_message("tok", None, "hi")


def test_callback_data_roundtrip():
    data = apply_callback_data("approve", "abc123")
    assert data == "approve:abc123"
    assert parse_callback_data(data) == ("approve", "abc123")
    assert parse_callback_data("cancel:xyz") == ("cancel", "xyz")


def test_resolve_ranked_job_maps_rank_to_job(tmp_path):
    store = Store(str(tmp_path / "r.db"))
    store.init_schema()
    j1 = store.upsert_job(JobPosting(source=Source.remoteok, title="Top Role", company="A", is_remote=True))
    j2 = store.upsert_job(JobPosting(source=Source.remoteok, title="Second", company="B", is_remote=True))
    store.upsert_match(Match(job_id=j1, score=0.95))
    store.upsert_match(Match(job_id=j2, score=0.80))
    assert resolve_ranked_job(store, 1)["title"] == "Top Role"
    assert resolve_ranked_job(store, 2)["title"] == "Second"
    assert resolve_ranked_job(store, 99) is None      # out of range → None
    assert resolve_ranked_job(store, 0) is None
    store.close()


def test_apply_preview_text_includes_key_fields():
    class Bundle:
        job = {"title": "AI Engineer", "company": "Acme"}
        cv_markdown = "x" * 1200
        cover_letter = "Dear team, I am a great fit."
        email_subject = "Application: AI Engineer"
        email_body = "Hello."
        apply_method = "email"
    text = apply_preview_text(Bundle())
    assert "AI Engineer @ Acme" in text
    assert "Application: AI Engineer" in text
    assert "Approve to send?" in text


def test_apply_preview_warns_for_non_email():
    class Bundle:
        job = {"title": "FE Eng", "company": "Globex"}
        cv_markdown = "x"
        cover_letter = "c"
        email_subject = "s"
        email_body = "b"
        apply_method = "ats_form"
    assert "Phase 4" in apply_preview_text(Bundle())
