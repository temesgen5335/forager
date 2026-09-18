"""Transparent, dependency-free heuristic hunt.

Runs over every stored job with no API calls — gives an immediate ranked shortlist
and acts as the cheap prefilter before (optional) LLM reranking. The hunt is
explainable: the rationale lists exactly which signals fired.

The hunt is *preference-weighted* rather than a flat keyword count, because a flat
count is what produces false positives at the top of the list:

  - Role signal distinguishes a target-role match in the TITLE from an incidental
    keyword appearing somewhere in the body. Previously any single role term in the
    title earned the full role bonus, so "Sales Engineer" scored like "AI Engineer".
  - Skills are weighted (`profile.skill_weights`), so matching four differentiating
    skills outranks matching four generic infra tools. Coverage saturates rather than
    normalizing over the whole skill list, which would punish every posting for not
    mentioning all of them.
  - Seniority mismatch is penalized: a "Junior" or "Head of Engineering" posting is a
    false positive for a mid-to-senior IC however well its keywords match.
  - Every must-have is checked, not just "remote".
"""

from __future__ import annotations

import json
import re

from jobagent.preferences import Profile

# Component ceilings. They sum to 0.92, leaving 0.08 for the remote bonus so a
# fully-matching remote posting can reach 1.0.
_W_ROLE = 0.35
_W_SKILL = 0.30
_W_KEYWORD = 0.15
_W_DOMAIN = 0.12
_W_REMOTE_BONUS = 0.08

# Skill weight at which coverage counts as complete. Six default-weight skills
# saturate; with weighting, three heavy ones can. Postings mention a handful of
# technologies, so normalizing over the entire profile list would flatten everything.
_SKILL_SATURATION = 6.0
_KEYWORD_SATURATION = 6
_DOMAIN_SATURATION = 3

_PENALTY_NOT_REMOTE = 0.30
_PENALTY_MUST_HAVE = 0.10
_PENALTY_SENIORITY = 0.20
_EXCLUDED_CEILING = 0.15

# Titles that mismatch a mid/senior individual-contributor profile. Kept deliberately
# narrow: a wrong penalty buries a good job, so only unambiguous signals belong here.
_TOO_JUNIOR = (
    "intern", "internship", "junior", "graduate", "entry level", "entry-level",
    "apprentice", "trainee", "working student",
)
_MANAGEMENT_TRACK = ("director", "vice president", "head of", "chief", "cto", "vp of")
# NOTE: both tuples are matched with _hits (word boundaries), never `in`. Substring
# matching flagged "SemiconduCTOrs" and "ConneCTOrs" as CTO roles — the same class of
# bug that made "Go" match "going" before _hits existed.
_REMOTE_WORDS = ("remote", "worldwide", "anywhere", "distributed")


def _hits(terms: list[str], hay: str) -> list[str]:
    """Word-boundary match (alnum-aware) so 'Go' doesn't match 'going' and 'RAG'
    doesn't match 'fragment'. Handles multi-word terms and dots (Next.js)."""
    out = []
    for t in terms:
        pat = r"(?<![a-z0-9])" + re.escape(t.lower()) + r"(?![a-z0-9])"
        if re.search(pat, hay):
            out.append(t)
    return out


def _weight_of(skill: str, weights: dict[str, float]) -> float:
    """Skill weight, case-insensitive, default 1.0.

    Non-numeric weights are rejected by Profile validation at load time, so a config
    typo fails loudly rather than silently skewing every score. Negatives are clamped
    to 0 here: they pass validation but would turn a match into a penalty.
    """
    if not weights:
        return 1.0
    lowered = {k.lower(): v for k, v in weights.items()}
    return max(0.0, lowered.get(skill.lower(), 1.0))


def _role_signal(title: str, tags_text: str, text: str, profile: Profile) -> tuple[float, str]:
    """How strongly this posting *is* one of the target roles.

    A target role in the title is the strongest signal available; a loose keyword in
    the title is weaker; a target role buried in the body is weakest.
    """
    title_zone = f"{title} {tags_text}"
    if _hits(profile.target_roles, title_zone):
        return 1.0, "target role in title"
    if _hits(profile.keywords, title_zone):
        return 0.6, "keyword in title"
    if _hits(profile.target_roles, text):
        return 0.3, "target role in body"
    return 0.0, ""


