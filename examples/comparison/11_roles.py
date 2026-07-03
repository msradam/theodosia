"""A ninth comparison: different agents, different phases, one shared machine.

Distills the roles-and-phases idea from LangGraph's hierarchical-agent-teams
tutorial:
    https://github.com/langchain-ai/langgraph/blob/23961cff61a42b52525f3b20b4094d8d2fba1744/docs/docs/tutorials/multi_agent/hierarchical_agent_teams.ipynb

This is NOT a structural port. That tutorial is a two-level hierarchy: a top
supervisor routes between a research_team (search, web_scraper) and a
writing_team (doc_writer, note_taker, chart_generator), each with its own
supervisor `Router`. We keep only the shape that matters for the comparison, a
three-role pipeline (research -> write -> review, with a review that can loop
back), so the point about phases and entry points is legible.

The idea this makes concrete: a single mounted machine can have distinct phases,
and different agents enter at different phases while the machine keeps the state.
A researcher, a writer, and a reviewer each drive their own MCP session; none of
them share code or talk directly. The machine enforces the phase order (you cannot
write before research, cannot review before a draft), gates the review verdict,
carries `notes`/`draft`/`review_notes` across every session, and runs the
revise-and-re-review loop. One ledger records the whole collaboration.

Each agent, on connecting, reads `theodosia://next` to learn whether its phase is
open. A writer that connects too early is refused: its entry point is not yet
reachable.

  langgraph_report / burr_report   the same pipeline in one process; the roles are
                                   just nodes one script runs.
  roles_collaborate                one mounted machine, three role-agents in three
                                   sessions, a review loop, one ledger.

Run:  ../../.venv/bin/python 11_roles.py
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


# ── the Burr report machine: research -> write -> review (-> loop) ────
@action(reads=[], writes=["stage", "notes"])
def research(state: State, notes: str) -> State:
    return state.update(stage="researched", notes=notes)


@action(reads=["notes"], writes=["stage", "draft", "revisions"])
def write(state: State, draft: str) -> State:
    return state.update(stage="drafted", draft=draft, revisions=state.get("revisions", 0) + 1)


@action(reads=["draft"], writes=["stage", "verdict", "review_notes"])
def review(state: State, verdict: str, comments: str = "") -> State:
    approved = verdict == "approve"
    return state.update(
        stage="approved" if approved else "rejected", verdict=verdict, review_notes=comments
    )


def _review_gate(state: dict, inputs: dict) -> dict | None:
    v = inputs.get("verdict")
    if v not in ("approve", "reject"):
        raise ValidationFailed(
            "verdict must be 'approve' or 'reject'",
            details={"got": v, "allowed": ["approve", "reject"]},
        )
    return None


review._theodosia_validator = _review_gate  # type: ignore[attr-defined]


def build_report_app(*, track: bool = False) -> ApplicationBuilder:
    builder = (
        ApplicationBuilder()
        .with_actions(research=research, write=write, review=review)
        .with_transitions(
            ("research", "write", Condition.expr("stage == 'researched'")),
            ("write", "review", Condition.expr("stage == 'drafted'")),
            ("review", "write", Condition.expr("stage == 'rejected'")),  # the revision loop
        )
        .with_state(stage="new", notes="", draft="", review_notes="", verdict="", revisions=0)
        .with_entrypoint("research")
    )
    if track:
        from theodosia import tracker

        builder = builder.with_tracker(tracker(project="comparison-roles"))
    return builder


# ── langgraph_report / burr_report: one process, roles are nodes ──────
def langgraph_report(notes: str, draft: str) -> dict[str, Any]:
    class S(TypedDict, total=False):
        notes: str
        draft: str
        verdict: str

    def n_research(s: S) -> dict:
        return {"notes": notes}

    def n_write(s: S) -> dict:
        return {"draft": draft}

    def n_review(s: S) -> dict:
        return {"verdict": "approve"}

    g = StateGraph(S)
    g.add_node("researcher", n_research)
    g.add_node("writer", n_write)
    g.add_node("reviewer", n_review)
    g.add_edge(START, "researcher")
    g.add_edge("researcher", "writer")
    g.add_edge("writer", "reviewer")
    g.add_edge("reviewer", END)
    out = g.compile().invoke({})
    return {"draft": out["draft"], "verdict": out["verdict"]}


def burr_report(notes: str, draft: str) -> dict[str, Any]:
    app = build_report_app().build()
    app.step(inputs={"notes": notes})  # research
    app.step(inputs={"draft": draft})  # write
    app.step(inputs={"verdict": "approve"})  # review
    return {"draft": app.state["draft"], "verdict": app.state["verdict"]}


# ── roles_collaborate: three agents, three sessions, one machine ──────
async def _step(c: Client, act: str, inputs: dict | None = None) -> dict:
    args: dict[str, Any] = {"action": act}
    if inputs is not None:
        args["inputs"] = inputs
    r = await c.call_tool("step", args, raise_on_error=False)
    return r.structured_content


async def _next(c: Client) -> list:
    return json.loads((await c.read_resource("theodosia://next"))[0].text)


async def _state(c: Client) -> dict:
    return json.loads((await c.read_resource("theodosia://state"))[0].text)


def _ledger_actions(home: Path) -> list[str]:
    actions: list[str] = []
    for path in home.rglob("ledger.jsonl"):
        for line in path.read_text().splitlines():
            if line.strip():
                rec = json.loads(line)
                if rec.get("action") and not rec.get("refused"):
                    actions.append(rec["action"])
    return actions


async def _roles_collaborate() -> dict[str, Any]:
    home = Path(tempfile.gettempdir()) / "theodosia-comparison-roles"
    for p in home.rglob("ledger.jsonl") if home.exists() else []:
        p.unlink()
    os.environ["THEODOSIA_HOME"] = str(home)
    os.environ["THEODOSIA_QUIET"] = "1"

    server = mount(
        build_report_app(track=True).build(), name="report"
    )  # built = one shared machine
    trail: list[dict[str, Any]] = []

    async with Client(server) as eager_writer:  # a writer that connects too early
        early = await _step(eager_writer, "write", {"draft": "premature"})
        trail.append(
            {
                "agent": "writer",
                "tried": "write@start",
                "refused": early.get("error"),
                "phase_open": await _next(eager_writer),
            }
        )

    async with Client(server) as researcher:
        trail.append({"agent": "researcher", "phase": await _next(researcher)})
        await _step(researcher, "research", {"notes": "otters use rocks to crack shellfish"})

    async with Client(server) as writer:
        trail.append(
            {
                "agent": "writer",
                "phase": await _next(writer),
                "sees_notes": (await _state(writer))["notes"],
            }
        )
        await _step(writer, "write", {"draft": "Otters use tools."})

    async with Client(server) as reviewer:
        trail.append({"agent": "reviewer", "phase": await _next(reviewer)})
        await _step(reviewer, "review", {"verdict": "reject", "comments": "add detail"})
        trail.append({"agent": "reviewer", "verdict": "reject", "phase_now": await _next(reviewer)})

    async with Client(server) as writer2:  # the writer re-engages after the rejection
        await _step(
            writer2, "write", {"draft": "Otters use rocks as tools to crack open shellfish."}
        )

    async with Client(server) as reviewer2:
        final = await _step(reviewer2, "review", {"verdict": "approve", "comments": "lgtm"})

    return {
        "trail": trail,
        "final_stage": final["state"]["stage"],
        "revisions": final["state"]["revisions"],
        "ledger_actions": _ledger_actions(home),
    }


def roles_collaborate() -> dict[str, Any]:
    return asyncio.run(_roles_collaborate())


def main() -> None:
    print("one process: the three roles are just nodes one script runs.")
    print(f"    burr_report -> verdict={burr_report('notes', 'a draft')['verdict']}\n")

    print("one mounted machine, three role-agents in three sessions:")
    r = roles_collaborate()
    for t in r["trail"]:
        print(f"    {json.dumps(t)}")
    print(f"\n    final stage: {r['final_stage']}   revisions: {r['revisions']}")
    print(f"    one ledger, the whole collaboration: {r['ledger_actions']}")

    assert r["final_stage"] == "approved" and r["revisions"] == 2
    assert r["ledger_actions"] == ["research", "write", "review", "write", "review"]
    print("\n  each agent entered at its own phase; the machine kept the state across all")
    print("  of them, ran the review loop, and recorded every role in one ledger.")


if __name__ == "__main__":
    main()
