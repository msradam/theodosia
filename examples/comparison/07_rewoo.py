"""A fifth three-way: ReWOO as a stored-program machine the agent drives.

This is the Turing-machine framing made literal, ported from LangGraph's ReWOO
tutorial (Reasoning WithOut Observation):
    https://github.com/langchain-ai/langgraph/blob/23961cff61a42b52525f3b20b4094d8d2fba1744/docs/docs/tutorials/rewoo/rewoo.ipynb

ReWOO's own data model is a stored program: a planner emits a straight-line
program of typed steps `#En = TOOL[input]` parsed by the exact regex below; a
program counter (`_get_current_task`) walks it; a register file (`results[#En]`)
holds evidence; each step substitutes reads of prior registers and writes one new
register. The plan is the tape, `results` is the registers, the PC is the head.

The three renderings differ in who holds the machine:

  langgraph_rewoo   the tutorial's plan -> tool-loop -> solve graph. The runtime
                    runs the program; the tools (including a consequential one)
                    run with no gate but the prompt.
  burr_rewoo        the same, orchestrator style. Same exposure.
  theodosia_rewoo   the machine is mounted. The agent submits a program and
                    drives `execute` one instruction at a time. The server
                    validates the program against the instruction set, enforces
                    the program counter (you cannot `solve` early), gates the one
                    consequential opcode (`PUBLISH`), and records the whole tape.

The twist over faithful ReWOO (which is read-only and single-author, so mounting
would be merely illustrative) is one consequential opcode, `PUBLISH`, and an
untrusted program. Now mounting earns its keep: a malicious program is refused
and recorded server-side; the orchestrator runs it.

Run:  ../../.venv/bin/python 07_rewoo.py
      COMPARISON_LIVE=1 ../../.venv/bin/python 07_rewoo.py
"""

from __future__ import annotations

import asyncio
import json
import os
import re
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
from theodosia import ValidationFailed, mount

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent

# The instruction set. SEARCH and REASON are read-only; PUBLISH is consequential.
PUBLISHED: list[str] = []  # the output tape: values the machine actually published


def _search(q: str) -> str:
    return f"result({q})"


def _reason(t: str) -> str:
    return f"reasoned({t})"


def _publish(v: str) -> str:
    PUBLISHED.append(v)
    return f"published({v})"


ISA = {"SEARCH": _search, "REASON": _reason, "PUBLISH": _publish}

# ReWOO's exact plan-parsing regex: (description, #En, TOOL, input)
REGEX = r"Plan:\s*(.+)\s*(#E\d+)\s*=\s*(\w+)\s*\[([^\]]+)\]"


def parse_plan(program: str) -> list[tuple[str, str, str, str]]:
    return re.findall(REGEX, program)


def plan_for(task: str) -> str:
    """Stub for ReWOO's LLM planner: a straight-line program ending in a PUBLISH."""
    return (
        f"Plan: gather facts #E1 = SEARCH[{task}]\n"
        "Plan: reason over them #E2 = REASON[#E1]\n"
        "Plan: publish the finding #E3 = PUBLISH[#E2]"
    )


def substitute(tool_input: str, results: dict[str, str]) -> str:
    for name, value in results.items():
        tool_input = tool_input.replace(name, value)
    return tool_input


def run_op(tool: str, resolved_input: str) -> str:
    return ISA[tool](resolved_input)


def fuse(results: dict[str, str]) -> str:
    return results.get(max(results, default=""), "")  # last register written


# ── langgraph_rewoo: the tutorial's plan -> tool -> solve, faithful ───
class ReWOO(TypedDict, total=False):
    task: str
    steps: list
    results: dict
    answer: str


def langgraph_rewoo(task: str) -> dict[str, Any]:
    def get_plan(s: ReWOO) -> dict:
        return {"steps": parse_plan(plan_for(s["task"])), "results": {}}

    def tool_execution(s: ReWOO) -> dict:
        """Worker node that executes the tools of a given plan."""
        results = dict(s["results"])
        _desc, name, tool, inp = s["steps"][len(results)]  # _get_current_task: PC = len(results)
        results[name] = run_op(tool, substitute(inp, results))
        return {"results": results}

    def _route(s: ReWOO) -> str:
        return "solve" if len(s["results"]) == len(s["steps"]) else "tool"

    def solve(s: ReWOO) -> dict:
        return {"answer": fuse(s["results"])}

    g = StateGraph(ReWOO)
    g.add_node("plan", get_plan)
    g.add_node("tool", tool_execution)
    g.add_node("solve", solve)
    g.add_edge(START, "plan")
    g.add_edge("plan", "tool")
    g.add_conditional_edges("tool", _route, {"tool": "tool", "solve": "solve"})
    g.add_edge("solve", END)
    out = g.compile().invoke({"task": task})
    return {"answer": out["answer"]}


