"""LLM reranker — scores the top heuristic candidates with real fit analysis.

Uses the shared LLM (`.complete` on agentkit's LLMService), so it inherits multi-provider
failover, the circuit breaker, and the OpenRouter free-model fan-out.
"""

from __future__ import annotations

import json

from jobagent.preferences import Profile

_SYSTEM = (
    "You are a job-fit assessor for a specific candidate. Given the candidate "
    "profile and a job posting, return STRICT JSON: "
    '{"score": <0..1 float>, "rationale": "<one sentence>", '
    '"gaps": ["<missing requirement>", ...]}. '
    "Score reflects genuine fit for THIS candidate. Be honest about gaps. "
    "Do not invent candidate qualifications."
)


def _profile_blurb(p: Profile) -> str:
    return (
        f"Candidate: {p.headline}\n"
        f"Target roles: {', '.join(p.target_roles)}\n"
        f"Seniority: {p.seniority} | Work mode: {p.work_mode} | Location: {p.location} ({p.timezone})\n"
        f"Core skills: {', '.join(p.core_skills)}\n"
        f"Domains of interest: {', '.join(p.domains)}\n"
        f"Must-haves: {', '.join(p.must_haves)}"
    )


def llm_score(job: dict, profile: Profile, llm) -> tuple[float, str, list[str]] | None:
    """Return (score, rationale, gaps) or None if the call/parse fails."""
    user = (
        f"{_profile_blurb(profile)}\n\n"
        f"JOB\nTitle: {job.get('title')}\nCompany: {job.get('company')}\n"
        f"Location: {job.get('location')} | Remote: {bool(job.get('is_remote'))}\n"
        f"Description (truncated):\n{(job.get('description') or '')[:4000]}"
    )
    try:
        raw = llm.complete(_SYSTEM, user, json_mode=True)
        data = json.loads(raw)
        score = max(0.0, min(1.0, float(data["score"])))
        return round(score, 3), str(data.get("rationale", "")), list(data.get("gaps", []))
    except Exception:  # noqa: BLE001 — never let hunting break the run
        return None
