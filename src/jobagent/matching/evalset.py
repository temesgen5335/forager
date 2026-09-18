"""Labeled evaluation set + metrics for the heuristic hunt.

This is the regression net for hunt quality. The suite proves the hunt *runs*;
this proves it still *ranks well*: a labeled set of clearly-relevant and
clearly-irrelevant postings — including every trap class that has actually bitten
(substring hits, seniority mismatches, requirement-mirroring boilerplate, exclusions,
tag-flooding) — with precision/recall/separation metrics over the ranked output.

Evaluated against EVAL_PROFILE, a frozen profile that mirrors the shipped default's
*shape* (AI/full-stack engineer, remote must-have, weighted skills). Deliberately NOT
the user's live preferences: editing config must never change what these metrics mean.

tests/test_eval_matching.py asserts floors so a hunt-logic change that degrades ranking
fails CI. scripts/eval_matching.py prints the full table for tuning. Floors are set
at measured reality (minus a small epsilon), not aspiration — raising them is the
tuning goal, and known misses are labeled in the dataset rather than hidden.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from jobagent.matching.heuristic import heuristic_score
from jobagent.preferences import Profile

EVAL_PROFILE = Profile(
    target_roles=["AI Engineer", "Software Engineer", "Full-Stack Engineer",
                  "Frontend Engineer", "Machine Learning Engineer"],
    seniority="mid-to-senior",
    # Opt into geo hunting: only genuinely global-remote postings are eligible, and the
    # user has explicitly allow-listed EMEA. Exercises both the scope switch and the
    # configurable include list. (Kept independent of the live user's config, per contract.)
    remote_scope="global",
    geo_eligible=["emea"],
    core_skills=["Python", "TypeScript", "FastAPI", "Next.js", "React",
                 "LangChain", "RAG", "LLM fine-tuning", "agentic systems",
                 "Docker", "AWS", "PostgreSQL", "CI/CD"],
    skill_weights={"LangChain": 2.0, "RAG": 2.0, "agentic systems": 2.0, "Python": 2.0,
                   "FastAPI": 1.5, "Next.js": 1.5,
                   "Docker": 0.5, "AWS": 0.5, "PostgreSQL": 0.5, "CI/CD": 0.5},
    domains=["agentic AI", "developer tools", "AI infrastructure"],
    must_haves=["remote"],
    exclude_keywords=["unpaid", "US citizens only", "clearance required", "on-site only"],
    keywords=["AI engineer", "agent", "LLM", "full-stack", "Python", "React"],
)


@dataclass
class EvalJob:
    title: str
    description: str
    relevant: bool
    note: str = ""                      # why this example exists / which trap it guards
    location: str = "Remote"
    is_remote: bool = True
    tags: list[str] = field(default_factory=list)

    def as_row(self) -> dict:
        return {"title": self.title, "description": self.description,
                "location": self.location, "is_remote": int(self.is_remote),
                "tags": json.dumps(self.tags), "company": "EvalCo"}


EXAMPLES: list[EvalJob] = [
    # --- clear positives ------------------------------------------------------
    EvalJob("Senior AI Engineer",
            "Build agentic LLM systems with LangChain and RAG pipelines in Python.",
            True, "bullseye: role + heavy skills"),
    EvalJob("AI Engineer, Platform",
            "Python services with FastAPI; retrieval-augmented generation at scale.",
            True, "role + skills, no buzzword stuffing"),
    EvalJob("Full-Stack Engineer",
            "Next.js and React frontends over Python FastAPI APIs. Postgres, AWS.",
            True, "full-stack positive with generic infra tail"),
    EvalJob("Machine Learning Engineer, LLM Serving",
            "Deploy and optimize LLM inference. Python, Docker, CI/CD.",
            True, "ML title + lighter skill hits"),
    EvalJob("Software Engineer, GenAI Platform",
            "Agentic systems and tool-use orchestration; LangChain in production.",
            True, "platform framing, heavy differentiators"),
    EvalJob("Frontend Engineer",
            "React and Next.js product work on an AI developer-tools team.",
            True, "frontend positive via domain"),
    EvalJob("Founding Engineer (AI-native startup)",
            "Own the stack: Python, FastAPI, RAG search, agent workflows.",
            True, "founding-eng positive; title lacks target role"),
    EvalJob("Applied AI Engineer",
            "Ship LLM features; prompt pipelines; evaluation harnesses. Python.",
            True, "applied-AI variant"),
    EvalJob("Senior Software Engineer, Automation",
            "Python automation of internal workflows; some LLM integration.",
            True, "weaker positive — role hit, few skills"),
    EvalJob("AI Engineer",
            "Greenfield agentic AI product. TypeScript and Python.",
            True, "short JD positive"),
    EvalJob("AI Engineer",
            "Fully remote, work from anywhere. Build agentic LLM systems with LangChain "
            "and RAG in Python.",
            True, "geo positive: worldwide-remote must NOT trip a region lock",
            location="Remote - Worldwide"),
    EvalJob("AI Engineer",
            "Remote across EMEA. Build agentic LLM systems with LangChain and RAG in Python.",
            True, "geo positive: EMEA includes the candidate's continent (Africa)",
            location="Remote - EMEA"),
    # --- hard negatives: each one is a trap class that has actually bitten ------
    EvalJob("Warehouse Operations Associate",
            "Lift boxes. Forklift certification a plus.",
            False, "obvious irrelevant"),
    EvalJob("Category Manager",
            "Ongoing fragment cataloguing across categories. No engineering.",
            False, "substring trap: 'Go' in ongoing, 'RAG' in fragment"),
    EvalJob("Technical Deployment Lead, Semiconductors",
            "Fab deployment scheduling. Python scripting occasionally.",
            False, "substring trap: 'cto' inside Semiconductors"),
    EvalJob("Junior AI Engineer (Internship)",
            "Learn LangChain and RAG under supervision. Python.",
            False, "seniority trap: junior/intern for a mid-senior profile"),
    EvalJob("Head of AI Engineering",
            "Lead a 20-person org. Set agentic AI strategy.",
            False, "management trap for an IC profile"),
    EvalJob("Sales Development Representative, AI Products",
            "Sell our LLM platform. No coding. Quota-carrying.",
            False, "buzzword body, non-engineering role"),
    EvalJob("Marketing Manager",
            "Own campaigns for our AI agent product. LLM familiarity nice.",
            False, "requirement-mirroring boilerplate in body"),
    EvalJob("AI Engineer",
            "Python and LangChain. On-site only, five days a week.",
            False, "exclusion trap: on-site only", location="NYC office", is_remote=False),
    EvalJob("Machine Learning Engineer (Defense)",
            "US citizens only; clearance required. PyTorch.",
            False, "exclusion trap: citizenship/clearance"),
    EvalJob("AI Research Internship (unpaid)",
            "Unpaid research assistantship on RAG systems.",
            False, "exclusion trap: unpaid"),
    EvalJob("Senior Accountant",
            "Month-end close. Excel. Python scripts for reconciliation.",
            False, "single incidental skill hit"),
    EvalJob("DevOps Engineer",
            "Kubernetes, Docker, AWS, CI/CD. No product work.",
            False, "generic-infra trap: real skills, wrong role"),
    EvalJob("Senior Graphic Designer",
            "Brand and product design.", False,
            "tag-flooding trap (KNOWN MISS: marketplace boards tag their whole stack)",
            tags=["react", "python", "full-stack"]),
    EvalJob("Data Entry Clerk (Remote)",
            "Type fast. Remote position, flexible hours.",
            False, "remote-only bait with zero signal"),
    # --- geo-eligibility traps: strong skills, but the candidate cannot be hired ---
    EvalJob("Senior AI Engineer",
            "Build agentic LLM systems with LangChain and RAG in Python. Remote — but you "
            "must be authorized to work in the United States.",
            False, "geo trap: work-authorization lock (US) on an otherwise-perfect role"),
    EvalJob("AI Engineer, Platform",
            "Python, FastAPI, retrieval-augmented generation, agentic systems.",
            False, "geo trap: Remote (US) in the location field",
            location="Remote (US)"),
    EvalJob("Machine Learning Engineer",
            "LLM serving in Python and Docker. UK-based candidates only.",
            False, "geo trap: region lock to a region the candidate is not in (UK)"),
    EvalJob("Senior AI Engineer",
            "Build agentic LLM systems with LangChain and RAG in Python.",
            False, "geo trap: location field is a US hub city, no cue in the body",
            location="San Francisco"),
    EvalJob("AI Engineer, Platform",
            "Python, FastAPI, retrieval-augmented generation, agentic systems.",
            False, "geo trap: foreign country in the location field (Canada-remote)",
            location="Canada - Remote (ON, AB)"),
    EvalJob("Senior AI Engineer - Austin, TX",
            "Build agentic LLM systems with LangChain and RAG in Python.",
            False, "geo trap: location field global but the TITLE pins a US City, ST",
            location="Distributed"),
]


def evaluate() -> dict:
    """Score every example; return ranked rows + the metrics the floors assert."""
    rows = []
    for ex in EXAMPLES:
        score, rationale, gaps = heuristic_score(ex.as_row(), EVAL_PROFILE)
        rows.append({"title": ex.title, "relevant": ex.relevant, "score": score,
                     "note": ex.note, "rationale": rationale, "gaps": gaps})
    rows.sort(key=lambda r: -r["score"])

    n_pos = sum(1 for r in rows if r["relevant"])

    def precision_at(k: int) -> float:
        top = rows[:k]
        return sum(1 for r in top if r["relevant"]) / max(1, len(top))

    def recall_at(k: int) -> float:
        return sum(1 for r in rows[:k] if r["relevant"]) / max(1, n_pos)

    pos_scores = [r["score"] for r in rows if r["relevant"]]
    neg_scores = [r["score"] for r in rows if not r["relevant"]]
    return {
        "rows": rows,
        "n": len(rows),
        "n_pos": n_pos,
        "precision_at_5": precision_at(5),
        "precision_at_10": precision_at(10),
        "recall_at_10": recall_at(10),
        "mean_pos": sum(pos_scores) / len(pos_scores),
        "mean_neg": sum(neg_scores) / len(neg_scores),
        "separation": sum(pos_scores) / len(pos_scores) - sum(neg_scores) / len(neg_scores),
    }
