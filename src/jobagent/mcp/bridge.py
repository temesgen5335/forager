"""Registration → MCP tool.

Three translations, each mechanical and each tested:

- **schema → signature.** A `ToolSpec.parameters` object is already portable JSON Schema
  (flat, primitives, enums, described). `parameters_from_schema` turns it into typed
  keyword-only parameters so the SDK derives an identical `inputSchema`; enums become
  `Literal[...]` so the constraint survives.
- **policy → annotations.** READ → read-only + idempotent; `Confirm.ALWAYS` → destructive
  (one-way in process terms); only `pull_jobs` is open-world. Hints, not authority.
- **confirmation → resolver.** For ACT/ADMIN tools the SDK fills an extra `approval`
  parameter by running a resolver first. The resolver *reads* the policy book (does this
  call need a person? is there a form-elicitation channel?) and, if so, returns
  `Elicit(card, Approve)`; the SDK pushes `elicitation/create` on a legacy connection or
  returns `input_required` on a 2026-07-28 one and resumes on the retry. The body then
  runs the governed `execute()` with `ask = "the person said yes"`. The Gatekeeper still
  mints and redeems its own argument-bound nonce underneath — R29 holds without trusting
  the client or the SDK's sealed `requestState`. The resolver never decides; the
  Gatekeeper decides inside `execute()`, after the intent is audited (R27).
"""

from __future__ import annotations

import inspect
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import AcceptedElicitation, Context, Elicit, ElicitationResult, Resolve
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, Field

from agentkit.audit import AuditUnavailable
from agentkit.llm.types import ToolResult, ToolSpec
from agentkit.permissions import Confirm, Permission, ToolPolicy
from jobagent.mcp.operator import Operator

# The only tool that reaches outside this machine.
OPEN_WORLD: frozenset[str] = frozenset({"pull_jobs"})
_PRIMITIVES: dict[str, type] = {"string": str, "integer": int, "number": float, "boolean": bool}


class Approve(BaseModel):
    """The whole elicitation form: one yes/no. The card is the message; nothing the
    model wrote reaches it (R29)."""

    ok: bool = Field(description="Approve this action exactly as described?")


def parameters_from_schema(schema: dict) -> list[inspect.Parameter]:
    required = set(schema.get("required", []))
    params: list[inspect.Parameter] = []
    for pname, prop in schema.get("properties", {}).items():
        if "enum" in prop:
            base: Any = Literal[tuple(prop["enum"])]
        elif prop.get("type") == "array":
            base = list[_PRIMITIVES.get((prop.get("items") or {}).get("type"), str)]
        else:
            base = _PRIMITIVES.get(prop.get("type"), str)
        desc = prop.get("description", "")
        if pname in required:
            annotation, default = Annotated[base, Field(description=desc)], inspect.Parameter.empty
        else:
            annotation, default = Annotated[base | None, Field(description=desc)], None
        params.append(inspect.Parameter(pname, inspect.Parameter.KEYWORD_ONLY,
                                        default=default, annotation=annotation))
    return params


def _sign(fn, params: list[inspect.Parameter], returns, name: str, doc: str):
    """Give a generated function the signature and annotations the SDK introspects."""
    fn.__signature__ = inspect.Signature(params, return_annotation=returns)
    fn.__annotations__ = {p.name: p.annotation for p in params} | {"return": returns}
    fn.__name__ = fn.__qualname__ = name
    fn.__doc__ = doc
    return fn


def _clean(kw: dict) -> dict:
    """Optional parameters the client omitted arrive as None; the governed tools use
    `args.get()` semantics, so drop them rather than pass a literal None."""
    return {k: v for k, v in kw.items() if v is not None}


def _ctx_param() -> inspect.Parameter:
    return inspect.Parameter("ctx", inspect.Parameter.KEYWORD_ONLY, annotation=Context)