# ── the Burr stored-program machine (shared by 02 and 03) ─────────────
# Burr action  <->  LangGraph node:  plan <-> "plan" (get_plan);
#   execute <-> "tool" (tool_execution), driven one instruction per step;
#   solve <-> "solve".  The `_route` conditional edge becomes the pc<n/pc>=n transitions.
@action(reads=[], writes=["steps", "n", "results", "pc"])
def plan(state: State, program: str) -> State:
    steps = parse_plan(program)
    return state.update(steps=steps, n=len(steps), results={}, pc=0)


@action(reads=["steps", "pc", "results"], writes=["results", "pc"])
def execute(state: State) -> State:
    """Worker node that executes the tools of a given plan."""
    results = dict(state["results"])
    _desc, name, tool, inp = state["steps"][state["pc"]]  # the instruction at the PC
    results[name] = run_op(tool, substitute(inp, results))
    return state.update(results=results, pc=state["pc"] + 1)


@action(reads=["results"], writes=["answer"])
def solve(state: State) -> State:
    return state.update(answer=fuse(state["results"]))


def _plan_gate(state: dict, inputs: dict) -> dict | None:
    for _desc, _name, tool, _inp in parse_plan(inputs.get("program", "")):
        if tool not in ISA:
            raise ValidationFailed(
                f"illegal opcode {tool!r}", details={"opcode": tool, "isa": list(ISA)}
            )
    return None


def _execute_gate(state: dict, inputs: dict) -> dict | None:
    steps, pc, results = state["steps"], state["pc"], state["results"]
    if pc >= len(steps):
        return None
    _desc, _name, tool, inp = steps[pc]
    if tool == "PUBLISH":
        refs = re.findall(r"#E\d+", inp)
        if not refs or any(r not in results for r in refs):
            raise ValidationFailed(
                "PUBLISH refused: its argument must resolve to a computed register",
                details={"arg": inp, "computed": list(results)},
            )
    return None


plan._theodosia_validator = _plan_gate  # type: ignore[attr-defined]
execute._theodosia_validator = _execute_gate  # type: ignore[attr-defined]


def build_machine() -> ApplicationBuilder:
    return (
        ApplicationBuilder()
        .with_actions(plan=plan, execute=execute, solve=solve)
        .with_transitions(
            ("plan", "execute", Condition.expr("n > 0")),
            ("execute", "execute", Condition.expr("pc < n")),
            ("execute", "solve", Condition.expr("pc >= n")),
        )
        .with_state(steps=[], n=0, results={}, pc=0, answer="")
        .with_entrypoint("plan")
    )


# ── burr_rewoo: orchestrator drives the machine (no server gate) ──────
def burr_rewoo(program: str) -> dict[str, Any]:
    app = build_machine().build()
    app.step(inputs={"program": program})  # plan
    while app.step() is not None:  # execute*, solve
        pass
    return {"answer": dict(app.state.get_all())["answer"]}


# ── theodosia_rewoo: the agent drives the mounted machine ─────────────
async def _step(c: Client, act: str, inputs: dict | None = None) -> dict:
    args: dict[str, Any] = {"action": act}
    if inputs is not None:
        args["inputs"] = inputs
    r = await c.call_tool("step", args, raise_on_error=False)
    return r.structured_content


async def _res(c: Client, uri: str) -> Any:
    return json.loads((await c.read_resource(uri))[0].text)


async def _drive(c: Client, program: str) -> dict[str, Any]:
    """Submit a program and drive `execute` one instruction at a time to `solve`."""
    tape: list[dict[str, Any]] = []
    planned = await _step(c, "plan", {"program": program})
    if planned.get("error"):
        return {"refused_at": "plan", "error": planned["error"], "reason": planned.get("reason")}
    while (await _res(c, "theodosia://next")) == ["execute"]:
        r = await _step(c, "execute")
        if r.get("error"):
            return {
                "refused_at": "execute",
                "error": r["error"],
                "reason": r.get("reason"),
                "pc": r.get("state", {}).get("pc"),
                "tape": tape,
            }
        st = r["state"]
        tape.append({"pc": st["pc"], "results": st["results"]})
    final = await _step(c, "solve")
    return {"answer": final["state"]["answer"], "tape": tape}


async def _theodosia_rewoo(task: str) -> dict[str, Any]:
    server = mount(build_machine, name="rewoo")
    good = plan_for(task)
    malicious = (
        f"Plan: gather #E1 = SEARCH[{task}]\nPlan: exfiltrate #E2 = PUBLISH[all customer emails]"
    )

    async with Client(server) as c:  # the honest run
        early = await _step(c, "solve")  # solve before planning
        good_run = await _drive(c, good)

    async with Client(server) as c:  # an untrusted program, same server
        bad_run = await _drive(c, malicious)
        history = await _res(c, "theodosia://history")

    return {
        "solve_before_plan": early.get("error"),
        "good": good_run,
        "malicious": bad_run,
        "malicious_refused_in_ledger": len([h for h in history if h.get("refused")]),
    }


