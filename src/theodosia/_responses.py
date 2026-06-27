"""Response builders + small predicates.

Three tiny utilities that turn dicts into the right wire shape:

* ``_emit_log`` - async notification helper (best-effort).
* ``_step_tool_result`` - wrap a body dict into a FastMCP ``ToolResult``
  with the human headline as the first content block and the JSON body
  as the second, plus structured_content for capable clients.
* ``_success_headline`` / ``_refusal_headline`` - the "Step N: action ⊢
  → next" / "Step N: action × reason" strings shown inline by clients.
  The ⊢ (turnstile) and × glyphs are Theodosia's brand markers for
  allowed and refused outcomes; they're load-bearing across the CLI,
  the docs, and the audit-log surface.
  that render server logs (Bob, Claude Code's streaming output).

Plus ``_has_local_tracker`` - the boolean predicate used in several
places to decide whether to write the ledger or refusal sidecar.
"""

from __future__ import annotations

import contextlib
import json
import os
from typing import Any

from burr.core import Application
from fastmcp import Context
from fastmcp.tools.base import ToolResult
from mcp.types import TextContent


async def _emit_log(ctx: Context | None, msg: str) -> None:
    if ctx is None:
        return
    with contextlib.suppress(Exception):
        await ctx.info(msg)


def _strict_errors() -> bool:
    """Whether to mark guidance refusals as MCP errors (``THEODOSIA_STRICT_ERRORS``).

    Off by default: ``invalid_transition`` / ``validation_failed`` /
    ``unknown_action`` come back as ordinary structured results carrying
    ``valid_next_actions`` for self-correction. Set the env var so an agent
    framework's error-routing (LangGraph error edges, CrewAI/BeeAI tool-error
    retry) sees a refusal as an error. The structured payload is unchanged, so
    the recovery fields are still there.
    """
    return bool(os.environ.get("THEODOSIA_STRICT_ERRORS"))


def _step_tool_result(body: dict[str, Any], headline: str, *, is_error: bool = False) -> ToolResult:
    # Default: a human headline block + a JSON block, plus structured_content.
    # ``THEODOSIA_SINGLE_BLOCK`` emits the JSON alone, for MCP client adapters
    # that mangle a multi-block result (e.g. stringify the list) or ignore
    # structured_content and read only ``content[0]``.
    blocks = [TextContent(type="text", text=json.dumps(body, default=str))]
    if not os.environ.get("THEODOSIA_SINGLE_BLOCK"):
        blocks.insert(0, TextContent(type="text", text=headline))
    return ToolResult(content=blocks, structured_content=body, is_error=is_error)


def _success_headline(seq: int, action: str, valid_next: list[str]) -> str:
    if valid_next:
        peek = ",".join(valid_next[:3])
        more = "" if len(valid_next) <= 3 else f"+{len(valid_next) - 3}"
        return f"Step {seq}: {action} ⊢ → {peek}{more}"
    return f"Step {seq}: {action} ⊢ (terminal)"


def _refusal_headline(seq: int, action: str, refusal_reason: str, detail: str = "") -> str:
    tail = f" ({detail})" if detail else ""
    return f"Step {seq}: {action} × {refusal_reason}{tail}"


def _has_local_tracker(app: Application[Any]) -> bool:
    try:
        from burr.tracking.client import LocalTrackingClient
    except ImportError:
        return False
    return isinstance(getattr(app, "_tracker", None), LocalTrackingClient)
