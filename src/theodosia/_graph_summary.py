"""User-facing graph descriptions: action surface text + graph summary dict.

Three builders that turn an ``Application``'s topology into things the
client sees:

* ``_render_action_surface`` - compact text appended to the server's
  ``instructions`` so a connecting MCP client sees the action namespace
  at connect time, before reading any resources.
* ``_compute_graph_summary`` - full structured description returned by
  ``theodosia://graph``. Cold-start discovery surface: one resource read
  and the model has the whole topology.
* ``_normalize_external_tools`` / ``_next_external_tools`` - sanitize
  and filter the per-action external-tools advisory map.

This module is pure introspection: takes an Application, returns data.
No closure coupling to ``mount()``.
"""

from __future__ import annotations

import warnings
from typing import Any

from burr.core import Application

from theodosia._introspect import _action_inputs, _input_schemas


def _first_doc_line(action: Any) -> str:
    """First line of an action fn's docstring, or empty string."""
    fn = getattr(action, "fn", None)
    doc = (fn.__doc__ or "").strip() if fn is not None and fn.__doc__ else ""
    return doc.splitlines()[0] if doc else ""


def _condition_expr(transition: Any) -> str | None:
    """The printed condition expression for an edge, or None when unconditional.

    Burr's Condition.expr produces a condition whose ``name`` is the printed
    expression, which is exactly what a model needs to know when to take the
    edge. ``default`` means unconditional.
    """
    try:
        cond = transition.condition
        cond_name = getattr(cond, "_name", None) or getattr(cond, "name", None)
        if cond_name and cond_name != "default":
            return str(cond_name)
    except Exception:
        return None
    return None


def _action_meta(action: Any, ext_map: dict[str, list[str]]) -> dict[str, Any]:
    """Per-action metadata block for ``theodosia://graph``."""
    required, optional = _action_inputs(action)
    fn = getattr(action, "fn", None)
    doc = (fn.__doc__ or "").strip() if fn is not None and fn.__doc__ else ""
    meta: dict[str, Any] = {
        "name": action.name,
        "description": doc,
        "reads": list(action.reads or []),
        "writes": list(action.writes or []),
        "required_inputs": required,
        "optional_inputs": optional,
        "input_schemas": _input_schemas(action),
    }
    if ext_map.get(action.name):
        meta["external_tools"] = ext_map[action.name]
    return meta


def _state_json_schema(app: Application[Any]) -> dict[str, Any] | None:
    """Pydantic JSON schema for state when Burr's PydanticTypingSystem is wired.

    Untyped state shows up as None; consumers fall back to inferring shape
    from per-action ``reads``/``writes``.
    """
    try:
        ts = app.state.typing_system
        state_type = ts.state_type() if hasattr(ts, "state_type") else None
        if state_type is not None and hasattr(state_type, "model_json_schema"):
            return state_type.model_json_schema()  # type: ignore[no-any-return]
    except Exception:
        return None
    return None


def _render_action_surface(app: Application[Any]) -> str:
    """Render a compact text summary of the FSM's action + transition surface.

    Appended to the server's `instructions` so an MCP client sees the
    action namespace at connect time, before reading any resources. The
    first line of each action's docstring (if any) becomes its summary;
    transitions show source and target, plus a `(when: expr)` clause for
    conditional edges. Inputs are deliberately omitted; they live on the
    `step` tool's argument schema (or `theodosia://graph` for full detail).
    """
    lines: list[str] = []
    entry = getattr(app, "entrypoint", None)
    if entry:
        lines.append(f"Actions (entry: {entry}):")
    else:
        lines.append("Actions:")
    for a in app.graph.actions:
        first = _first_doc_line(a)
        lines.append(f"  - {a.name}: {first}" if first else f"  - {a.name}")

    lines.extend(("", "Transitions:"))
    for t in app.graph.transitions:
        cond = _condition_expr(t)
        if cond:
            lines.append(f"  - {t.from_.name} -> {t.to.name}  (when: {cond})")
        else:
            lines.append(f"  - {t.from_.name} -> {t.to.name}")
    return "\n".join(lines)


def _normalize_external_tools(
    external_tools: dict[str, list[str]] | None, app: Application[Any]
) -> dict[str, list[str]]:
    """Keep only entries whose action name exists in the graph. Warn on
    unknowns rather than failing, so a typo is recoverable."""
    if not external_tools:
        return {}
    known = {a.name for a in app.graph.actions}
    out: dict[str, list[str]] = {}
    for action_name, tools in external_tools.items():
        if action_name not in known:
            warnings.warn(
                f"external_tools names unknown action {action_name!r}; "
                f"known actions: {sorted(known)}. Ignoring this entry.",
                stacklevel=3,
            )
            continue
        out[action_name] = [t for t in (tools or ()) if isinstance(t, str) and t.strip()]
    return out


def _next_external_tools(
    external_tools_map: dict[str, list[str]], valid_next_actions: list[str]
) -> dict[str, list[str]]:
    """Per-reachable-action external tools, for surfacing in step responses.

    Only includes reachable actions that actually declare external tools,
    so the response stays empty (omitted by the caller) for FSMs that
    don't use the feature.
    """
    return {a: external_tools_map[a] for a in valid_next_actions if external_tools_map.get(a)}


def _compute_graph_summary(
    app: Application[Any],
    server_name: str,
    external_tools_map: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Build a static description of an Application's graph.

    Computed once at mount time and returned as-is by the
    ``theodosia://graph`` resource. Includes per-action metadata
    (description, reads, writes, required/optional inputs) and the
    full transition table including conditions as printed expressions.

    The point of this surface is cold-start discovery: a model
    connecting to the server can read one resource and have the full
    topology without trial-and-error or repeated state probes.
    """
    ext_map = external_tools_map or {}
    actions_meta = [_action_meta(a, ext_map) for a in app.graph.actions]
    transitions_meta = [
        {"from": t.from_.name, "to": t.to.name, "condition": _condition_expr(t)}
        for t in app.graph.transitions
    ]
    state_schema = _state_json_schema(app)

    return {
        "name": server_name,
        "entrypoint": app.entrypoint,
        "actions": actions_meta,
        "transitions": transitions_meta,
        "state_schema": state_schema,
        "meta_tools": [
            {
                "name": "reset_session",
                "description": (
                    "Reset this session's FSM to its entrypoint, clearing "
                    "sub-runs and appending a reset marker to history. "
                    "Always callable regardless of FSM state. Refuses in "
                    "shared-app mode."
                ),
            },
            {
                "name": "fork_at",
                "description": (
                    "Rewind the session to the state captured after a "
                    "specific history entry (by ``seq`` from "
                    "``theodosia://history``). Lets an agent explore alternate "
                    "paths from any checkpoint without losing the audit "
                    "trail. Refuses in shared-app mode."
                ),
            },
            {
                "name": "fork_from_past",
                "description": (
                    "Resume a past Burr run by loading its state from "
                    "disk. Requires the Application to have a "
                    "LocalTrackingClient. Use for resuming sessions "
                    "across server restarts or forking from any "
                    "persisted past app_id."
                ),
            },
        ],
    }