def _seniority_gap(title: str, profile: Profile) -> str | None:
    """Return a gap description if the posting's level clearly mismatches the profile."""
    seniority = (profile.seniority or "").lower()
    wants_ic = not any(w in seniority for w in ("manager", "director", "lead", "head"))
    entry_level_profile = any(w in seniority for w in ("junior", "entry", "graduate", "intern"))

    if not entry_level_profile:
        junior = _hits(list(_TOO_JUNIOR), title)
        if junior:
            return f"level mismatch: '{junior[0]}' role"
    if wants_ic:
        mgmt = _hits(list(_MANAGEMENT_TRACK), title)
        if mgmt:
            return f"management-track role ('{mgmt[0]}')"
    return None


# --- geographic eligibility (fully configurable — no policy is hardcoded) ------
# Whether a posting's work location is reachable is a *preference*, so it is driven
# entirely by the profile, never baked in here (R22). The switch is `profile.remote_scope`:
#
#   "any"    — off (default): geo hunting does nothing, except honour geo_blocked below.
#   "global" — keep only genuinely global-remote postings; ANY posting that pins itself to
#              a specific place (a country, region, state or city — INCLUDING the candidate's
#              own) is region-locked and demoted, capped like an exclusion.
#
# Three optional lists shape it:
#   geo_global_terms — what counts as "globally open" (falls back to DEFAULT_GLOBAL_TERMS).
#   geo_eligible     — extra location patterns to always allow (e.g. "latam", "emea").
#   geo_blocked      — location/description patterns to always demote (honoured in any scope).
#
# The place test is gazetteer-free: strip the remote/global vocabulary and connectors from
# the location; if any word survives, the location names a place. So "Remote" and "Remote -
# Worldwide" pass, while "Remote - US", "San Francisco", "China - Remote", "Remote - EMEA"
# and "Remote - CA" do not — with no country list to keep current.

# Body phrasings that require work rights in a specific region ("US-based", "authorized to
# work in the UK"). Under "global" scope ANY of these disqualifies — the candidate is
# global-remote, in none of them. The region set need only be wide enough for the common
# cases; the location test below is the main workhorse.
_REGIONS: dict[str, tuple[str, ...]] = {
    "US": ("united states", "u.s.a.", "u.s.", "usa", "us"),
    "UK": ("united kingdom", "u.k.", "great britain", "uk"),
    "EU": ("european union", "eea"),
    "Canada": ("canada",),
    "India": ("india",),
    "Australia": ("australia",),
}
_LOCK_TEMPLATES: tuple[str, ...] = (
    r"authori[sz]ed to work in (?:the )?{r}",
    r"work authori[sz]ation in (?:the )?{r}",
    r"{r} work authori[sz]ation",
    r"must be (?:based|located|physically located) in (?:the )?{r}",
    r"must be (?:an? )?{r}[- ](?:based|resident)",
    r"must reside in (?:the )?{r}",
    r"must live in (?:the )?{r}",
    r"eligible to work in (?:the )?{r}",
    r"(?:residents?|citizens?) of (?:the )?{r}",
    # "{r}-based" only counts as a *requirement*, not company boilerplate ("US-based
    # company" must NOT match), so it must be followed by an applicant word or "only".
    r"{r}[- ]based (?:candidates?|applicants?|employees?|talent|residents?|only)",
    r"{r} (?:citizens?|nationals?|residents?) only",
)


def _region_frag(aliases: tuple[str, ...]) -> str:
    """A word-boundaried alternation of a region's surface forms, longest alias first, so a
    short code ('us') never matches inside another word ('Belarus')."""
    ordered = sorted(aliases, key=len, reverse=True)
    return r"(?<![a-z0-9])(?:" + "|".join(re.escape(a) for a in ordered) + r")(?![a-z0-9])"


_LOCK_PATTERNS = [
    re.compile("|".join("(?:" + tpl.replace("{r}", _region_frag(al)) + ")"
                        for tpl in _LOCK_TEMPLATES))
    for al in _REGIONS.values()
]