def has_form_elicitation(ctx: Context) -> bool:
    """Same test the SDK applies before sending an Elicit: a bare `elicitation: {}`
    counts as form support; url-only does not."""
    caps = ctx.client_capabilities
    el = caps.elicitation if caps is not None else None
    return el is not None and (el.form is not None or el.url is None)


def title_for(name: str) -> str:
    return name.replace("_", " ").capitalize()


def annotations_for(policy: ToolPolicy, name: str) -> ToolAnnotations:
    read = policy.permission is Permission.READ
    return ToolAnnotations(title=title_for(name), read_only_hint=read, idempotent_hint=read,
                           destructive_hint=policy.confirm is Confirm.ALWAYS,
                           open_world_hint=name in OPEN_WORLD)


def make_confirm_resolver(op: Operator, name: str, schema: dict):
    """The renderer half of a confirmation: decide whether a person must be asked and,
    if so, what they see. Reads policy state; never records a decision."""
    params = [_ctx_param()] + parameters_from_schema(schema)

    def resolver(**kw):
        ctx: Context = kw.pop("ctx")
        args = _clean(kw)
        policy = op.policy_for(name)
        if policy is None or policy.confirm is Confirm.NEVER:
            return Approve(ok=True)
        if policy.confirm is Confirm.SESSION and op.is_granted(name):
            return Approve(ok=True)                     # approved earlier this session
        if policy.permission is Permission.ADMIN and not op.may_confirm_admin():
            return Approve(ok=False)                    # the gate will say why
        if not has_form_elicitation(ctx):
            return Approve(ok=False)                    # the body passes ask=None
        return Elicit(op.card(name, args), Approve)

    return _sign(resolver, params, Approve | Elicit[Approve], f"confirm_{name}",
                 f"Ask the operator before {name} runs.")


def to_call_result(result: ToolResult, *, base_url: str) -> CallToolResult:
    text = result.content
    if result.is_error and "no confirmation channel" in text:
        text += f" A person can do this in the dashboard: {base_url}"
    return CallToolResult(content=[TextContent(type="text", text=text)],
                          structured_content=result.data, is_error=result.is_error)


def _note_client(op: Operator, ctx: Context) -> None:
    try:
        params = ctx.session.client_params
        info = params.client_info if params is not None else None
        op.note_client(getattr(info, "name", "") or "unknown", getattr(info, "version", "") or "",
                       str(ctx.protocol_version or ""))
    except Exception:  # noqa: BLE001 — identity is a courtesy line, never a reason to fail a call
        op.note_client("unknown", "", "")


def make_tool_fn(op: Operator, spec: ToolSpec, policy: ToolPolicy):
    """The MCP-facing function for one governed tool."""
    params = parameters_from_schema(spec.parameters)
    needs_approval = policy.confirm is not Confirm.NEVER
    extra = [_ctx_param()]
    if needs_approval:
        resolver = make_confirm_resolver(op, spec.name, spec.parameters)
        extra.append(inspect.Parameter(
            "approval", inspect.Parameter.KEYWORD_ONLY,
            annotation=Annotated[ElicitationResult[Approve], Resolve(resolver)]))

    def run(**kw) -> CallToolResult:
        ctx: Context = kw.pop("ctx")
        approval = kw.pop("approval", None)
        args = _clean(kw)
        _note_client(op, ctx)
        if not needs_approval or not has_form_elicitation(ctx):
            ask = None          # READ never asks; without a channel the gate refuses (R29)
        else:
            def ask(*_):
                return isinstance(approval, AcceptedElicitation) and bool(approval.data.ok)
        try:
            result = op.execute(spec.name, args, ask=ask)
        except AuditUnavailable as exc:
            return CallToolResult(content=[TextContent(
                type="text", text=f"Refused: the trail is unavailable ({exc}); "
                                  f"nothing runs without a record.")], is_error=True)
        return to_call_result(result, base_url=op.base_url)

    return _sign(run, params + extra, CallToolResult, spec.name, spec.description)
