"""A seventh comparison: two independent agents driving one mounted machine.

Ported from LangGraph's multi-agent-collaboration tutorial:
    https://github.com/langchain-ai/langgraph/blob/23961cff61a42b52525f3b20b4094d8d2fba1744/docs/docs/tutorials/multi_agent/multi-agent-collaboration.ipynb

There a `researcher` and a `chart_generator` share one state; the chart generator
runs code through `python_repl_tool` (arbitrary local exec, flagged "UNSAFE WHEN
NOT SANDBOXED"). Two things a server should hold: the handoff protocol (you cannot
chart before research produced data) and a gate on the code the charter runs.

Fidelity: this keeps the source's two node names (researcher <-> research,
chart_generator <-> chart) and the shared-state handoff, but drops the tutorial's
ReAct ping-pong routing (`get_next_node`); `finalize` is added to close the run.

The renderings the earlier examples could not make: mounting a BUILT application
(not a factory) shares one FSM across MCP sessions, so **two independent clients
drive the same machine**. A researcher client and a charter client, potentially in
different frameworks, coordinate only through the server. The server enforces the
handoff, gates the charter's spec, and the durable ledger records BOTH clients'
attempts in one hash-chained trail.

  langgraph_collab / burr_collab   single process; the chart step runs the spec
                                   ungated, so a malicious spec executes.
  two_agents_drive                 one mounted machine, two client sessions; the
                                   malicious spec is refused, the handoff is
                                   enforced, and one ledger holds both agents.

Run:  ../../.venv/bin/python 09_multi_agent.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, TypedDict

from burr.core import ApplicationBuilder, State, action
from burr.core.action import Condition
from fastmcp import Client
from langgraph.graph import END, START, StateGraph

sys.path.insert(0, os.path.dirname(__file__))
from theodosia import ValidationFailed, mount

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent

# The charter's consequential capability: code that actually ran (python_repl).
EXECUTED: list[str] = []

_UNSAFE = (
    "import",
    "__",
    "exec(",
    "eval(",
    "open(",
    "os.",
    "sys.",
    "subprocess",
    "system(",
    ";",
    "`",
)


def is_safe_spec(spec: str) -> bool:
    s = spec.lower()
    return bool(s.strip()) and not any(tok in s for tok in _UNSAFE)


def execute_chart(spec: str) -> str:
    EXECUTED.append(spec)  # python_repl_tool would run this
    return f"chart[{spec}]"


# ── the Burr collaboration machine (research -> chart -> finalize) ────
@action(reads=[], writes=["stage", "data"])
def research(state: State, finding: str) -> State:
    return state.update(stage="researched", data=finding)


@action(reads=["data"], writes=["stage", "chart"])
def chart(state: State, spec: str) -> State:
    return state.update(stage="charted", chart=execute_chart(spec))


@action(reads=["chart", "data"], writes=["stage", "report"])
def finalize(state: State) -> State:
    return state.update(stage="done", report=f"{state['chart']} from {state['data']!r}")


def _chart_gate(state: dict, inputs: dict) -> dict | None:
    spec = str(inputs.get("spec") or "")
    if not is_safe_spec(spec):
        raise ValidationFailed(
            "chart refused: spec must be a chart description, not code",
            details={"spec": spec},
        )
    return None


chart._theodosia_validator = _chart_gate  # type: ignore[attr-defined]


def build_collab_app(*, track: bool = False) -> ApplicationBuilder:
    builder = (
        ApplicationBuilder()
        .with_actions(research=research, chart=chart, finalize=finalize)
        .with_transitions(
            ("research", "chart", Condition.expr("stage == 'researched'")),
            ("chart", "finalize", Condition.expr("stage == 'charted'")),
        )
        .with_state(stage="new", data="", chart="", report="")
        .with_entrypoint("research")
    )
    if track:
        from theodosia import tracker

        builder = builder.with_tracker(tracker(project="comparison-collab"))
    return builder


# ── langgraph_collab: research -> chart handoff; chart runs ungated ───
def langgraph_collab(finding: str, spec: str) -> dict[str, Any]:
    class S(TypedDict, total=False):
        data: str
        chart: str

    def n_research(s: S) -> dict:
        return {"data": finding}

    def n_chart(s: S) -> dict:
        return {"chart": execute_chart(spec)}  # python_repl: runs whatever the charter emitted

    g = StateGraph(S)
    g.add_node("researcher", n_research)
    g.add_node("chart_generator", n_chart)
    g.add_edge(START, "researcher")
    g.add_edge("researcher", "chart_generator")
    g.add_edge("chart_generator", END)
    out = g.compile().invoke({})
    return {"chart": out["chart"]}


def burr_collab(finding: str, spec: str) -> dict[str, Any]:
    app = build_collab_app().build()
    app.step(inputs={"finding": finding})  # research
    app.step(inputs={"spec": spec})  # chart (raw Burr ignores the gate)
    return {"chart": app.state["chart"]}


# ── two_agents_drive: two clients, one mounted machine, one ledger ────
async def _step(c: Client, act: str, inputs: dict | None = None) -> dict:
    args: dict[str, Any] = {"action": act}
    if inputs is not None:
        args["inputs"] = inputs
    r = await c.call_tool("step", args, raise_on_error=False)
    return r.structured_content


async def _res(c: Client, uri: str) -> Any:
    return json.loads((await c.read_resource(uri))[0].text)


def _read_ledger(home: Path) -> list[dict]:
    entries: list[dict] = []
    for path in home.rglob("ledger.jsonl"):
        for line in path.read_text().splitlines():
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


async def _two_agents_drive() -> dict[str, Any]:
    EXECUTED.clear()
    home = Path(tempfile.gettempdir()) / "theodosia-comparison-collab"
    if home.exists():
        for p in home.rglob("ledger.jsonl"):
            p.unlink()
    os.environ["THEODOSIA_HOME"] = str(home)
    os.environ["THEODOSIA_QUIET"] = "1"

    server = mount(build_collab_app(track=True).build(), name="collab")  # built = shared machine
    transcript: list[dict[str, Any]] = []

    async with Client(server) as researcher:  # agent 1
        await _step(researcher, "research", {"finding": "otters use tools"})

    async with Client(server) as charter:  # agent 2, a separate session, shared state
        seen = await _res(charter, "theodosia://state")
        transcript.append(
            {"charter_sees": seen.get("data"), "next": await _res(charter, "theodosia://next")}
        )
        mal = await _step(charter, "chart", {"spec": "__import__('os').system('rm -rf /')"})
        transcript.append({"malicious_chart": mal.get("error"), "reason": mal.get("reason")})
        ok = await _step(charter, "chart", {"spec": "bar chart of tool use"})
        await _step(charter, "finalize")

    ledger = _read_ledger(home)
    actions = [e.get("action") for e in ledger if e.get("action")]
    return {
        "transcript": transcript,
        "chart_made": ok["state"]["chart"],
        "executed": list(EXECUTED),
        "ledger_actions": actions,
        "ledger_refused": len([e for e in ledger if e.get("refused")]),
    }


def two_agents_drive() -> dict[str, Any]:
    return asyncio.run(_two_agents_drive())


def main() -> None:
    finding = "otters use tools"
    malicious = "__import__('os').system('rm -rf /')"

    print("single process: the chart step runs whatever the charter emits.")
    EXECUTED.clear()
    langgraph_collab(finding, malicious)
    lg_exec = list(EXECUTED)
    EXECUTED.clear()
    burr_collab(finding, malicious)
    burr_exec = list(EXECUTED)
    print(f"    langgraph_collab executed: {lg_exec}")
    print(f"    burr_collab executed:      {burr_exec}   <- arbitrary code ran")

    print("\ntwo independent clients drive ONE mounted machine:")
    tw = two_agents_drive()
    for t in tw["transcript"]:
        print(f"    {json.dumps(t)}")
    print(f"    chart made: {tw['chart_made']}")
    print(f"    code that actually executed: {tw['executed']}   <- the malicious spec never ran")
    print(f"    one ledger, both agents: {tw['ledger_actions']}  ({tw['ledger_refused']} refused)")

    assert malicious in burr_exec and malicious not in tw["executed"]
    assert "research" in tw["ledger_actions"] and "chart" in tw["ledger_actions"]
    print("\n  the researcher and the charter share no code and never talk directly;")
    print("  the server enforces the handoff and the gate, and records both in one ledger.")


if __name__ == "__main__":
    main()
