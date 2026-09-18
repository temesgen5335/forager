"""One thread owns the governed session.

The SDK runs tool functions on AnyIO worker threads. `Store` is `check_same_thread=True`
(R15), and the Gatekeeper's pending nonces and the Auditor's counters are plain dicts.
Rather than lock each of them, the Operator runs every governed call on one dedicated
thread that created the Store — concurrency is impossible by construction, not by
discipline. `pull_jobs` is the one thing that leaves this thread, and it opens its own
Store (Task 9).

Session = process: one Gatekeeper (so `Confirm.SESSION` means "once per coding-agent
session"), one Auditor (one run id on the trail), one Store connection. `start()`
writes an opening note so a session killed before `close()` is still reconstructable
from `events_for_run`; `close()` writes the `run` row that `list_runs(kind_detail=
"agent_session")` lists.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from agentkit.llm.types import ToolCall, ToolResult, ToolSpec
from agentkit.permissions import Permission, ToolPolicy
from agentkit.session import Surface
from jobagent.assistant import build_assistant
from jobagent.assistant.card import render_card
from jobagent.assistant.operator_tools import OperatorDeps
from jobagent.assistant.sink import StoreSink
from jobagent.store import Store

# A coding-agent session spans hours and fit-checks and drafts are the point of it, but
# a runaway loop must still hit a wall. Baer's budget is 20; it is a counter, not a
# prompt (agentkit's rule for metered tools).
AGENT_COST_BUDGET = 60
DEFAULT_ADMIN_SURFACES = frozenset({Surface.WEB, Surface.CLI})
ADMIN_SURFACES_WITH_AGENT = DEFAULT_ADMIN_SURFACES | {Surface.AGENT}


class Operator:
    """The governed session behind one MCP server process."""

    def __init__(self, settings, *, admin: bool = False, deps: OperatorDeps | None = None,
                 base_url: str = "http://localhost:1234"):
        self.settings = settings
        self.admin = admin
        self.deps = deps or OperatorDeps(db_path=settings.db_path)
        self.base_url = base_url
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="operator")
        self._thread: threading.Thread | None = None
        self.store = None
        self.assistant = None
        self._client_noted = False
        self._closed = False

    # --- the one thread ------------------------------------------------------------

    def run(self, fn: Callable[[], Any]) -> Any:
        """Run `fn` on the owning thread and return its result. Re-entrant: a call made
        *from* the owning thread runs inline, so a tool that reads policy state cannot
        deadlock on its own executor."""
        if threading.current_thread() is self._thread:
            return fn()
        return self._pool.submit(fn).result()

    def start(self) -> None:
        def _build() -> None:
            self._thread = threading.current_thread()
            self.store = Store(self.settings.db_path)
            self.store.init_schema()
            self.assistant = build_assistant(
                store=self.store, settings=self.settings, sink=StoreSink(self.store),
                surface=Surface.AGENT, ask=None,
                admin_surfaces=ADMIN_SURFACES_WITH_AGENT if self.admin else DEFAULT_ADMIN_SURFACES,
                cost_budget=AGENT_COST_BUDGET, base_url=self.base_url, deps=self.deps,
            )
            box = self.assistant.toolbox
            if not self.admin:
                # Hidden, not merely refused: `allowed` narrows specs() too, and a tool
                # the model can see but never use teaches it to try (guard.py).
                book = box.gate.book
                box.allowed = frozenset(
                    name for name in box.inner.tools
                    if book.policy_for(name).permission is not Permission.ADMIN)
            self.assistant.auditor.note("agent_session_open", surface=str(Surface.AGENT),
                                        admin=self.admin, tools=len(box.specs()))
        self.run(_build)

    # --- what the bridge needs ------------------------------------------------------

    @property
    def run_id(self) -> str:
        return self.assistant.run_id

    def specs(self) -> tuple[ToolSpec, ...]:
        return self.run(lambda: tuple(self.assistant.toolbox.specs()))

    def policy_for(self, name: str) -> ToolPolicy | None:
        return self.run(lambda: self.assistant.toolbox.gate.book.policy_for(name))

    def is_granted(self, name: str) -> bool:
        return self.run(lambda: name in self.assistant.toolbox.gate.granted)

    def may_confirm_admin(self) -> bool:
        return self.run(lambda: self.assistant.context.may_confirm_admin())

    def card(self, name: str, args: dict) -> str:
        return self.run(lambda: render_card(name, args, self.policy_for(name),
                                            self.settings, self.store))

    def note_client(self, name: str, version: str, protocol: str) -> None:
        """Who is on the other end, once per session. An audit line, never a
        `SessionContext` field (R28)."""
        if self._client_noted:
            return
        self._client_noted = True
        self.run(lambda: self.assistant.auditor.note(
            "agent_client", client=name, version=version, protocol=protocol))

    def execute(self, name: str, args: dict, *, ask) -> ToolResult:
        """One governed call. `ask` is this call's confirmation channel (or None for
        "no channel"); it is set for the duration of the call only."""
        def _go() -> ToolResult:
            box = self.assistant.toolbox
            box.ask = ask
            try:
                return box.execute(ToolCall(f"mcp_{name}", name, dict(args or {})))
            finally:
                box.ask = None
        return self.run(_go)

    def close(self, summary: str = "agent session ended") -> None:
        if self._closed or self.assistant is None:
            return
        self._closed = True

        def _close() -> None:
            box = self.assistant.toolbox
            try:
                self.assistant.auditor.close(summary=summary, surface=str(Surface.AGENT),
                                             admin=self.admin, refusals=box.refusals)
            finally:
                self.store.close()
        try:
            self.run(_close)
        finally:
            self._pool.shutdown(wait=True)
