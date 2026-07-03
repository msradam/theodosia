"""An eighth comparison: a code assistant that self-corrects from server refusals.

Ported from LangGraph's code-assistant tutorial:
    https://github.com/langchain-ai/langgraph/blob/23961cff61a42b52525f3b20b4094d8d2fba1744/docs/docs/tutorials/code_assistant/langgraph_code_assistant.ipynb

There a model emits a structured code object, `check_code` runs `exec(imports)`
then `exec(code)` in-process (no sandbox), and on error the graph reflects and
regenerates. Two things to move server-side: the gate on what may be executed,
and the structured refusal the driver corrects from.

Fidelity: `langgraph_code` keeps the source node names (generate, check_code) but
drops `reflect` and the `decide_to_finish` retry loop; `generate` is stubbed.
Burr <-> LangGraph: submit's gate <-> check_code (the compile/forbidden check),
run <-> check_code's exec. The tutorial's reflect->generate loop becomes the
server refusing a bad draft and the agent resubmitting.

This is the example that shows the *self-correction loop* the other renderings
only hint at. Mounted, a bad submission is refused with `validation_failed` and
the exact error; the FSM does not advance, so the agent revises and resubmits
until it passes. The ledger records the failed attempt and the fix. Any MCP client
gets that contract for free.

  langgraph_code / burr_code   run whatever code was generated, ungated.
  theodosia_code               the submission is checked server-side; a syntax
                               error or a forbidden op is refused with the error,
                               and the agent self-corrects.

Run:  ../../.venv/bin/python 10_code_assistant.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, TypedDict

from burr.core import ApplicationBuilder, State, action
from burr.core.action import Condition
from fastmcp import Client
from langgraph.graph import END, START, StateGraph

sys.path.insert(0, os.path.dirname(__file__))
from theodosia import ValidationFailed, mount

_FORBIDDEN = ("import", "exec(", "eval(", "open(", "__", "os.", "sys.", "subprocess")


def check_code(code: str) -> str | None:
    """Return an error string if the code must not run, else None."""
    if any(tok in code for tok in _FORBIDDEN):
        return "forbidden operation (imports / exec / file or process access are not allowed)"
    try:
        compile(code, "<submitted>", "exec")
    except SyntaxError as exc:
        return f"SyntaxError: {exc.msg}"
    if "def solve" not in code:
        return "must define a function named solve()"
    return None


def run_code(code: str) -> Any:
    """Execute validated code in a restricted namespace and call solve().

    The exec is the point of this example: it stands in for the tutorial's
    ``check_code`` running model-generated Python. It runs with no builtins, and
    under Theodosia only after the `submit` gate passed. Flagged intentionally.
    """
    ns: dict[str, Any] = {}
    exec(compile(code, "<submitted>", "exec"), {"__builtins__": {}}, ns)  # nosec B102
    return ns["solve"]()


# ── the Burr machine: submit -> run (submit is gated) ─────────────────
@action(reads=[], writes=["stage", "code"])
def submit(state: State, code: str) -> State:
    return state.update(stage="checked", code=code)


@action(reads=["code"], writes=["stage", "result"])
def run(state: State) -> State:
    return state.update(stage="ran", result=run_code(state["code"]))


def _submit_gate(state: dict, inputs: dict) -> dict | None:
    err = check_code(str(inputs.get("code") or ""))
    if err:
        raise ValidationFailed(f"code refused: {err}", details={"error": err})
    return None


submit._theodosia_validator = _submit_gate  # type: ignore[attr-defined]


def build_code_app() -> ApplicationBuilder:
    return (
        ApplicationBuilder()
        .with_actions(submit=submit, run=run)
        .with_transitions(("submit", "run", Condition.expr("stage == 'checked'")))
        .with_state(stage="new", code="", result=None)
        .with_entrypoint("submit")
    )


# ── langgraph_code / burr_code: run whatever was generated ────────────
def langgraph_code(code: str) -> dict[str, Any]:
    class S(TypedDict, total=False):
        code: str
        result: Any

    def generate(s: S) -> dict:
        return {"code": code}

    def check_code(s: S) -> dict:
        return {"result": run_code(s["code"])}  # the tutorial's check_code runs exec(), no sandbox

    g = StateGraph(S)
    g.add_node("generate", generate)
    g.add_node("check_code", check_code)
    g.add_edge(START, "generate")
    g.add_edge("generate", "check_code")
    g.add_edge("check_code", END)
    return {"result": g.compile().invoke({})["result"]}


def burr_code(code: str) -> dict[str, Any]:
    app = build_code_app().build()
    app.step(inputs={"code": code})  # submit (raw Burr ignores the gate)
    app.step()  # run
    return {"result": app.state["result"]}


# ── theodosia_code: the agent self-corrects from the refusal ──────────
async def _step(c: Client, act: str, inputs: dict | None = None) -> dict:
    args: dict[str, Any] = {"action": act}
    if inputs is not None:
        args["inputs"] = inputs
    r = await c.call_tool("step", args, raise_on_error=False)
    return r.structured_content


async def _res(c: Client, uri: str) -> Any:
    return json.loads((await c.read_resource(uri))[0].text)


async def _theodosia_code(attempts: list[str]) -> dict[str, Any]:
    server = mount(build_code_app, name="coder")
    transcript: list[dict[str, Any]] = []
    async with Client(server) as c:
        for code in attempts:  # the agent's successive drafts
            r = await _step(c, "submit", {"code": code})
            if r.get("error"):
                transcript.append(
                    {"submit": code, "refused": r["error"], "why": r["details"]["error"]}
                )
                continue  # FSM did not advance; revise and resubmit
            transcript.append(
                {"submit": code, "accepted": True, "next": await _res(c, "theodosia://next")}
            )
            final = await _step(c, "run")
            history = await _res(c, "theodosia://history")
            return {
                "transcript": transcript,
                "result": final["state"]["result"],
                "ledger_entries": len(history),
                "refused_in_ledger": len([h for h in history if h.get("refused")]),
            }
    return {"transcript": transcript, "result": None, "ledger_entries": 0, "refused_in_ledger": 0}


def theodosia_code(attempts: list[str]) -> dict[str, Any]:
    return asyncio.run(_theodosia_code(attempts))


def main() -> None:
    good = "def solve():\n    return 6 * 7"
    buggy = "def solve(\n    return 6 * 7"  # syntax error
    dangerous = "import os\ndef solve():\n    return os.getcwd()"  # forbidden op

    print("orchestrator runs whatever the model generated (here, good code):")
    print(f"    langgraph_code -> {langgraph_code(good)['result']}")
    print("  a buggy or unsafe draft just crashes; recovering needs bespoke in-graph retry.")

    print("\nmounted, the agent submits, is refused with the exact error, and self-corrects:")
    th = theodosia_code([buggy, dangerous, good])  # three drafts; only the last passes
    for t in th["transcript"]:
        print(f"    {json.dumps(t)}")
    print(f"    result after self-correction: {th['result']}")
    print(f"    ledger: {th['ledger_entries']} entries, {th['refused_in_ledger']} refused drafts")

    assert th["result"] == 42
    assert th["refused_in_ledger"] == 2  # the syntax error and the forbidden import
    print("\n  the driver needed no retry logic: the server returned each error as data,")
    print("  and every draft, rejected or accepted, is on the ledger.")


if __name__ == "__main__":
    main()
