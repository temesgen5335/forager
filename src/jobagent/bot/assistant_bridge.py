"""The assistant, reachable from Telegram.

Deliberately almost all pure functions. `tests/test_bot.py` covers `service.py` but not
the handlers — they need live `Update`/`Context` objects — and that gap is how a call to
an undefined `_llm()` once shipped in the `/apply` path. So everything here that can be
tested without a Telegram runtime lives here, and the handler in `app.py` is thin enough
to read in one go.

**Config changes cannot be approved from chat.** A single tap on a phone, with no
re-auth and a screen too small to read a diff on, is a materially weaker signal than the
same click on an authenticated dashboard — and the action it would authorize rewrites
the pipeline's configuration. `Surface.CHAT` is outside `admin_surfaces`, so the refusal
is structural rather than a rule this module remembers to apply. Chat gets the computed
diff read-only and a pointer to the dashboard.

Ordinary actions (stash) *are* confirmable here, through the inline-button pattern the
bot already uses. The button carries only a nonce; the arguments stay in `bot_data`, so
there is nothing in the callback payload to tamper with — the same property the HTTP
surface gets for the same reason.
"""

from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass, field

from jobagent.assistant import ASSISTANT_NAME

MAX_TELEGRAM_CHARS = 3500      # 4096 hard limit; leave room for the footer
NONCE_TTL_S = 300.0


def address(text: str, name: str = ASSISTANT_NAME) -> str | None:
    """If a message opens by addressing the assistant by name, return the rest.

    So "Baer, is the pipeline healthy?" reaches the assistant just like `/ask` does —
    which is what makes the name real in chat and not only in the model's answer.

    Deliberately *leading* address only. A message that merely mentions the name in
    passing ("did Baer answer earlier?") is not a command, and hijacking it would make
    the bot feel like it is interrupting. Returns the remainder ("" when the message is
    just the bare name, so the caller can prompt for more), or None when not addressed.
    """
    if not text:
        return None
    # name, then an optional comma/colon/dash, then either end-of-string or whitespace.
    m = re.match(rf"\s*{re.escape(name)}\b[\s,:—-]*", text, re.IGNORECASE)
    return text[m.end():].strip() if m else None


@dataclass(frozen=True)
class Answer:
    """What one question produced. Rendered separately so the formatting is testable
    without a model or a bot."""

    text: str
    provider: str = ""
    model: str = ""
    strategy: str = ""
    warnings: tuple[str, ...] = ()
    run_id: str = ""
    pending: tuple[dict, ...] = ()
    error: str = ""


@dataclass
class PendingBox:
    """Approvals waiting on a button press. Lives in `bot_data`; never sent anywhere."""

    now: object = time.monotonic
    items: dict = field(default_factory=dict)

    def add(self, tool: str, args: dict, card: str) -> str:
        self._sweep()
        nonce = secrets.token_urlsafe(9)      # fits Telegram's 64-byte callback payload
        self.items[nonce] = {"tool": tool, "args": args, "card": card,
                             "expires_at": self.now() + NONCE_TTL_S}
        return nonce

    def take(self, nonce: str) -> dict | None:
        self._sweep()
        item = self.items.pop(nonce, None)     # single-use
        return item if item and item["expires_at"] > self.now() else None

    def _sweep(self) -> None:
        for key in [k for k, v in self.items.items() if v["expires_at"] <= self.now()]:
            del self.items[key]


def truncate(text: str, limit: int = MAX_TELEGRAM_CHARS) -> str:
    """Telegram rejects messages over 4096 characters. Cutting at a line boundary and
    saying so beats a 400 from the API or a sentence that stops mid-word."""
    text = text or ""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if "\n" in cut:
        cut = cut[:cut.rindex("\n")]
    return cut + "\n\n…(truncated)"


