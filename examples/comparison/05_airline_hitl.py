"""Sourced scenario: LangGraph's own sensitive-tool gate vs Theodosia's.

The LangGraph side is the real gating mechanism from the customer-support
tutorial: a safe/sensitive tool split compiled with
``interrupt_before=["sensitive_tools"]`` (a client-side pause for approval).
Source (verbatim structure):
    https://github.com/langchain-ai/langgraph/blob/23961cff61a42b52525f3b20b4094d8d2fba1744/docs/docs/tutorials/customer-support/customer-support.ipynb
The shipped helper behind that pause is ``HumanInterrupt`` / ``interrupt([...])``
(``langgraph.prebuilt.interrupt``, moved to ``langchain.agents.interrupt`` in v1).

Three functions:

  langgraph_world()   the real interrupt_before gate. It pauses before the
                      mutation (good), but the gate lives in the graph this
                      client runs: the sensitive tool is a plain callable, so a
                      second caller invokes it directly with no pause.

  theodosia_world()   the same rebooking mounted as an FSM. The mutation is
                      reachable only through `step` after a server-enforced
                      `confirm`, so no client can skip it, and slot values
                      (new_flight_id) are validated against the searched flights.

  slot_fill_live()    [COMPARISON_LIVE=1] a real Claude agent drives the mounted
                      FSM. Because every step is step(action, inputs), it must
                      fill each action's slots; we report what it filled and any
                      recovery from a refused slot.

Run:  ../../.venv/bin/python 05_airline_hitl.py
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
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode

sys.path.insert(0, os.path.dirname(__file__))
from _airline import (
    COMMITS,
    build_airline_app,
    confirmed_commit,
    search_flights,
    update_ticket_to_new_flight,
)

from theodosia import mount

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent


# ── LangGraph world: the real interrupt_before gate + the side door ───
@tool
def lg_search_flights(route: str, date: str) -> list:
    """Search available flights (safe, read-only)."""
    return search_flights(route, date)


@tool
def lg_update_ticket_to_new_flight(ticket_no: str, new_flight_id: int) -> dict:
    """Rebook a ticket to a new flight (SENSITIVE: mutates the booking)."""
    return update_ticket_to_new_flight(ticket_no, new_flight_id)


SAFE_TOOLS = [lg_search_flights]
SENSITIVE_TOOLS = [lg_update_ticket_to_new_flight]
SENSITIVE_NAMES = {t.name for t in SENSITIVE_TOOLS}


def _assistant(state: MessagesState) -> dict:
    # A real model decides this; stubbed so the demo runs key-free. It asks to
    # rebook TKT-42 onto flight 205, a sensitive mutation.
    if any(isinstance(m, AIMessage) and m.tool_calls for m in state["messages"]):
        return {"messages": [AIMessage(content="rebooked")]}
    call = {
        "name": "lg_update_ticket_to_new_flight",
        "args": {"ticket_no": "TKT-42", "new_flight_id": 205},
        "id": "call_1",
    }
    return {"messages": [AIMessage(content="", tool_calls=[call])]}


def _route(state: MessagesState) -> str:
    last = state["messages"][-1]
    if not getattr(last, "tool_calls", None):
        return END
    return "sensitive_tools" if last.tool_calls[0]["name"] in SENSITIVE_NAMES else "safe_tools"


def build_langgraph_agent():
    """The tutorial's gating: split safe/sensitive tools, interrupt before sensitive."""
    g = StateGraph(MessagesState)
    g.add_node("assistant", _assistant)
    g.add_node("safe_tools", ToolNode(SAFE_TOOLS))
    g.add_node("sensitive_tools", ToolNode(SENSITIVE_TOOLS))
    g.add_edge(START, "assistant")
    g.add_conditional_edges("assistant", _route, ["safe_tools", "sensitive_tools", END])
    g.add_edge("safe_tools", "assistant")
    g.add_edge("sensitive_tools", "assistant")
    return g.compile(checkpointer=InMemorySaver(), interrupt_before=["sensitive_tools"])


def langgraph_world() -> dict[str, Any]:
    COMMITS.clear()
    agent = build_langgraph_agent()
    cfg = {"configurable": {"thread_id": "A"}}

    agent.invoke({"messages": [HumanMessage("Rebook ticket TKT-42 to flight 205")]}, cfg)
    paused = agent.get_state(cfg)
    interrupted = bool(paused.next)  # ('sensitive_tools',): the gate fired for this path
    commits_before_approval = len(COMMITS)

    agent.invoke(None, cfg)  # the client approves; the sensitive tool now runs
    commits_after_approval = len(COMMITS)

    # A second caller (another team, a headless runner) that does not run this
    # graph reaches the same capability directly. The interrupt is not in the way.
    lg_update_ticket_to_new_flight.invoke({"ticket_no": "TKT-42", "new_flight_id": 206})

    return {
        "interrupted": interrupted,
        "pending_node": list(paused.next),
        "commits_before_approval": commits_before_approval,
        "commits_after_approval": commits_after_approval,
        "side_door_commit": len(COMMITS) > commits_after_approval,
        "commits_total": len(COMMITS),
    }


# ── Theodosia world: the same rebooking, server-gated ─────────────────
async def _step(client: Client, action: str, inputs: dict | None = None) -> dict:
    args: dict[str, Any] = {"action": action}
    if inputs is not None:
        args["inputs"] = inputs
    r = await client.call_tool("step", args, raise_on_error=False)
    return r.structured_content


