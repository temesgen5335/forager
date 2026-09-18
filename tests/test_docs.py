"""Governance documentation must stay true.

Docs rot silently: nothing fails when `CLAUDE.md` describes a module that was
renamed, or cites a rule nobody wrote. These tests make the common drift loud.

They deliberately check *structural* claims — a rule exists, a path exists, a command
exists — not prose. Asserting on wording would make every edit a test failure and the
tests would be deleted within a month.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ENTRY_POINTS = ("CLAUDE.md", "AGENTS.md")


def _rules_text() -> str:
    return (ROOT / ".claude" / "rules.md").read_text()


def _defined_rules() -> set[str]:
    return set(re.findall(r"\*\*(R\d+[a-z]?) —", _rules_text()))


def test_every_cited_rule_actually_exists():
    """A doc pointing at R47 when there is no R47 is worse than no pointer: it reads
    as authority and cannot be looked up."""
    defined = _defined_rules()
    assert len(defined) > 20, "rules.md parse looks wrong — too few rules found"

    problems = []
    for name in ENTRY_POINTS + (".claude/agent.md", ".claude/context.md", "README.md"):
        path = ROOT / name
        if not path.exists():
            continue
        for cited in set(re.findall(r"\bR\d+[a-z]?\b", path.read_text())):
            if cited not in defined:
                problems.append(f"{name} cites undefined {cited}")
    assert problems == [], problems


def test_rule_numbers_are_not_reused():
    numbers = re.findall(r"\*\*(R\d+[a-z]?) —", _rules_text())
    duplicates = {n for n in numbers if numbers.count(n) > 1}
    assert duplicates == set(), f"rules.md defines these twice: {duplicates}"


@pytest.mark.parametrize("doc", ENTRY_POINTS)
def test_entry_points_describe_the_packages_that_exist(doc):
    """Both top-level packages must be findable from the first doc an agent reads.

    `agentkit` and `jobagent/assistant` are ~4k lines that once existed for a whole
    session without appearing in any entry point — someone onboarding would have been
    handed a map of a system that no longer matched.
    """
    text = (ROOT / doc).read_text()
    for package in ("agentkit", "assistant"):
        assert package in text, f"{doc} never mentions {package}/"


@pytest.mark.parametrize("doc", ENTRY_POINTS + (".claude/agent.md", "README.md"))
def test_no_doc_points_at_a_path_that_does_not_exist(doc):
    """Backtick-quoted repo paths must resolve. This is how a doc survives a rename:
    the rename breaks the test rather than the reader."""
    path = ROOT / doc
    if not path.exists():
        pytest.skip(f"{doc} not present")

    # Line-aware: a path introduced by a creation verb or an "e.g." is illustrative —
    # a tutorial step saying `Create src/.../myboard.py` is *supposed* to name a file
    # that does not exist yet. Only descriptive references are claims about reality.
    pattern = re.compile(r"`((?:src|tests|scripts|dashboard|docs|config|deploy|"
                         r"\.claude|\.github)/[\w./-]+)`")
    illustrative = re.compile(r"\b(create|add|copy|rename|e\.g\.|for example|new)\b", re.I)

    missing = []
    for line in path.read_text().splitlines():
        if illustrative.search(line):
            continue
        for candidate in pattern.findall(line):
            if "*" in candidate or "{" in candidate:
                continue
            if not (ROOT / candidate.rstrip("/")).exists():
                missing.append(f"{candidate}  (line: {line.strip()[:60]})")
    assert missing == [], f"{doc} points at paths that do not exist: {missing}"


@pytest.mark.parametrize("doc", ENTRY_POINTS)
def test_documented_make_targets_exist(doc):
    """A command in the onboarding doc that does not run is the first thing a new
    developer tries."""
    makefile = (ROOT / "Makefile").read_text()
    targets = set(re.findall(r"^([a-z_]+):", makefile, re.M))
    documented = set(re.findall(r"^make ([a-z_]+)", (ROOT / doc).read_text(), re.M))
    missing = sorted(documented - targets)
    assert missing == [], f"{doc} documents non-existent make targets: {missing}"


def test_make_forage_is_an_alias_for_pipeline():
    """`make forage` must run the same entrypoint as `make pipeline` — the dialect verb
    is real in the CLI, not just docs. Both targets stay so muscle memory keeps working."""
    mk = (ROOT / "Makefile").read_text()
    assert re.search(r"^forage:", mk, re.M), "no `forage` make target"
    assert re.search(r"^pipeline:", mk, re.M), "`pipeline` target must remain"
    # forage resolves to pipeline's recipe (prerequisite form) or runs the same script.
    forage_line = next(l for l in mk.splitlines() if l.startswith("forage:"))
    assert "pipeline" in forage_line or "scripts/pipeline.py" in mk


def test_the_test_file_count_claimed_in_context_is_accurate():
    """The file count is exact and cheap to check, so there is no excuse for drift.

    It was wrong when this test was written — context.md claimed 37 files against 35 on
    disk, a number nobody had recounted in a while.
    """
    text = (ROOT / ".claude" / "context.md").read_text()
    claimed = {int(n) for n in re.findall(r"across (\d+) files", text)}
    actual = len(list((ROOT / "tests").glob("test_*.py")))
    assert claimed == {actual}, f"context.md claims {claimed} test files, found {actual}"


def test_the_test_count_claimed_in_context_has_not_drifted_far():
    """An exact count would make every added test a doc change, which is how a doc
    check gets deleted. A 15% band still catches real staleness — the count sat at
    353 while the suite passed 500, which is the failure worth catching."""
    text = (ROOT / ".claude" / "context.md").read_text()
    claimed = [int(n) for n in re.findall(r"\*\*(\d{3,4})\*\* tests", text)]
    assert claimed, "context.md no longer states a test count"

    actual = 0
    for path in (ROOT / "tests").glob("test_*.py"):
        actual += len(re.findall(r"^def test_", path.read_text(), re.M))
    assert actual > 100, "test discovery looks wrong"

    drift = abs(claimed[0] - actual) / actual
    assert drift < 0.15, (f"context.md claims {claimed[0]} tests, found roughly "
                          f"{actual} — {drift:.0%} adrift")
