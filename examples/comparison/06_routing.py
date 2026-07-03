"""A fourth three-way: LangGraph's Routing workflow, where the agent chooses.

Routing is the first of these workflows that BRANCHES, so "query your legal next
moves and pick one" stops being decoration. The routing decision has to live
somewhere, and that is the whole comparison:

  LangGraph (01-style)   a router node classifies, a conditional edge follows it.
                         The graph decides.
  Burr orchestrator      a `route` action writes a decision, conditioned
                         transitions follow it. The runtime decides.
  Theodosia              from `load`, all three writers are legal; the caller
                         reads theodosia://next and picks one. The AGENT decides,
                         and the server bounds the choice to the legal set.

Source (Routing, Graph API), reproduced verbatim in ``langgraph_route`` with a
stubbed classifier and writers so it runs key-free:
    https://docs.langchain.com/oss/python/langgraph/workflows-agents

Run:  ../../.venv/bin/python 06_routing.py
      COMPARISON_LIVE=1 ../../.venv/bin/python 06_routing.py
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
from typing import Any, TypedDict

from burr.core import ApplicationBuilder, State, action
from burr.core.action import Condition
from fastmcp import Client
from langgraph.graph import END, START, StateGraph

sys.path.insert(0, os.path.dirname(__file__))
from theodosia import mount

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent

KINDS = ["story", "joke", "poem"]


def classify(request: str) -> str:
    """Stub for the docs' structured-output router (``llm.with_structured_output``)."""
    r = request.lower()
    for kind in ("joke", "poem", "story"):
        if kind in r:
            return kind
    return "story"


def write(kind: str, request: str) -> str:
    """Stub for a handler's ``llm.invoke``."""
    return f"a {kind} about {request.split('about')[-1].strip() or request}"


# ── 01: LangGraph Routing, verbatim structure ─────────────────────────
class RouteState(TypedDict, total=False):
    input: str
    decision: str
    output: str


def langgraph_route(request: str) -> dict[str, str]:
    def llm_call_1(s: RouteState) -> dict:
        """Write a story"""
        return {"output": write("story", s["input"])}

    def llm_call_2(s: RouteState) -> dict:
        """Write a joke"""
        return {"output": write("joke", s["input"])}

    def llm_call_3(s: RouteState) -> dict:
        """Write a poem"""
        return {"output": write("poem", s["input"])}

    def llm_call_router(s: RouteState) -> dict:
        """Route the input to the appropriate node"""
        return {"decision": classify(s["input"])}

    def route_decision(s: RouteState) -> str:
        """Conditional edge function to route to the appropriate node"""
        return {"story": "llm_call_1", "joke": "llm_call_2", "poem": "llm_call_3"}[s["decision"]]

    b = StateGraph(RouteState)
    b.add_node("llm_call_1", llm_call_1)
    b.add_node("llm_call_2", llm_call_2)
    b.add_node("llm_call_3", llm_call_3)
    b.add_node("llm_call_router", llm_call_router)
    b.add_edge(START, "llm_call_router")
    b.add_conditional_edges(
        "llm_call_router",
        route_decision,
        {"llm_call_1": "llm_call_1", "llm_call_2": "llm_call_2", "llm_call_3": "llm_call_3"},
    )
    b.add_edge("llm_call_1", END)
    b.add_edge("llm_call_2", END)
    b.add_edge("llm_call_3", END)
    out = b.compile().invoke({"input": request})
    return {"decision": classify(request), "output": out["output"]}


# ── shared Burr writers, and two builders (decided vs open) ────────────
# Burr action  <->  LangGraph node (in langgraph_route above)
#   story      <->  llm_call_1     joke <-> llm_call_2     poem <-> llm_call_3
#   route      <->  llm_call_router (+ the conditioned transitions <-> route_decision)
@action(reads=["input"], writes=["output"])
def story(state: State) -> State:
    """Write a story"""
    return state.update(output=write("story", state["input"]))


@action(reads=["input"], writes=["output"])
def joke(state: State) -> State:
    """Write a joke"""
    return state.update(output=write("joke", state["input"]))


@action(reads=["input"], writes=["output"])
def poem(state: State) -> State:
    """Write a poem"""
    return state.update(output=write("poem", state["input"]))


@action(reads=[], writes=["input", "decision"])
def route(state: State, request: str) -> State:
    """Route the input to the appropriate node"""
    return state.update(input=request, decision=classify(request))


@action(reads=[], writes=["input", "loaded"])
def load(state: State, request: str) -> State:
    return state.update(input=request, loaded=True)


def build_router_decided() -> ApplicationBuilder:
    """Burr, orchestrator style: the graph routes via conditioned transitions."""
    return (
        ApplicationBuilder()
        .with_actions(route=route, story=story, joke=joke, poem=poem)
        .with_transitions(
            ("route", "story", Condition.expr("decision == 'story'")),
            ("route", "joke", Condition.expr("decision == 'joke'")),
            ("route", "poem", Condition.expr("decision == 'poem'")),
        )
        .with_state(input="", decision="", output="")
        .with_entrypoint("route")
    )