async def _res(client: Client, uri: str) -> Any:
    return json.loads((await client.read_resource(uri))[0].text)


async def _theodosia_world() -> dict[str, Any]:
    COMMITS.clear()
    server = mount(lambda: build_airline_app(track=False), name="airline")
    transcript: list[dict[str, Any]] = []

    async with Client(server) as c:
        transcript.append({"read": "theodosia://next", "value": await _res(c, "theodosia://next")})

        r = await _step(c, "rebook", {"new_flight_id": 205})  # the mutation, up front
        transcript.append(
            {
                "step": "rebook@start",
                "refused": r.get("error"),
                "valid": r.get("valid_next_actions"),
            }
        )

        await _step(c, "find", {"booking_ref": "TKT-42"})
        options = (await _step(c, "search", {"date": "2026-08-02"}))["state"]["options"]

        r = await _step(c, "rebook", {"new_flight_id": 205})  # before confirm
        transcript.append(
            {
                "step": "rebook before confirm",
                "refused": r.get("error"),
                "valid": r.get("valid_next_actions"),
            }
        )

        await _step(c, "confirm", {"acknowledge": "yes, move me to 205"})

        r = await _step(c, "rebook", {"new_flight_id": 999})  # a flight not searched
        transcript.append(
            {"step": "rebook bad slot 999", "refused": r.get("error"), "reason": r.get("reason")}
        )

        final = await _step(c, "rebook", {"new_flight_id": options[0]})
        history = await _res(c, "theodosia://history")

    return {
        "options_from_search": options,
        "rebooked": "error" not in final,
        "final_stage": final.get("state", {}).get("stage"),
        "transcript": transcript,
        "commits_total": len(COMMITS),
        "unconfirmed_commits": len([x for x in COMMITS if not confirmed_commit(x)]),
        "refused_in_ledger": len([h for h in history if h.get("refused")]),
    }


def theodosia_world() -> dict[str, Any]:
    return asyncio.run(_theodosia_world())


# ── Live: a real Claude agent fills slots via step(action, inputs) ────
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


async def _slot_fill_live() -> dict[str, Any] | None:
    if not _claude_ready():
        return None
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        ToolUseBlock,
        query,
    )

    home = Path(tempfile.gettempdir()) / "theodosia-comparison-airline"
    os.environ["THEODOSIA_HOME"] = str(home)
    os.environ["THEODOSIA_PROJECT"] = "comparison-airline"
    os.environ["THEODOSIA_QUIET"] = "1"
    home.mkdir(parents=True, exist_ok=True)

    launch = [
        str(REPO / ".venv" / "bin" / "theodosia"),
        "serve",
        "_airline:build_airline_app",
        "--app-dir",
        str(HERE),
    ]
    options = ClaudeAgentOptions(
        mcp_servers={"airline": {"type": "stdio", "command": launch[0], "args": launch[1:]}},
        allowed_tools=["mcp__airline__*"],
        permission_mode="bypassPermissions",
        model=os.environ.get("COMPARISON_MODEL"),
        max_budget_usd=float(os.environ.get("COMPARISON_BUDGET", "2.0")),
        max_turns=25,
    )
    prompt = (
        "Rebook ticket TKT-42 onto a flight departing 2026-08-02 by driving the mounted `step` "
        "state machine to a terminal rebooked state. The steps are find, search, confirm, rebook. "
        "`find` needs booking_ref; `search` needs date; `confirm` needs an acknowledge string; "
        "`rebook` needs new_flight_id, which must be one of the flights `search` returned. "
        "If a step is refused, read valid_next_actions or the reason and continue."
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


def slot_fill_live() -> dict[str, Any] | None:
    return asyncio.run(_slot_fill_live())


def main() -> None:
    print("scenario: rebook a ticket (a sensitive mutation). LangGraph gates it in the graph;")
    print("Theodosia gates it in the server.\n")

    lw = langgraph_world()
    lb, la = lw["commits_before_approval"], lw["commits_after_approval"]
    print("LANGGRAPH WORLD  (interrupt_before=['sensitive_tools'], the tutorial's gate)")
    print(f"  paused before the mutation? {lw['interrupted']}  pending={lw['pending_node']}")
    print(f"  commits: {lb} before approval, {la} after")
    print(f"  a second caller hit the sensitive tool directly: {lw['side_door_commit']}")
    print(f"  total mutations: {lw['commits_total']}  <- one never passed any gate")

    tw = theodosia_world()
    tot, unc, ref = tw["commits_total"], tw["unconfirmed_commits"], tw["refused_in_ledger"]
    print("\nTHEODOSIA WORLD  (rebook reachable only via `step` after a server-enforced confirm)")
    print(f"  search returned flights: {tw['options_from_search']}")
    for t in tw["transcript"]:
        if "step" in t:
            print(f"    {t['step']:<22} refused={t.get('refused')}")
    print(f"  rebooked: {tw['rebooked']} (stage={tw['final_stage']})")
    print(f"  mutations: {tot}  unconfirmed: {unc}  refused in ledger: {ref}")

    assert lw["side_door_commit"] is True
    assert tw["unconfirmed_commits"] == 0 and tw["rebooked"] is True

    if os.environ.get("COMPARISON_LIVE") == "1":
        print("\n[live] a real Claude agent fills the slots via step(action, inputs)...")
        live = slot_fill_live()
        if live is not None:
            for sf in live["slot_fills"]:
                print(f"    step {sf['action']:<8} inputs={sf['inputs']}")
            print(f"  cost_usd: {live['cost_usd']}")


if __name__ == "__main__":
    main()