# What "globally open" means when the profile does not override it via geo_global_terms.
DEFAULT_GLOBAL_TERMS: tuple[str, ...] = (
    "worldwide", "world wide", "anywhere", "global", "globally", "international",
    "work from anywhere", "location independent", "location agnostic", "any location",
    "all locations", "no location", "fully distributed",
)
# Remote vocabulary that, on its own, does not pin a place.
_REMOTE_WORDS_STRIP = ("remote", "remote-first", "remote first", "fully remote", "remote only",
                       "remote position", "remote role", "distributed")
# Filler words dropped before deciding whether a place name survives.
_LOC_CONNECTORS = ("in", "the", "only", "based", "from", "work", "position", "role", "open",
                   "to", "and", "or", "team", "first", "home", "hq", "office", "flexible",
                   "friendly", "eligible", "candidates", "preferred", "timezone", "time",
                   "zone", "zones", "hours", "within", "across")


def _match_any(terms, hay: str) -> str | None:
    """The first configured term that word-boundary matches, or None."""
    for t in terms or ():
        if t and re.search(r"(?<![a-z0-9])" + re.escape(t.lower()) + r"(?![a-z0-9])", hay):
            return t
    return None


def _names_a_place(location: str, global_terms) -> bool:
    """True if the location names a specific place rather than only being 'remote/global'.
    Gazetteer-free: strip the remote/global/connector vocabulary; any letters left are a
    place name. So 'Remote' and 'Remote - Worldwide' are not places, but 'Remote - US',
    'San Francisco' and 'China - Remote' are."""
    s = (location or "").lower()
    strip = set(_REMOTE_WORDS_STRIP) | {t.lower() for t in global_terms} | set(_LOC_CONNECTORS)
    for w in sorted(strip, key=len, reverse=True):
        s = re.sub(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", " ", s)
    return bool(re.sub(r"[^a-z]+", "", s))       # any letters left → a place is named


# Some boards keep the location field global ("Distributed") but pin the role in the
# TITLE — "Senior Customer Engineer - Charlotte, NC". A "City, ST" with a real US state
# abbreviation is a high-precision signal (case-sensitive, so a role abbrev like "ML"/"AI"
# after a comma does not trip it). This is a detection primitive, not a preference.
_US_STATE_ABBR = frozenset((
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV "
    "NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC"
).split())
_CITY_STATE_RE = re.compile(r"\b[A-Z][A-Za-z.\-]+,\s*([A-Z]{2})\b")


def _title_names_us_location(title: str) -> bool:
    return any(m.group(1) in _US_STATE_ABBR for m in _CITY_STATE_RE.finditer(title or ""))


def _geo_verdict(location: str, title: str, text: str, profile: Profile) -> str | None:
    """A gap string if the posting is out of the profile's geographic scope, else None.
    Entirely config-driven: with the defaults (remote_scope='any', empty lists) it always
    returns None, so a profile that has not opted in is unaffected."""
    scope = (getattr(profile, "remote_scope", "") or "any").strip().lower()
    hit = _match_any(getattr(profile, "geo_blocked", None), f"{location} {text}".lower())
    if hit:
        return f"excluded location: {hit}"
    if scope != "global":
        return None
    global_terms = getattr(profile, "geo_global_terms", None) or DEFAULT_GLOBAL_TERMS
    loc = (location or "").lower()
    # _names_a_place already strips the global vocabulary, so a purely-global location
    # ("Remote - Worldwide") survives as "no place". geo_eligible is the one thing it does
    # not know about, so an explicit allow-list entry is the only override. This ordering
    # is why "Ukraine Anywhere" locks (a place survives) while "Anywhere" does not.
    if not _match_any(getattr(profile, "geo_eligible", None), loc) and _names_a_place(loc, global_terms):
        return "region-locked (not global-remote)"
    if _title_names_us_location(title):
        return "region-locked (US location in title)"
    if any(pat.search(text) for pat in _LOCK_PATTERNS):
        return "region-locked (in description)"
    return None


def heuristic_score(job: dict, profile: Profile) -> tuple[float, str, list[str]]:
    """Return (score 0..1, rationale, gaps) for one job row (dict from the store)."""
    title = (job.get("title") or "").lower()
    desc = (job.get("description") or "").lower()
    try:
        tags = json.loads(job.get("tags") or "[]")
    except (json.JSONDecodeError, TypeError):
        tags = []
    tags_text = " ".join(str(t).lower() for t in tags)
    text = " ".join([title, desc, tags_text])

    gaps: list[str] = []

    # --- positive signals ---------------------------------------------------------
    role_strength, role_why = _role_signal(title, tags_text, text, profile)

    # A level-mismatched title is not the target role: "Junior AI Engineer" matching
    # "AI Engineer" is a false role signal, not a true one with a defect. A flat
    # penalty alone let a junior/intern posting with perfect skill hits outrank
    # genuine mid-senior positives (caught by the eval harness) — so the mismatch
    # dampens the role component itself, and the flat penalty below still applies.
    seniority_gap = _seniority_gap(title, profile)
    if seniority_gap is not None and role_strength > 0:
        role_strength *= 0.3

    skill_hits = _hits(profile.core_skills, text)
    matched_weight = sum(_weight_of(s, profile.skill_weights) for s in skill_hits)
    skill_cover = min(1.0, matched_weight / _SKILL_SATURATION)

    kw_hits = _hits(profile.keywords, text)
    domain_hits = _hits(profile.domains, text)

    score = (
        _W_ROLE * role_strength
        + _W_SKILL * skill_cover
        + _W_KEYWORD * (min(len(kw_hits), _KEYWORD_SATURATION) / _KEYWORD_SATURATION)
        + _W_DOMAIN * (min(len(domain_hits), _DOMAIN_SATURATION) / _DOMAIN_SATURATION)
    )

    # --- must-haves ---------------------------------------------------------------
    # Trust the structured flag and the location/title for remote — NOT the full
    # description, which is full of "remote-friendly culture" boilerplate.
    loc_title = (job.get("location") or "").lower() + " " + title
    remote_ok = bool(job.get("is_remote")) or any(w in loc_title for w in _REMOTE_WORDS)

    for must in profile.must_haves:
        if must.lower() == "remote":
            if remote_ok:
                score += _W_REMOTE_BONUS
            else:
                score -= _PENALTY_NOT_REMOTE
                gaps.append("not clearly remote")
        elif not _hits([must], text):
            # Other must-haves are prose ("async-friendly", "AI-native"); absence is
            # weak evidence rather than proof, so penalize lightly but name the gap.
            score -= _PENALTY_MUST_HAVE
            gaps.append(f"must-have not found: {must}")

    # --- level mismatch (gap computed above, where it dampens the role signal) -----
    if seniority_gap is not None:
        score -= _PENALTY_SENIORITY
        gaps.append(seniority_gap)

    # --- exclusions ---------------------------------------------------------------
    exclude_hits = _hits(profile.exclude_keywords, text)
    if exclude_hits:
        # Hard down-rank rather than dropping: the user still sees the posting and why.
        score = min(score, _EXCLUDED_CEILING)
        gaps.append("excluded: " + ", ".join(exclude_hits))

    # --- geographic eligibility (config-driven; off unless the profile opts in) --------
    # Original-case title so the "City, ST" check can require uppercase state abbreviations.
    geo_gap = _geo_verdict(job.get("location") or "", job.get("title") or "", text, profile)
    if geo_gap:
        # Out of the profile's geographic scope — cap like an exclusion (visible, demoted).
        score = min(score, _EXCLUDED_CEILING)
        gaps.append(geo_gap)

    score = max(0.0, min(1.0, score))

    # --- rationale ----------------------------------------------------------------
    parts = []
    if role_why:
        parts.append(role_why)
    if skill_hits:
        # Heaviest-weighted matches first — those are the ones worth reading.
        ranked = sorted(set(skill_hits), key=lambda s: -_weight_of(s, profile.skill_weights))
        parts.append("skills: " + ", ".join(ranked[:6]))
    if domain_hits:
        parts.append("domains: " + ", ".join(sorted(set(domain_hits))))
    parts.append("region-locked" if geo_gap else ("remote" if remote_ok else "location unclear"))
    rationale = "; ".join(parts)

    return round(score, 3), rationale, gaps
