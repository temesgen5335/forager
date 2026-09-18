"""`personalagent://` resources — READ tools in JSON clothing.

A resource is application-driven context (Claude Code attaches it with
`@personalagent:<uri>`); a tool is model-driven. Every resource here is *served through*
the governed `execute()` of the READ tool that owns the data, so a resource read leaves
the same intent/decision/result lines a tool call does and there is no second access
path. Codex ignores resources, which is why each of these is also a tool.
"""

from __future__ import annotations

import json

from jobagent.mcp.operator import Operator


def _read(op: Operator, name: str, args: dict, *, as_json: bool) -> str:
    result = op.execute(name, args, ask=None)
    if result.is_error:
        # A refusal or a tool error is content, not a protocol failure: the reader sees
        # the same sentence the model would.
        return result.content
    if as_json:
        if result.data is not None:
            return json.dumps(result.data, indent=2, default=str)
        return json.dumps({"error": result.content}, indent=2, default=str)
    return result.content


def register_resources(server, op: Operator) -> None:
    @server.resource("personalagent://status", name="status", title="Setup and pipeline status",
                     description="What is configured, what is missing, and the next command.",
                     mime_type="application/json")
    def status() -> str:
        return _read(op, "setup_status", {}, as_json=True)

    @server.resource("personalagent://profile", name="profile", title="Search profile",
                     description="Roles, skills, weights, locations, watchlist and source toggles.",
                     mime_type="application/json")
    def profile() -> str:
        return _read(op, "current_profile", {}, as_json=True)

    @server.resource("personalagent://settings", name="settings", title="Pipeline settings",
                     description="Non-secret settings; credential values are never shown.",
                     mime_type="text/plain")
    def settings_view() -> str:
        return _read(op, "current_config", {}, as_json=False)

    @server.resource("personalagent://lifecycle", name="lifecycle", title="Application lifecycle",
                     description="The allowed status moves (R23).", mime_type="application/json")
    def lifecycle() -> str:
        return _read(op, "lifecycle", {}, as_json=True)

    @server.resource("personalagent://matches", name="matches", title="Ranked matches",
                     description="The top unstashed matches, default filters.",
                     mime_type="application/json")
    def matches() -> str:
        return _read(op, "list_matches", {}, as_json=True)

    @server.resource("personalagent://applications", name="applications", title="Applications",
                     description="Applications and their current status.", mime_type="text/plain")
    def applications() -> str:
        return _read(op, "applications", {}, as_json=False)

    @server.resource("personalagent://runs", name="runs", title="Recent runs",
                     description="Recent pipeline passes and their counts.", mime_type="text/plain")
    def runs() -> str:
        return _read(op, "recent_runs", {}, as_json=False)

    @server.resource("personalagent://runs/{run_id}", name="run", title="One run, reconstructed",
                     description="Every event logged under one run id, oldest first.",
                     mime_type="text/plain")
    def run_detail(run_id: str) -> str:
        return _read(op, "run_detail", {"run_id": run_id}, as_json=False)

    @server.resource("personalagent://jobs/{job_id}", name="job", title="One posting",
                     description="Full detail and score for one posting.", mime_type="text/plain")
    def job(job_id: str) -> str:
        return _read(op, "job_detail", {"job_id": job_id}, as_json=False)
