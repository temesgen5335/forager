"""Bound store growth by dropping stale postings.

Job-board postings are dead within weeks, but the store keeps every one forever —
locally that is already 200 MB+, and on GitHub Actions the cached store would blow
through the 10 GB repo cache limit in a couple of months.

Anything you acted on (an application or a tailored CV) is never pruned, whatever its
age. That is your own history, not scrape data.

Usage:
    python scripts/prune_store.py --older-than 60          # dry run: report only
    python scripts/prune_store.py --older-than 60 --apply
    python scripts/prune_store.py --older-than 60 --apply --vacuum
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from jobagent.config import get_settings  # noqa: E402
from jobagent.core.schemas import Event  # noqa: E402
from jobagent.store import Store  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--older-than", type=int, default=60,
                    help="drop postings not seen in this many days (default 60)")
    ap.add_argument("--apply", action="store_true",
                    help="actually delete; without this the script only reports")
    ap.add_argument("--vacuum", action="store_true",
                    help="reclaim file space afterwards (rewrites the whole db)")
    args = ap.parse_args()

    store = Store(get_settings().db_path)
    store.init_schema()
    try:
        before = store.count_jobs()
        if not args.apply:
            # Dry run: prune on a throwaway basis is not possible in-place, so report
            # the counts the same query would delete.
            cutoff_stale = store.conn.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE last_seen_at < "
                "  datetime('now', ?)"
                "  AND id NOT IN (SELECT job_id FROM applications)"
                "  AND id NOT IN (SELECT job_id FROM cv_variants)",
                (f"-{args.older_than} days",),
            ).fetchone()["n"]
            print(f"[prune] {before} jobs stored; {cutoff_stale} would be dropped "
                  f"(not seen in {args.older_than}d, never acted on)")
            print("[prune] dry run — pass --apply to delete")
            return

        out = store.prune_jobs(older_than_days=args.older_than, vacuum=args.vacuum)
        print(f"[prune] {before} → {store.count_jobs()} jobs "
              f"(-{out['jobs']}); matches -{out['matches']}, triage -{out['triage']}")
        if out["kept_acted_on"]:
            print(f"[prune] kept {out['kept_acted_on']} stale job(s) you applied to")
        # Retention is a real state change; the trail should show it.
        store.log_event(Event(kind="prune", payload={**out, "older_than_days": args.older_than}))
    finally:
        store.close()


if __name__ == "__main__":
    main()