def build_router_open() -> ApplicationBuilder:
    """Theodosia: from `load`, all three writers are legal; the caller chooses."""
    return (
        ApplicationBuilder()
        .with_actions(load=load, story=story, joke=joke, poem=poem)
        .with_transitions(
            ("load", "story", Condition.expr("loaded")),
            ("load", "joke", Condition.expr("loaded")),
            ("load", "poem", Condition.expr("loaded")),
        )
        .with_state(input="", output="", loaded=False)
        .with_entrypoint("load")
    )


# ── 02: Burr orchestrator ─────────────────────────────────────────────
def burr_route(request: str) -> dict[str, str]:
    app = build_router_decided().build()
    app.step(inputs={"request": request})  # route: classify + write decision
    app.step()  # the conditioned transition runs the chosen writer
    state = dict(app.state.get_all())
    return {"decision": state["decision"], "output": state["output"]}


# ── 03: Theodosia (the caller picks among legal next-states) ──────────
async def _step(client: Client, act: str, inputs: dict | None = None) -> dict:
    args: dict[str, Any] = {"action": act}
    if inputs is not None:
        args["inputs"] = inputs
    r = await client.call_tool("step", args, raise_on_error=False)
    return r.structured_content


async def _res(client: Client, uri: str) -> Any:
    return json.loads((await client.read_resource(uri))[0].text)


async def _theodosia_route(request: str) -> dict[str, Any]:
    server = mount(build_router_open, name="writer")
    async with Client(server) as c:
        early = await _step(c, "joke")  # picking a branch before load
        await _step(c, "load", {"request": request})
        legal = await _res(c, "theodosia://next")  # all three writers are open
        choice = classify(request)  # the CALLER decides which door to walk
        final = await _step(c, choice)
        history = await _res(c, "theodosia://history")
    return {
        "refused_before_load": early.get("error"),
        "legal_choices": legal,
        "decision": choice,
        "output": final["state"]["output"],
        "refused_in_ledger": len([h for h in history if h.get("refused")]),
    }


def theodosia_route(request: str) -> dict[str, Any]:
    return asyncio.run(_theodosia_route(request))


# ── live: a real Claude agent does the routing ───────────────────────
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


async def _route_with_claude_agent(request: str) -> dict[str, Any] | None:
    if not _claude_ready():
        return None
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        ToolUseBlock,
        query,
    )

    home = Path(tempfile.gettempdir()) / "theodosia-comparison-routing"
    os.environ["THEODOSIA_HOME"] = str(home)
    os.environ["THEODOSIA_QUIET"] = "1"
    home.mkdir(parents=True, exist_ok=True)

    launch = [
        str(REPO / ".venv" / "bin" / "theodosia"),
        "serve",
        "06_routing:build_router_open",
        "--app-dir",
        str(HERE),
    ]
    options = ClaudeAgentOptions(
        mcp_servers={"writer": {"type": "stdio", "command": launch[0], "args": launch[1:]}},
        allowed_tools=["mcp__writer__*"],
        permission_mode="bypassPermissions",
        model=os.environ.get("COMPARISON_MODEL"),
        max_budget_usd=float(os.environ.get("COMPARISON_BUDGET", "2.0")),
        max_turns=15,
    )
    prompt = (
        f"Request: {request!r}. Drive the mounted `step` state machine: first `load` the request, "
        "then read theodosia://next and choose the ONE writer action (story, joke, or poem) that "
        "best matches the request. Take that one step to a terminal state."
    )
    picks: list[str] = []
    result: Any = None
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, AssistantMessage):
            picks.extend(
                dict(b.input).get("action", "?")
                for b in msg.content
                if isinstance(b, ToolUseBlock) and b.name.endswith("step")
            )
        elif isinstance(msg, ResultMessage):
            result = msg
    return {"agent_steps": picks, "cost_usd": getattr(result, "total_cost_usd", None)}


def route_with_claude_agent(request: str) -> dict[str, Any] | None:
    return asyncio.run(_route_with_claude_agent(request))


def main() -> None:
    request = "Write me a joke about cats"
    print(f"request: {request!r}\n")

    lg = langgraph_route(request)
    br = burr_route(request)
    th = theodosia_route(request)

    print(f"  01 langgraph      decision={lg['decision']}  (router node + conditional edge)")
    print(
        f"  02 burr           decision={br['decision']}  (route action + conditioned transitions)"
    )
    print(
        f"  03 theodosia      decision={th['decision']}  from legal choices {th['legal_choices']}"
    )
    print(
        f"                    (the caller picked; a branch before `load` was refused "
        f"{th['refused_before_load']!r})"
    )
    assert lg["decision"] == br["decision"] == th["decision"] == "joke"
    assert lg["output"] == br["output"] == th["output"]
    assert th["legal_choices"] == KINDS
    print(f"\n  all three routed to: {th['output']}")
    print("  the difference is WHO chose: the graph, the runtime, or the agent.")

    if os.environ.get("COMPARISON_LIVE") == "1":
        print("\n[live] a real Claude agent reads its legal moves and routes...")
        live = route_with_claude_agent(request)
        if live is not None:
            print(f"  agent steps: {live['agent_steps']}  cost_usd: {live['cost_usd']}")


if __name__ == "__main__":
    main()
