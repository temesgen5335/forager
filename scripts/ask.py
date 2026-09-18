"""Ask the assistant a question from the terminal.

The cheapest of the three interfaces and the one to iterate on: no server, no browser,
no bot token. It exercises the whole path — routing, degradation, the governed toolbox,
the trail — so a problem found here is found before it reaches a surface where
someone is waiting.

Confirmation is one mechanism with three renderers. This one prints the computed diff
and reads y/n; the dashboard renders a card; Telegram renders buttons. All three redeem
the *same* server-side nonce bound to `sha256(args)`, so none of them can approve an
action whose arguments changed after the operator looked at them.

Usage:
    python scripts/ask.py "is the pipeline healthy?"
    python scripts/ask.py --explain "why is telegram stale?"   # show the routing decision
    python scripts/ask.py --reindex "which postings mention rust?"
    python scripts/ask.py --read-only "..."                    # refuse every write
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agentkit.llm.chain import build_chain  # noqa: E402
from agentkit.llm.router import describe  # noqa: E402
from agentkit.llm.runner import Runner  # noqa: E402
from agentkit.llm.tasks import NoCapableModel  # noqa: E402
from agentkit.session import Surface  # noqa: E402
from jobagent.assistant import build_assistant  # noqa: E402
from jobagent.assistant.card import render_card  # noqa: E402
from jobagent.assistant.sink import StoreSink  # noqa: E402
from jobagent.config import get_settings  # noqa: E402
from jobagent.store import Store  # noqa: E402


def confirm_at_the_terminal(name: str, args: dict, policy) -> bool:
    """Print the shared card, then ask. The card is computed from stored rows and
    validated arguments — not from anything the model said about what it intends."""
    print(f"\n  ┌─ {name} needs your approval")
    card = render_card(name, args, policy, get_settings(), _STORE)
    for line in card.splitlines():
        print(f"  │  {line}")
    if card.startswith("REFUSED:"):
        print("  └─ not offering this.\n")
        return False
    try:
        answer = input("  └─ approve? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False        # a closed stdin is a no, never a yes
    return answer in ("y", "yes")


_STORE = None


def main() -> int:
    global _STORE

    ap = argparse.ArgumentParser()
    ap.add_argument("question", nargs="*", help="what to ask")
    ap.add_argument("--explain", action="store_true",
                    help="print the routing decision and why each backend was skipped")
    ap.add_argument("--reindex", action="store_true",
                    help="rebuild the posting search index before asking")
    ap.add_argument("--read-only", action="store_true",
                    help="no confirmation channel, so every write is refused")
    ap.add_argument("--provider", default="", help="force one provider")
    args = ap.parse_args()

    question = " ".join(args.question).strip()
    if not question and not args.reindex:
        ap.error("ask something, or pass --reindex")

    settings = get_settings()
    store = _STORE = Store(settings.db_path)
    store.init_schema()

    try:
        from jobagent.assistant.operator_tools import OperatorDeps  # noqa: E402
        deps = OperatorDeps(db_path=settings.db_path)
        assistant = build_assistant(
            store=store, settings=settings, sink=StoreSink(store),
            surface=Surface.CLI,
            ask=None if args.read_only else confirm_at_the_terminal,
            deps=deps,
        )

        if args.reindex:
            from jobagent.assistant.knowledge import reindex_postings
            count = reindex_postings(store, assistant.index)
            print(f"indexed {count} postings")
            if not question:
                return 0

        backends = build_chain(settings)
        if args.provider:
            backends = [b for b in backends if b.name == args.provider]
        if not backends:
            print("No usable LLM provider. Set an API key in .env "
                  "(GROQ_API_KEY is free).", file=sys.stderr)
            return 2

        task = assistant.task()
        if args.explain:
            print(describe(task, backends), "\n")

        runner = Runner(backends=backends, toolbox=assistant.toolbox)
        try:
            outcome = runner.run(task, system=assistant.system_prompt, prompt=question)
        except NoCapableModel as exc:
            # The rejection list says what to change, which is the whole reason it is
            # carried on the exception rather than logged and discarded.
            print(f"\n{exc}", file=sys.stderr)
            return 1

        print(f"\n{outcome.value}\n")
        marks = [f"{outcome.provider}/{outcome.model}", str(outcome.strategy),
                 f"{outcome.elapsed_ms}ms", f"run {assistant.run_id}"]
        print(f"— {' · '.join(marks)}")
        for warning in outcome.warnings:
            print(f"  ! {warning}")

        assistant.auditor.close(summary=question[:120])
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