def format_answer(answer: Answer) -> str:
    """The message body. Plain text, not Markdown: answers quote job and channel text
    that this system did not write, and an unbalanced `*` or `_` in it would make
    Telegram reject the whole message."""
    if answer.error:
        return f"⚠️ {answer.error}"

    parts = [truncate(answer.text)]
    footer = [f"{answer.provider}/{answer.model}"] if answer.provider else []
    if answer.strategy:
        footer.append(answer.strategy)
    if footer:
        parts.append("— " + " · ".join(footer))
    # Degradation is stated, never hidden: an answer from a weaker model must not look
    # the same as an answer from the best one available.
    parts += [f"! {w}" for w in answer.warnings]
    return "\n\n".join(parts)


def format_pending(item: dict, *, dashboard_url: str = "") -> str:
    """The confirmation card. Built from the server-computed diff, never model prose."""
    lines = [f"⚠️ {item['tool']} needs your approval", "", item.get("card", "")]
    if dashboard_url:
        lines += ["", f"Or review it in the dashboard: {dashboard_url}"]
    return truncate("\n".join(lines))


def ask_blocking(*, db_path: str, settings, question: str,
                 pending_box: PendingBox, dashboard_url: str = "") -> Answer:
    """Run one question. Blocking — the caller runs it in a worker thread.

    Opens its own Store because SQLite is single-thread and this executes off the event
    loop, the same rule every other threaded helper in the bot follows.
    """
    from agentkit.llm.chain import build_chain
    from agentkit.llm.runner import Runner
    from agentkit.llm.tasks import NoCapableModel
    from agentkit.session import Surface
    from jobagent.assistant import build_assistant
    from jobagent.assistant.card import render_card
    from jobagent.assistant.sink import StoreSink
    from jobagent.store import Store

    question = (question or "").strip()
    if not question:
        return Answer(text="", error="Ask something, e.g. /ask is the pipeline healthy?")

    store = Store(db_path)
    store.init_schema()
    try:
        captured: list[dict] = []

        def capture(tool, args, policy):
            """Record the approval and refuse this turn — the answer should arrive now
            rather than hang on a person walking past their phone."""
            card = render_card(tool, args, policy, settings, store)
            nonce = pending_box.add(tool, args, card)
            captured.append({"nonce": nonce, "tool": tool, "card": card})
            return False

        assistant = build_assistant(store=store, settings=settings, sink=StoreSink(store),
                                    surface=Surface.CHAT, ask=capture)
        backends = build_chain(settings)
        if not backends:
            return Answer(text="", error="No LLM provider is configured. "
                                         "Set a key in the dashboard settings.")
        try:
            outcome = Runner(backends=backends, toolbox=assistant.toolbox).run(
                assistant.task(), system=assistant.system_prompt, prompt=question)
        except NoCapableModel as exc:
            return Answer(text="", error=f"No model could answer that.\n{exc}"[:600])

        assistant.auditor.close(summary=question[:120])
        return Answer(
            text=str(outcome.value), provider=outcome.provider, model=outcome.model,
            strategy=str(outcome.strategy), warnings=tuple(outcome.warnings),
            run_id=assistant.run_id, pending=tuple(captured),
        )
    finally:
        store.close()


def run_confirmed(*, db_path: str, settings, item: dict) -> str:
    """Execute one approved action. Returns the text to show."""
    from agentkit.llm.types import ToolCall
    from agentkit.session import Surface
    from jobagent.assistant import build_assistant
    from jobagent.assistant.sink import StoreSink
    from jobagent.store import Store

    store = Store(db_path)
    store.init_schema()
    try:
        assistant = build_assistant(
            store=store, settings=settings, sink=StoreSink(store), surface=Surface.CHAT,
            # The operator pressed the button. The gatekeeper still mints and redeems
            # its own argument-bound nonce underneath, and CHAT is still outside
            # admin_surfaces, so a config change is refused here even with a press.
            ask=lambda tool, args, policy: tool == item["tool"] and args == item["args"])
        result = assistant.toolbox.execute(
            ToolCall("tg_confirm", item["tool"], item["args"]))
        assistant.auditor.close(summary=f"confirmed {item['tool']}")
        return truncate(result.content)
    finally:
        store.close()
