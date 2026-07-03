"""Rendering 3 of 3: the same Burr prompt chain, mounted over MCP with Theodosia.

Same graph as ``02_burr_orchestrator.py`` (the identical ``build_joke_app``); the
only new code is one ``mount()`` call. Now the graph is a server and the agent is
the caller. What that adds, none of it in the graph:

  * the caller queries its legal next moves            (theodosia://next)
  * an out-of-order step is refused server-side        (invalid_transition)
  * ``generate_joke``'s ``topic`` is a validated slot  (validation_failed)
  * every attempt, including refusals, is on a ledger  (theodosia://history)

  run_theodosia(topic)          deterministic, offline, no model.
  drive_with_claude_agent(topic)  [COMPARISON_LIVE=1] a real Claude agent drives
                                  it on your authenticated Claude Code session.

Run:  ../../.venv/bin/python 03_theodosia_mcp.py
      COMPARISON_LIVE=1 ../../.venv/bin/python 03_theodosia_mcp.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from fastmcp import Client

sys.path.insert(0, os.path.dirname(__file__))
from _shared import build_joke_app, final_text

from theodosia import mount

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent


# ── deterministic in-process drive (no model) ─────────────────────────
async def _res(client: Client, uri: str) -> Any:
    return json.loads((await client.read_resource(uri))[0].text)


async def _step(client: Client, action: str, inputs: dict | None = None) -> dict:
    args: dict[str, Any] = {"action": action}
    if inputs is not None:
        args["inputs"] = inputs
    r = await client.call_tool("step", args, raise_on_error=False)
    return r.structured_content


async def _drive_inprocess(topic: str) -> dict[str, Any]:
    server = mount(build_joke_app, name="jokes")  # the one new line
    transcript: list[dict[str, Any]] = []

    async with Client(server) as c:
        transcript.append({"read": "theodosia://next", "value": await _res(c, "theodosia://next")})

        refused_order = await _step(c, "polish_joke")  # out of order
        transcript.append(
            {
                "step": "polish_joke@start",
                "refused": refused_order.get("error"),
                "valid": refused_order.get("valid_next_actions"),
            }
        )

        refused_slot = await _step(c, "generate_joke", {"topic": ""})  # empty slot
        transcript.append(
            {
                "step": "generate_joke empty topic",
                "refused": refused_slot.get("error"),
                "reason": refused_slot.get("reason"),
            }
        )

        await _step(c, "generate_joke", {"topic": topic})
        transcript.append(
            {"read": "theodosia://next after generate", "value": await _res(c, "theodosia://next")}
        )
        await _step(c, "improve_joke")
        final = await _step(c, "polish_joke")
        state = final["state"]

        history = await _res(c, "theodosia://history")

    return {
        "final": final_text(state),
        "verdict": state["verdict"],
        "transcript": transcript,
        "ledger_entries": len(history),
        "refused_in_ledger": len([h for h in history if h.get("refused")]),
    }


def run_theodosia(topic: str) -> dict[str, Any]:
    return asyncio.run(_drive_inprocess(topic))


# ── live: a real Claude agent drives the server (authed session) ──────
def _claude_ready() -> bool:
    if shutil.which("claude") is None:
        print("[live skipped] `claude` CLI not on PATH.")
        return False
    try:
        proc = subprocess.run(
            ["claude", "auth", "status"], capture_output=True, text=True, timeout=10
        )
        if not json.loads(proc.stdout or "{}").get("loggedIn"):
            raise ValueError
    except Exception:
        print(
            "[live skipped] not logged in. Run `claude auth login`, then retry COMPARISON_LIVE=1."
        )
        return False
    return True


async def _drive_with_claude_agent(topic: str) -> dict[str, Any] | None:
    if not _claude_ready():
        return None
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        ToolUseBlock,
        query,
    )

    home = Path(tempfile.gettempdir()) / "theodosia-comparison-jokes"
    os.environ["THEODOSIA_HOME"] = str(home)
    os.environ["THEODOSIA_PROJECT"] = "comparison-jokes"
    os.environ["THEODOSIA_QUIET"] = "1"
    home.mkdir(parents=True, exist_ok=True)

    launch = [
        str(REPO / ".venv" / "bin" / "theodosia"),
        "serve",
        "_shared:build_joke_app",
        "--app-dir",
        str(HERE),
    ]
    options = ClaudeAgentOptions(
        mcp_servers={"jokes": {"type": "stdio", "command": launch[0], "args": launch[1:]}},
        allowed_tools=["mcp__jokes__*"],
        permission_mode="bypassPermissions",
        model=os.environ.get("COMPARISON_MODEL"),
        max_budget_usd=float(os.environ.get("COMPARISON_BUDGET", "2.0")),
        max_turns=20,
    )
    prompt = (
        f"Write and polish a joke about '{topic}' by driving the mounted `step` state machine to a "
        "terminal state. The steps are generate_joke (needs a topic), improve_joke, polish_joke, "
        "in that order. If a step is refused, read valid_next_actions or the reason and continue."
    )

    slot_fills: list[dict[str, Any]] = []
    result: Any = None
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, ToolUseBlock) and block.name.endswith("step"):
                    inp = dict(block.input)
                    slot_fills.append({"action": inp.get("action"), "inputs": inp.get("inputs")})
        elif isinstance(msg, ResultMessage):
            result = msg

    return {"slot_fills": slot_fills, "cost_usd": getattr(result, "total_cost_usd", None)}


def drive_with_claude_agent(topic: str) -> dict[str, Any] | None:
    return asyncio.run(_drive_with_claude_agent(topic))


if __name__ == "__main__":
    topic = sys.argv[1] if len(sys.argv) > 1 else "cat"
    print(f"[theodosia over mcp] topic={topic}")
    out = run_theodosia(topic)
    for line in out["transcript"]:
        print("  ", json.dumps(line))
    print(f"  final: {out['final']}")
    print(f"  ledger: {out['ledger_entries']} entries, {out['refused_in_ledger']} refused")

    if os.environ.get("COMPARISON_LIVE") == "1":
        print("\n[live] handing the mounted chain to a real Claude agent (authed session)...")
        live = drive_with_claude_agent(topic)
        if live is not None:
            for sf in live["slot_fills"]:
                print(f"    step {sf['action']:<14} inputs={sf['inputs']}")
            print(f"  cost_usd: {live['cost_usd']}")
