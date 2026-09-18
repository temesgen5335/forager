"""The bridge: every MCP tool is a governed tool, every confirmation is a person's.

Verified against the SDK's in-memory client, which speaks both protocol eras. Nothing
here touches the network (R17).
"""

import sqlite3

import anyio
import pytest

mcp = pytest.importorskip("mcp")
from mcp import Client  # noqa: E402
from mcp.types import ElicitResult  # noqa: E402

from jobagent.assistant.operator_tools import OperatorDeps  # noqa: E402
from jobagent.config import Settings  # noqa: E402
from jobagent.core.schemas import JobPosting, Match  # noqa: E402
from jobagent.mcp import build_server  # noqa: E402
from jobagent.mcp.operator import Operator  # noqa: E402
from jobagent.store.db import Store  # noqa: E402


def _settings(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBAGENT_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOBAGENT_PROFILE_PATH", str(tmp_path / "profile.json"))
    monkeypatch.setenv("JOBAGENT_CV_PATH", str(tmp_path / "cv.md"))
    return Settings(_env_file=None)


def _deps(tmp_path):
    return OperatorDeps(db_path=str(tmp_path / "t.db"), local_path=str(tmp_path / "none.toml"),
                        overlay_path=str(tmp_path / "profile.json"), cv_loader=lambda: "",
                        llm_factory=lambda: None, env_path=str(tmp_path / ".env"),
                        spawn=lambda fn: fn())


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """(server, operator, db_path). A fresh store, seeded with two scored jobs."""
    settings = _settings(tmp_path, monkeypatch)
    db = str(tmp_path / "t.db")
    s = Store(db)
    s.init_schema()
    ids = []
    for i in range(2):
        jid = s.upsert_job(JobPosting(title=f"AI Engineer {i}", company="Acme", source="remoteok",
                                      url=f"http://x/{i}", location="Remote", description="python"))
        s.upsert_match(Match(job_id=jid, score=0.8, rationale="fits"))
        ids.append(jid)
    s.close()
    op = Operator(settings, deps=_deps(tmp_path))
    server = build_server(settings, operator=op)
    yield server, op, db, ids
    op.close()


def _accepting(seen):
    async def cb(context, params):
        seen.append(params.message)
        return ElicitResult(action="accept", content={"ok": True})
    return cb


async def _declining(context, params):
    return ElicitResult(action="decline")


def _events(db, run_id):
    s = Store(db)
    try:
        return s.events_for_run(run_id)
    finally:
        s.close()


def test_the_tool_list_is_the_governed_toolbox(rig):
    server, op, _, _ = rig

    async def main():
        async with Client(server, raise_exceptions=True) as c:
            first = (await c.list_tools()).tools
            second = (await c.list_tools(cache_mode="refresh")).tools
            return first, second

    tools, again = anyio.run(main)
    assert [t.name for t in tools] == [s.name for s in op.specs()]      # same order, every time
    assert [t.name for t in tools] == [t.name for t in again]
    by_name = {t.name: t for t in tools}
    triage = by_name["triage"]
    spec = next(s for s in op.specs() if s.name == "triage")
    assert set(triage.input_schema["properties"]) == set(spec.parameters["properties"])
    assert set(triage.input_schema.get("required", [])) == set(spec.parameters["required"])
    assert triage.input_schema["properties"]["state"]["enum"] == ["dismissed", "snoozed", "active"]
    assert all(p.get("description") for p in triage.input_schema["properties"].values())
    assert "approval" not in triage.input_schema["properties"] and "ctx" not in triage.input_schema["properties"]
    assert "apply_config_change" not in by_name and "rollback_config" not in by_name   # hidden without --admin
    assert not any(w in n for n in by_name for w in ("send", "submit", "approve", "apply_to", "ats"))


def test_annotations_are_derived_from_the_policy(rig):
    server, _, _, _ = rig

    async def main():
        async with Client(server, raise_exceptions=True) as c:
            return {t.name: t.annotations for t in (await c.list_tools()).tools}

    ann = anyio.run(main)
    assert ann["pipeline_health"].read_only_hint is True and ann["pipeline_health"].idempotent_hint is True
    assert ann["triage"].read_only_hint is False and ann["triage"].destructive_hint is False
    assert ann["set_application_status"].destructive_hint is True
    assert ann["pull_jobs"].open_world_hint is True and ann["triage"].open_world_hint is False


def test_a_read_tool_runs_without_asking_and_is_audited(rig):
    server, op, db, _ = rig
    asked = []

    async def main():
        async with Client(server, raise_exceptions=True, elicitation_callback=_accepting(asked)) as c:
            return await c.call_tool("pipeline_health", {})

    r = anyio.run(main)
    assert not r.is_error and "jobs=2" in r.content[0].text
    assert asked == []
    kinds = [e["kind"] for e in _events(db, op.run_id)]
    assert {"tool_intent", "tool_decision", "tool_result"} <= set(kinds)


def test_an_action_asks_once_then_is_trusted_for_the_session(rig):
    server, op, db, ids = rig
    shown = []

    async def main():
        async with Client(server, raise_exceptions=True, elicitation_callback=_accepting(shown)) as c:
            a = await c.call_tool("triage", {"job_id": ids[0], "state": "dismissed"})
            # A second ACT call, different arguments: the session grant covers it without
            # asking again. Both persist ("dismissed" is the tool's durable state; a
            # duration-less "snooze" lapses on read, which is the triage tool's own
            # behavior, not the bridge's — and not what this test is about).
            b = await c.call_tool("triage", {"job_id": ids[1], "state": "dismissed"})
            return a, b

    a, b = anyio.run(main)
    assert not a.is_error and not b.is_error
    assert len(shown) == 1 and "job_id" in shown[0] and "dismissed" in shown[0]
    s = Store(db)
    try:
        assert s.get_triage(ids[0])["state"] == "dismissed"
        assert s.get_triage(ids[1])["state"] == "dismissed"
    finally:
        s.close()
    decisions = [e for e in _events(db, op.run_id) if e["kind"] == "tool_decision"]
    assert [d["decision"] for d in decisions][-2:] == ["allow", "allow"]


def test_a_declined_confirmation_is_a_refusal_with_an_intent_line(rig):
    server, op, db, ids = rig

    async def main():
        async with Client(server, raise_exceptions=True, elicitation_callback=_declining) as c:
            return await c.call_tool("triage", {"job_id": ids[0], "state": "dismissed"})

    r = anyio.run(main)
    assert r.is_error and "declined by the operator" in r.content[0].text
    events = _events(db, op.run_id)
    assert any(e["kind"] == "tool_intent" and e["tool"] == "triage" for e in events)
    assert any(e["kind"] == "tool_decision" and e["decision"] == "deny" for e in events)
    s = Store(db)
    try:
        assert s.get_triage(ids[0]) is None
    finally:
        s.close()


def test_a_client_without_elicitation_cannot_confirm_anything(rig):
    server, op, _, ids = rig

    async def main():
        async with Client(server, raise_exceptions=True) as c:          # no elicitation_callback
            return await c.call_tool("triage", {"job_id": ids[0], "state": "dismissed"})

    r = anyio.run(main)
    assert r.is_error
    assert "no confirmation channel" in r.content[0].text and op.base_url in r.content[0].text


def test_a_bad_argument_is_an_execution_error_the_model_can_fix(rig):
    server, _, _, ids = rig

    async def main():
        async with Client(server) as c:
            return await c.call_tool("triage", {"job_id": ids[0], "state": "bogus"})

    r = anyio.run(main)
    assert r.is_error and "state" in r.content[0].text


def test_structured_content_rides_beside_the_text(rig):
    server, _, _, _ = rig

    async def main():
        async with Client(server, raise_exceptions=True) as c:
            return await c.call_tool("list_matches", {"min_score": 0.1})

    r = anyio.run(main)
    assert not r.is_error
    assert r.structured_content["total"] == 2 and len(r.structured_content["rows"]) == 2


def test_a_broken_audit_trail_stops_every_call_before_the_gate(rig):
    server, op, _, _ = rig

    class BrokenSink:
        def emit(self, kind, payload):
            raise sqlite3.OperationalError("disk I/O error")

    healthy = op.run(lambda: op.assistant.auditor.sink)
    op.run(lambda: setattr(op.assistant.auditor, "sink", BrokenSink()))

    async def main():
        async with Client(server) as c:
            return await c.call_tool("pipeline_health", {})

    r = anyio.run(main)
    assert r.is_error and "trail is unavailable" in r.content[0].text
    # The injected failure is over; hand the working sink back so the session can close
    # onto the ledger at teardown (op.close() writes its final `run` line through it).
    op.run(lambda: setattr(op.assistant.auditor, "sink", healthy))


def test_admin_tools_appear_only_when_the_server_was_launched_with_admin(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    op = Operator(settings, admin=True, deps=_deps(tmp_path))
    server = build_server(settings, operator=op)

    async def main():
        async with Client(server, raise_exceptions=True) as c:
            return {t.name for t in (await c.list_tools()).tools}

    try:
        names = anyio.run(main)
        assert {"apply_config_change", "rollback_config"} <= names
    finally:
        op.close()