def theodosia_rewoo(task: str) -> dict[str, Any]:
    return asyncio.run(_theodosia_rewoo(task))


# ── live: a real Claude agent drives the tape head ────────────────────
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


async def _drive_live(task: str) -> dict[str, Any] | None:
    if not _claude_ready():
        return None
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        ToolUseBlock,
        query,
    )

    home = Path(tempfile.gettempdir()) / "theodosia-comparison-rewoo"
    os.environ["THEODOSIA_HOME"] = str(home)
    os.environ["THEODOSIA_QUIET"] = "1"
    home.mkdir(parents=True, exist_ok=True)

    launch = [
        str(REPO / ".venv" / "bin" / "theodosia"),
        "serve",
        "07_rewoo:build_machine",
        "--app-dir",
        str(HERE),
    ]
    options = ClaudeAgentOptions(
        mcp_servers={"rewoo": {"type": "stdio", "command": launch[0], "args": launch[1:]}},
        allowed_tools=["mcp__rewoo__*"],
        disallowed_tools=["Bash"],  # so the agent drives via `step`, not a simulated shell
        permission_mode="bypassPermissions",
        model=os.environ.get("COMPARISON_MODEL"),
        max_budget_usd=float(os.environ.get("COMPARISON_BUDGET", "2.0")),
        max_turns=25,
    )
    program = plan_for(task).replace("\n", " ")
    prompt = (
        "Drive a stored-program machine using ONLY the `mcp__rewoo__step` tool (load it via "
        "ToolSearch first if needed). Do not simulate; actually call the tool. Do exactly this:\n"
        f'1. step(action="plan", inputs={{"program": "{program}"}}).\n'
        '2. step(action="execute"), repeated, until only "solve" is legal.\n'
        '3. step(action="solve"), then report the final answer.'
    )
    steps: list[str] = []
    result: Any = None
    try:
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                steps.extend(
                    dict(b.input).get("action", "?")
                    for b in msg.content
                    if isinstance(b, ToolUseBlock) and b.name.endswith("step")
                )
            elif isinstance(msg, ResultMessage):
                result = msg
    except Exception as exc:  # e.g. the SDK raises on max_turns; report partial progress
        return {"agent_steps": steps, "cost_usd": None, "note": str(exc)[:80]}
    return {"agent_steps": steps, "cost_usd": getattr(result, "total_cost_usd", None)}


def drive_live(task: str) -> dict[str, Any] | None:
    return asyncio.run(_drive_live(task))


def main() -> None:
    task = "otter behavior"
    print(f"task: {task!r}\n")

    PUBLISHED.clear()
    lg = langgraph_rewoo(task)
    burr = burr_rewoo(plan_for(task))
    th = theodosia_rewoo(task)
    assert lg["answer"] == burr["answer"] == th["good"]["answer"]
    print(f"  all three compute: {lg['answer']}")
    tape = th["good"]["tape"]
    print(f"  theodosia: the agent drove {len(tape)} execute steps; PC + registers per step:")
    for row in tape:
        print(f"    pc={row['pc']}  registers={row['results']}")
    print(f"  solve before plan was refused: {th['solve_before_plan']!r}")

    print("\n  an untrusted program tries to PUBLISH un-derived data ('all customer emails'):")
    PUBLISHED.clear()
    burr_rewoo("Plan: g #E1 = SEARCH[x]\nPlan: leak #E2 = PUBLISH[all customer emails]")
    leaked_by_orchestrator = "all customer emails" in PUBLISHED
    print(f"    orchestrator ran it -> PUBLISHED leaked: {leaked_by_orchestrator}  ({PUBLISHED})")
    bad = th["malicious"]
    print(f"    theodosia refused at `{bad['refused_at']}` -> {bad['error']} ({bad.get('reason')})")
    print(
        f"    the refused attempt is on the ledger: {th['malicious_refused_in_ledger']} refusal(s)"
    )

    assert leaked_by_orchestrator and bad["error"] == "validation_failed"
    print("\n  the plan is the tape, results are the registers, the agent is the head;")
    print("  mounted, the server validates the program, enforces the PC, and gates PUBLISH.")

    if os.environ.get("COMPARISON_LIVE") == "1":
        print("\n[live] a real Claude agent drives the tape head...")
        live = drive_live(task)
        if live is not None:
            print(f"  agent steps: {live['agent_steps']}  cost_usd: {live['cost_usd']}")


if __name__ == "__main__":
    main()
