"""Core domain schemas — the common contract every layer speaks.

Every ingestion adapter normalizes its source into `JobPosting`. Matching produces
`Match`. The apply pipeline produces `Application` + `CVVariant`. `Event` is the
append-only audit trail. Keep these stable; adapters and tools depend on them.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Source(str, Enum):
    """Where a posting came from. One enum value per ingestion adapter."""

    remoteok = "remoteok"
    remotive = "remotive"
    himalayas = "himalayas"    # remote-first board; carries an explicit location restriction
    greenhouse = "greenhouse"
    lever = "lever"
    ashby = "ashby"
    telegram = "telegram"
    aggregator = "aggregator"  # SerpApi/Apify → Indeed/LinkedIn/Glassdoor/JobRight
    scrape = "scrape"          # Playwright fallback


class ApplyMethod(str, Enum):
    email = "email"            # Tier 1: draft + send email
    ats_form = "ats_form"      # Tier 2: HITL browser form-fill (GH/Lever/Ashby)
    external_link = "external_link"  # hand off to user with a deep link
    unknown = "unknown"


class ApplicationStatus(str, Enum):
    """Forward-leaning lifecycle for a single application (mirrors HITL gating)."""

    matched = "matched"          # surfaced to user, no action yet
    drafting = "drafting"        # assets being generated
    awaiting_approval = "awaiting_approval"  # HITL gate — nothing sent yet
    submitted = "submitted"
    rejected = "rejected"
    interview = "interview"
    offer = "offer"
    skipped = "skipped"          # user declined
    failed = "failed"            # automation could not complete


# Which status moves make sense. An application is a real-world process, so the graph
# is mostly forward: you cannot un-submit something that was sent, and "offer" cannot
# revert to "matched". Enforcing this stops the tracker from accumulating states that
# never happened, which would quietly corrupt the funnel analytics.
#
# Deliberate corrections (a mis-click) are still possible via an explicit correction
# flag on the API — see api/app.py — which bypasses this map and logs an event. The
# map governs the normal path; it is not a cage.
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    ApplicationStatus.matched.value: {ApplicationStatus.drafting.value, ApplicationStatus.skipped.value},
    ApplicationStatus.drafting.value: {
        ApplicationStatus.awaiting_approval.value, ApplicationStatus.failed.value,
        ApplicationStatus.skipped.value,
    },
    ApplicationStatus.awaiting_approval.value: {
        ApplicationStatus.submitted.value, ApplicationStatus.skipped.value,
        ApplicationStatus.failed.value,
    },
    # Post-submission outcomes. `offer` direct from `submitted` happens (small companies).
    ApplicationStatus.submitted.value: {
        ApplicationStatus.interview.value, ApplicationStatus.rejected.value,
        ApplicationStatus.offer.value, ApplicationStatus.failed.value,
    },
    ApplicationStatus.interview.value: {
        ApplicationStatus.offer.value, ApplicationStatus.rejected.value,
    },
    # Covers both a declined and a rescinded offer; there is no separate "declined".
    ApplicationStatus.offer.value: {ApplicationStatus.rejected.value},
    ApplicationStatus.rejected.value: set(),                                  # terminal
    ApplicationStatus.skipped.value: {ApplicationStatus.matched.value},       # reconsider
    ApplicationStatus.failed.value: {                                         # retry
        ApplicationStatus.drafting.value, ApplicationStatus.skipped.value,
    },
}


def allowed_next(current: str) -> set[str]:
    """Statuses reachable from `current` on the normal path (excluding itself)."""
    return set(ALLOWED_TRANSITIONS.get(current, set()))


def can_transition(current: str, target: str) -> bool:
    """True if target is reachable from current. Same→same is allowed (idempotent PATCH)."""
    return target == current or target in ALLOWED_TRANSITIONS.get(current, set())


# Decorations boards add to an otherwise identical title. Stripped for CLUSTERING only —
# never for matching, where "Senior" and "Intern" are exactly the signal that matters.
_TITLE_NOISE = re.compile(
    r"\((?:[^()]*)\)|\[[^\]]*\]"                       # (Remote), [Contract]
    r"|\b(?:senior|sr\.?|junior|jr\.?|staff|principal|lead|mid|mid-level|entry[- ]level)\b"
    r"|\b(?:full[- ]?time|part[- ]?time|contract|permanent|freelance|intern(?:ship)?)\b"
    r"|\b(?:remote|hybrid|on[- ]?site|onsite|wfh)\b"
    r"|\b(?:m/f/d|m/w/d|f/m/x|all genders)\b",
    re.I)
# A trailing location after a dash or comma: "Backend Engineer — Berlin", "…, London".
_TRAILING_PLACE = re.compile(r"\s*[-–—,|]\s*[^-–—,|]{1,40}$")
_COMPANY_NOISE = re.compile(
    r"\b(?:inc|inc\.|llc|ltd|ltd\.|gmbh|bv|b\.v\.|sa|s\.a\.|ag|plc|co|corp|"
    r"corporation|company|technologies|technology|labs|group|holdings)\b", re.I)
_NON_WORD = re.compile(r"[^a-z0-9]+")


def normalize_title(title: str | None) -> str:
    """Title reduced to the role itself, for grouping across boards."""
    text = (title or "").lower()
    text = _TITLE_NOISE.sub(" ", text)
    # Applied AFTER noise removal, so "(Remote) — Berlin" does not leave "berlin" behind
    # as the only survivor. Guarded on length: a two-word title must not be halved.
    stripped = _TRAILING_PLACE.sub("", text)
    if len(stripped.split()) >= 2:
        text = stripped
    return " ".join(_NON_WORD.sub(" ", text).split())


def normalize_company(company: str | None) -> str:
    """Company reduced to its distinctive part: "Acme Inc." and "ACME" agree."""
    text = _COMPANY_NOISE.sub(" ", (company or "").lower())
    return " ".join(_NON_WORD.sub(" ", text).split())


class JobPosting(BaseModel):
    """Normalized job posting. The dedup_hash collapses the same role seen on
    multiple sources into one logical job."""

    model_config = ConfigDict(use_enum_values=True)

    id: str | None = None  # store-assigned (dedup_hash) once persisted
    source: Source
    source_job_id: str | None = None  # native id within the source, if any
    title: str
    company: str | None = None
    location: str | None = None
    is_remote: bool = False
    description: str = ""
    salary_text: str | None = None
    apply_method: ApplyMethod = ApplyMethod.unknown
    apply_url: str | None = None
    apply_email: str | None = None
    url: str | None = None  # canonical posting URL
    posted_at: datetime | None = None
    fetched_at: datetime = Field(default_factory=_utcnow)
    tags: list[str] = Field(default_factory=list)
    raw: dict = Field(default_factory=dict)  # full source payload — never discard

    def dedup_hash(self) -> str:
        """Stable identity across sources: normalized company+title+location.

        This is the PRIMARY KEY and must never change meaning: every stored job, every
        `applications.job_id`, and every stash row is keyed on it. Redefining it would
        re-id the whole store and orphan the operator's own history — a MAJOR by this
        project's versioning policy. Cross-board grouping goes in `cluster_key` instead.
        """
        basis = "|".join(
            (self.company or "").strip().lower().split()
            + (self.title or "").strip().lower().split()
            + (self.location or "").strip().lower().split()
        )
        return hashlib.sha256(basis.encode()).hexdigest()[:16]

    def cluster_key(self) -> str:
        """Groups the SAME role seen on several boards, without touching identity.

        The dedup hash is exact, so "Senior Backend Engineer" on Greenhouse and
        "Senior Backend Engineer (Remote)" on LinkedIn are two rows — which is correct
        for storage (each keeps its own apply URL) and wrong for a queue, where they are
        one decision. This key normalises away the decorations boards add:

        - parenthetical and bracketed suffixes: "(Remote)", "[Contract]"
        - a trailing location after a dash or comma: "— Berlin", ", London"
        - seniority and employment-type words, which vary per board for one role
        - punctuation and duplicate whitespace

        Location is deliberately EXCLUDED: the same role is posted with "Remote",
        "Remote (EMEA)" and "" on three boards, so including it would defeat the point.
        """
        return hashlib.sha256(
            f"{normalize_company(self.company)}|{normalize_title(self.title)}".encode()
        ).hexdigest()[:16]


class Match(BaseModel):
    """Heuristic/LLM assessment of one job against the user's profile."""

    model_config = ConfigDict(use_enum_values=True)

    job_id: str
    score: float = Field(ge=0.0, le=1.0)  # 0..1 fit
    rationale: str = ""                   # why it fits
    gaps: list[str] = Field(default_factory=list)  # missing requirements
    # Which scorer produced `score`. Heuristic scores used to overwrite LLM ones on
    # every run with nothing recording that a better number had existed — a known gap
    # from the July 2026 audit. `llm_score` keeps the expensive answer in its own field
    # so a cheap re-run cannot erase it (the store COALESCEs rather than overwrites).
    score_source: str = "heuristic"       # heuristic | llm
    llm_score: float | None = None
    created_at: datetime = Field(default_factory=_utcnow)


class CVVariant(BaseModel):
    """A CV tailored to a specific job. HARD RULE: reframes real experience only,
    never fabricates. `base_cv_id` tracks provenance back to the master CV."""

    model_config = ConfigDict(use_enum_values=True)

    id: str | None = None
    job_id: str
    base_cv_id: str
    content_markdown: str
    notes: str = ""  # what was emphasized/reordered and why
    created_at: datetime = Field(default_factory=_utcnow)


class Application(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    id: str | None = None
    job_id: str
    status: ApplicationStatus = ApplicationStatus.matched
    cv_variant_id: str | None = None
    cover_letter: str | None = None
    email_draft: str | None = None
    apply_method: ApplyMethod = ApplyMethod.unknown
    approved_at: datetime | None = None  # HITL gate stamp — set only on user approval
    submitted_at: datetime | None = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class Event(BaseModel):
    """Append-only audit line. Every state change and external action logs one."""

    model_config = ConfigDict(use_enum_values=True)

    id: int | None = None
    kind: str            # e.g. "ingest", "match", "approve", "submit", "error"
    job_id: str | None = None
    payload: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)
