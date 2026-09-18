"""The hunt engine — score stored jobs against the profile, persist Matches.

Strategy: heuristic-score every job (cheap, always), then optionally LLM-rerank
the strongest heuristic candidates when an OpenRouter key is available.
"""

from __future__ import annotations

from dataclasses import dataclass

from jobagent.core.schemas import Event, Match
from jobagent.matching.heuristic import heuristic_score
from jobagent.matching.llm import llm_score
from jobagent.preferences import Profile
from jobagent.store import Store


@dataclass
class MatchReport:
    scored: int = 0
    llm_reranked: int = 0
    used_llm: bool = False


def run_matching(
    store: Store,
    profile: Profile,
    *,
    llm=None,
    llm_top_k: int = 30,
    llm_threshold: float = 0.45,
    run_id: str | None = None,
) -> MatchReport:
    """Score all jobs heuristically; LLM-rerank the top candidates if an llm is given."""
    report = MatchReport()
    jobs = store.get_jobs()

    scored: list[tuple[dict, float]] = []
    for job in jobs:
        score, rationale, gaps = heuristic_score(job, profile)
        store.upsert_match(Match(job_id=job["id"], score=score, rationale=rationale, gaps=gaps))
        report.scored += 1
        scored.append((job, score))

    if llm is not None:
        report.used_llm = True
        candidates = sorted(scored, key=lambda x: x[1], reverse=True)
        candidates = [j for j, s in candidates if s >= llm_threshold][:llm_top_k]
        for job in candidates:
            result = llm_score(job, profile, llm)
            if result is not None:
                score, rationale, gaps = result
                store.upsert_match(
                    Match(job_id=job["id"], score=score, rationale=f"[LLM] {rationale}",
                          gaps=gaps, score_source="llm", llm_score=score)
                )
                report.llm_reranked += 1

    # The hunt previously logged nothing, so the trail showed ingestion and then
    # silence — a hunt that crashed or scored zero jobs was invisible.
    store.log_event(Event(kind="match", payload={
        "scored": report.scored, "llm_reranked": report.llm_reranked,
        "used_llm": report.used_llm, "run_id": run_id,
    }))
    return report
