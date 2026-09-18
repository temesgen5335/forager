"""HTTP surface for the assistant.

The confirmation flow is the only part that is genuinely different from the CLI, and
the difference is forced: HTTP cannot block waiting for a human. So the model's turn
completes *without* the write — the tool is refused and the pending approval is
returned alongside the answer — and the operator confirms it as a separate request.

That split makes confirm-then-swap structurally impossible rather than merely detected.
The client sends **only a nonce**; the arguments live server-side and are never accepted
from the caller. There is no field in which to send different arguments than the ones
the card described, which is a stronger guarantee than the digest check the CLI relies
on (and the digest check still runs underneath).

Pending approvals are held in process memory, deliberately. They expire in minutes, a
restart legitimately voids them, and persisting an approval-in-waiting is exactly the
kind of state that outlives the intent behind it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from fastapi import HTTPException
from pydantic import BaseModel

from agentkit.llm.chain import build_chain
from agentkit.llm.runner import Runner
from agentkit.llm.tasks import NoCapableModel
from agentkit.llm.types import ToolCall
from agentkit.permissions import NONCE_TTL_S, args_digest
from agentkit.session import Surface
from jobagent.assistant import build_assistant
from jobagent.assistant.card import render_card
from jobagent.assistant.sink import StoreSink

MAX_QUESTION_CHARS = 2000


class AskReq(BaseModel):
    question: str
    # Rebuild the posting index first. Off by default: it is a full-table pass, and
    # most questions are about pipeline state rather than posting text.
    reindex: bool = False


@dataclass
class _Pending:
    """One approval waiting on the operator. Arguments stay here, never on the wire."""

    nonce: str
    tool: str
    args: dict
    digest: str
    card: str
    expires_at: float


@dataclass
class PendingRegistry:
    """Process-local approvals. Not a cache — losing one is correct behaviour."""

    now: object = time.monotonic
    _items: dict[str, _Pending] = field(default_factory=dict)

    def add(self, nonce: str, tool: str, args: dict, card: str) -> None:
        self._sweep()
        self._items[nonce] = _Pending(nonce, tool, args, args_digest(args), card,
                                      self.now() + NONCE_TTL_S)

    def take(self, nonce: str) -> _Pending | None:
        self._sweep()
        pending = self._items.pop(nonce, None)     # single-use: popped on sight
        if pending is None or pending.expires_at <= self.now():
            return None
        return pending

    def _sweep(self) -> None:
        for key in [k for k, v in self._items.items() if v.expires_at <= self.now()]:
            del self._items[key]


def register(app, *, store_factory, settings_factory, auth, read_auth=None, limit=None):
    """Mount the assistant routes. Both are auth-gated: `ask` spends LLM quota and
    `confirm` performs a privileged write, so neither may be reachable anonymously."""

    pending = PendingRegistry()

    @app.post("/assistant/ask", dependencies=auth + (limit or []))
    def ask(req: AskReq):
        question = (req.question or "").strip()
        if not question:
            raise HTTPException(422, "Ask something.")
        if len(question) > MAX_QUESTION_CHARS:
            raise HTTPException(422, f"Question too long (max {MAX_QUESTION_CHARS}).")

        settings = settings_factory()
        store = store_factory()
        try:
            captured: list[dict] = []

            def capture(name: str, args: dict, policy) -> bool:
                """Record the approval and refuse *this* turn.

                Returning False is not a rejection of the idea — it is what makes the
                answer arrive now instead of hanging on a person. The operator confirms
                separately, against arguments the client never held.
                """
                nonce = __import__("secrets").token_urlsafe(24)
                card = render_card(name, args, policy, settings, store)
                pending.add(nonce, name, args, card)
                captured.append({"nonce": nonce, "tool": name, "card": card})
                return False

            assistant = build_assistant(
                store=store, settings=settings, sink=StoreSink(store),
                surface=Surface.WEB, ask=capture)

            if req.reindex:
                from jobagent.assistant.knowledge import reindex_postings
                reindex_postings(store, assistant.index)

            backends = build_chain(settings)
            if not backends:
                raise HTTPException(503, "No LLM provider is configured.")

            try:
                outcome = Runner(backends=backends,
                                 toolbox=assistant.toolbox).run(
                    assistant.task(), system=assistant.system_prompt, prompt=question)
            except NoCapableModel as exc:
                raise HTTPException(503, str(exc)) from exc

            assistant.auditor.close(summary=question[:120])
            return {
                "answer": outcome.value,
                "provider": outcome.provider,
                "model": outcome.model,
                "strategy": str(outcome.strategy),
                "degraded": outcome.degraded,
                "warnings": list(outcome.warnings),
                "elapsed_ms": outcome.elapsed_ms,
                "run_id": assistant.run_id,
                "pending": captured,
            }
        finally:
            store.close()

    @app.post("/assistant/confirm/{nonce}", dependencies=auth + (limit or []))
    def confirm(nonce: str):
        """Approve one waiting action. The body is empty on purpose — there is nothing
        for the caller to supply, and therefore nothing to tamper with."""
        item = pending.take(nonce)
        if item is None:
            raise HTTPException(404, "Unknown, expired, or already-used confirmation.")
        if item.digest != args_digest(item.args):
            # Belt and braces: server-side storage means this cannot drift, so if it
            # ever does, something is wrong enough to refuse.
            raise HTTPException(409, "Stored arguments no longer match the approval.")

        settings = settings_factory()
        store = store_factory()
        try:
            assistant = build_assistant(
                store=store, settings=settings, sink=StoreSink(store),
                surface=Surface.WEB,
                # Approved by the operator in the request that reached here. The
                # gatekeeper still mints and redeems its own argument-bound nonce
                # underneath, so the binding is enforced twice.
                ask=lambda name, args, policy: name == item.tool
                and args_digest(args) == item.digest)
            result = assistant.toolbox.execute(
                ToolCall(f"confirm_{nonce[:8]}", item.tool, item.args))
            assistant.auditor.close(summary=f"confirmed {item.tool}")
            return {"tool": item.tool, "ok": not result.is_error,
                    "result": result.content, "run_id": assistant.run_id}
        finally:
            store.close()

    @app.get("/assistant/sessions", dependencies=(read_auth or []))
    def sessions(limit: int = 20):
        """Past assistant sessions, kept out of the pipeline trail."""
        store = store_factory()
        try:
            return {"sessions": store.list_runs(limit, kind_detail="agent_session")}
        finally:
            store.close()

    return pending
