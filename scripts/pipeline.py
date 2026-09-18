"""End-to-end forage: ingest → match → (optionally) push digest to Telegram.

This is the single command the systemd timer runs on a schedule. Each stage is
independent and logged, so a failure in one is visible without killing the others.

Usage:
    python scripts/pipeline.py            # ingest, match, send digest
    python scripts/pipeline.py --no-send  # ingest + match only (no Telegram)
    python scripts/pipeline.py --top 15
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from jobagent.bot.notify import send_message  # noqa: E402
from jobagent.bot.service import jobs_text  # noqa: E402
from jobagent.config import get_settings  # noqa: E402
from jobagent.digest import format_followups, health_banner  # noqa: E402
from jobagent.llm_client import build_llm  # noqa: E402
from jobagent.pipeline import forage, new_run_id  # noqa: E402
from jobagent.preferences import load_preferences  # noqa: E402
from jobagent.store import Store  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=10, help="jobs in the digest")
    parser.add_argument("--no-send", action="store_true", help="skip the Telegram push")
    args = parser.parse_args()

    settings = get_settings()
    profile = load_preferences().profile
    store = Store(settings.db_path)
    store.init_schema()

    run_id = new_run_id()
    print(f"[forage] {run_id}")

    def digest(store_, report) -> dict:
        """Stage 3, run between matching and the summary so its outcome lands on the
        same `run` row. Carries a health banner so a degraded forage announces itself."""
        health = store_.pipeline_health()
        banner = health_banner(report.ingest, health, gap_hours=report.gap_hours_before)
        followups = format_followups(store_.applications_needing_followup())
        if banner:
            print("[health] " + banner.strip().replace("\n", "\n[health] "))
        if args.no_send:
            print("[digest] skipped (--no-send)")
            return {"digest": "skipped (--no-send)"}
        if not (settings.telegram_bot_token and settings.telegram_destination):
            print("[digest] skipped (no TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)")
            return {"digest": "skipped (no bot creds)"}
        try:
            sent = send_message(settings.telegram_bot_token, settings.telegram_destination,
                                banner + jobs_text(store_, args.top) + followups)
            print(f"[digest] sent in {sent} message(s)")
            return {"digest": f"sent ({sent} message(s))"}
        except Exception as exc:  # noqa: BLE001 — report, don't fail the whole run
            print(f"[digest] send failed: {exc}")
            return {"digest": f"failed: {exc}"}

    try:
        llm = build_llm(settings)
        report = forage(store, settings, profile, llm=llm, run_id=run_id,
                        trigger="pipeline", after_match=digest)
        if report.skipped:
            print(f"[forage] {report.skipped} — exiting")
            return
        ing = report.ingest
        print(f"[ingest] {ing.total_new} new / {ing.total_fetched} fetched"
              + (f" / {ing.total_dropped} filtered {ing.drops_by_reason}"
                 if ing.total_dropped else ""))
        for r in ing.results:
            if r.error:
                print(f"[ingest]   {r.source}: ERROR {r.error}")
        mode = (f"heuristic+LLM ({' → '.join(llm.chain)})" if report.match.used_llm
                else "heuristic")
        print(f"[match] scored {report.match.scored} ({mode}); "
              f"LLM-reranked {report.match.llm_reranked}")
        print(f"[forage] {run_id} done in {report.duration_s}s")
    finally:
        store.close()


if __name__ == "__main__":
    main()
