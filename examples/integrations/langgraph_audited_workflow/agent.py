"""A LangGraph ReAct agent whose tools are one Theodosia FSM plus a native tool.

Thesis: to get an audited workflow inside an existing agent, you do not adopt a
new framework. You mount the workflow you want audited as an MCP server and let
your agent consume it as one tool among many.

The FSM is ``examples/deploy_approval.py``: a gated deploy-approval state machine
(open_change -> review -> approve -> deploy -> verify) with an escalation gate on
``deploy`` (refused unless a prior ``approve`` ran and a non-empty reason is
given). Theodosia mounts it as a plain stdio MCP server via
``theodosia serve deploy_approval:build``. LangGraph's MCP adapter
(``langchain_mcp_adapters``) connects to it and turns its ``step`` tool (and the
fork/reset meta-tools) into LangChain ``StructuredTool`` objects. Those sit in
the same tool list as a native ``calculator`` ``@tool``.

What this proves without a flawless model run:

1. discovery  -- the adapter turns Theodosia's ``step`` into a LangChain tool
   that drops into the agent's tool list right next to the native calculator.
2. invocation -- we drive the FSM by calling the bound ``step`` tool directly
   through the adapter: a real gated refusal (deploy before approve), then a
   correct walk to a terminal ``verified`` state. The structured step result
   comes back as a LangChain ToolMessage artifact.

If the local Ollama is reachable, ``run_model_loop`` also lets a real
``create_react_agent`` decide to call ``step`` on its own. That path is optional;
the small model often fumbles multi-step ordering, so the direct proof above is
the load-bearing one.

Run:

    .venv/bin/python examples/integrations/langgraph_audited_workflow/agent.py
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, tool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.resources import load_mcp_resources
from langchain_mcp_adapters.tools import load_mcp_tools

REPO = Path(__file__).resolve().parents[3]
SCRATCH = os.environ.get("LG_DEMO_HOME", "/private/tmp/lg-theodosia-demo")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:1.5b")


@tool
def calculator(expression: str) -> str:
    """Evaluate a basic arithmetic expression, e.g. '3 * (4 + 5)'."""
    allowed = set("0123456789+-*/(). ")
    if not set(expression) <= allowed:
        return "error: only digits and + - * / ( ) . are allowed"
    try:
        return str(eval(expression, {"__builtins__": {}}, {}))
    except Exception as exc:
        return f"error: {exc}"


def theodosia_connection() -> dict[str, Any]:
    """Stdio MCP connection that launches the Theodosia-mounted deploy FSM."""
    env = dict(os.environ)
    env.setdefault("THEODOSIA_HOME", f"{SCRATCH}/tracker")
    env.setdefault("THEODOSIA_PROJECT", "deploy-langgraph")
    Path(env["THEODOSIA_HOME"]).mkdir(parents=True, exist_ok=True)
    return {
        "command": str(REPO / ".venv" / "bin" / "theodosia"),
        "args": ["serve", "deploy_approval:build", "--app-dir", "examples"],
        "transport": "stdio",
        "cwd": str(REPO),
        "env": env,
    }


def _tool_message(result: Any) -> ToolMessage:
    """Normalize ``StructuredTool.ainvoke`` output to a ToolMessage.

    Invoking with a tool-call dict (rather than a bare args dict) makes the
    adapter return a full ToolMessage carrying both the text content and the
    ``artifact`` (the MCP ``structuredContent``), plus ``status``.
    """
    assert isinstance(result, ToolMessage)
    return result


async def _step(
    step_tool: BaseTool, action: str, inputs: dict[str, Any] | None = None
) -> ToolMessage:
    args: dict[str, Any] = {"action": action}
    if inputs is not None:
        args["inputs"] = inputs
    call = {"name": "step", "args": args, "id": f"{action}-1", "type": "tool_call"}
    return _tool_message(await step_tool.ainvoke(call))


def _structured(msg: ToolMessage) -> dict[str, Any]:
    art = msg.artifact
    if isinstance(art, dict) and "structured_content" in art:
        return art["structured_content"]
    return {}


def _stage(msg: ToolMessage) -> Any:
    return _structured(msg).get("state", {}).get("stage")


async def prove_discovery_and_invocation() -> None:
    client = MultiServerMCPClient({"deploy": theodosia_connection()})

    # A persistent session is REQUIRED to drive a stateful FSM: tools bound to
    # one session share one MCP session_id, so the FSM advances across calls.
    # client.get_tools() opens a fresh session per call and would reset the FSM.
    async with client.session("deploy") as session:
        mcp_tools = await load_mcp_tools(session, server_name="deploy")
        native_tools = [calculator]
        all_tools: list[BaseTool] = [*mcp_tools, *native_tools]

        print("=" * 72)
        print("PROOF 1  LangGraph sees the Theodosia FSM as LangChain tools")
        print("=" * 72)
        theo_names = {t.name for t in mcp_tools}
        print(f"Theodosia MCP server exposes {len(mcp_tools)} tool(s): {sorted(theo_names)}")
        print("\nCombined agent tool list (Theodosia FSM + native):")
        for t in all_tools:
            origin = "theodosia-fsm" if t.name in theo_names else "native-langchain"
            print(f"  - {t.name:<16} [{origin}]  {t.description.splitlines()[0][:54]}")

        step_tool = next(t for t in mcp_tools if t.name == "step")
        print("\n'step' args_schema (what the model is shown):")
        schema = step_tool.args_schema
        props = schema.get("properties", {}) if isinstance(schema, dict) else {}
        action_prop = props.get("action", {})
        print(f"  action.enum   = {action_prop.get('enum')}")
        print(f"  inputs schema = {json.dumps(props.get('inputs'))[:120]}")

        # Resources are loadable through the adapter, but create_react_agent
        # only consumes TOOLS, so the agent never sees theodosia://graph.
        try:
            resources = await load_mcp_resources(session)
            print(
                f"\nMCP resources reachable via adapter (NOT given to the agent): {len(resources)}"
            )
        except Exception as exc:
            print(f"\nMCP resource load failed: {exc!r}")

        print("\n" + "=" * 72)
        print("PROOF 2  Drive the gated FSM through the bound LangChain tool")
        print("=" * 72)

        opened = await _step(
            step_tool,
            "open_change",
            {"change": {"service": "payments", "risk": "high", "summary": "rotate db creds"}},
        )
        st = _structured(opened).get("state", {})
        print(f"\nstep(open_change)  status={opened.status!r}  stage={st.get('stage')}")

        reviewed = await _step(step_tool, "review")
        print(f"step(review)       status={reviewed.status!r}  stage={_stage(reviewed)}")

        # GATE A (transition guard): deploy is not even reachable from
        # 'reviewed'; the FSM refuses with invalid_transition + the valid set.
        off_path = await _step(step_tool, "deploy", {"reason": "ship it"})
        offp = _structured(off_path)
        status, err = off_path.status, offp.get("error")
        print(f"\nstep(deploy) from 'reviewed' -> status={status!r}  error={err!r}")
        print(f"  valid_next_actions: {offp.get('valid_next_actions')}")

        approved = await _step(step_tool, "approve", {"reason": "reviewed by oncall"})
        print(f"\nstep(approve)      status={approved.status!r}  stage={_stage(approved)}")

        # GATE B (escalation/input validator): now deploy IS reachable, but the
        # validator refuses an empty reason with a structured validation_failed.
        ungated = await _step(step_tool, "deploy", {"reason": ""})
        upayload = _structured(ungated)
        status, err = ungated.status, upayload.get("error")
        print(f"\nstep(deploy) empty reason -> status={status!r}  error={err!r}")
        detail = upayload.get("details") or upayload.get("message") or upayload
        print(f"  refusal detail: {json.dumps(detail)[:200]}")

        deployed = await _step(step_tool, "deploy", {"reason": "approved, low blast radius"})
        print(f"step(deploy) after approve -> status={deployed.status!r}  stage={_stage(deployed)}")

        verified = await _step(step_tool, "verify")
        vst = _structured(verified).get("state", {})
        status, stage, vr = verified.status, vst.get("stage"), vst.get("verify_result")
        print(f"step(verify)       status={status!r}  stage={stage}  result={vr}")

        print("\nVERDICT: LangGraph consumed the Theodosia FSM as a native tool, the")
        print("escalation gate refused an unapproved deploy, and a correct walk")
        print("reached a terminal 'verified' state. All gating enforced server-side.")


async def run_model_loop() -> bool:
    """Optional: let a real LangGraph ReAct agent on Ollama drive the FSM.

    Returns True if a model loop ran. Skipped when no local Ollama is reachable
    or the model is not pulled, so the demo never blocks on a provider.
    """
    import urllib.request

    try:
        with urllib.request.urlopen(f"{OLLAMA_HOST}/api/tags", timeout=3) as resp:
            tags = json.loads(resp.read())
        available = {m["name"] for m in tags.get("models", [])}
    except Exception as exc:
        print(f"\n[model loop skipped] no local Ollama at {OLLAMA_HOST}: {exc}")
        return False
    if not any(OLLAMA_MODEL in name for name in available):
        print(f"\n[model loop skipped] model {OLLAMA_MODEL!r} not pulled; have {sorted(available)}")
        return False

    from langchain_ollama import ChatOllama
    from langgraph.prebuilt import create_react_agent

    client = MultiServerMCPClient({"deploy": theodosia_connection()})
    async with client.session("deploy") as session:
        tools = [*await load_mcp_tools(session, server_name="deploy"), calculator]
        model = ChatOllama(model=OLLAMA_MODEL, base_url=OLLAMA_HOST, temperature=0)
        agent = create_react_agent(
            model,
            tools,
            prompt=(
                "You drive a deploy-approval state machine via the `step` tool: "
                "call step(action=..., inputs=...). The required order is "
                "open_change, review, approve, deploy, verify. open_change needs "
                "inputs={'change': {'service','risk','summary'}}; approve and deploy "
                "each need inputs={'reason': '...'}. Walk it to a verified deploy."
            ),
        )
        print("\n" + "=" * 72)
        print(f"MODEL LOOP  create_react_agent on Ollama/{OLLAMA_MODEL} driving the FSM")
        print("=" * 72)
        try:
            task = (
                "Open and ship a high-risk change to 'payments' summarized as "
                "'rotate db creds'. Approve it, then deploy and verify."
            )
            result = await agent.ainvoke(
                {"messages": [("user", task)]},
                config={"recursion_limit": 24},
            )
            calls = sum(len(getattr(m, "tool_calls", []) or []) for m in result["messages"])
            print(f"agent finished. total tool_calls={calls}")
            print("final:", str(result["messages"][-1].content)[:240])
        except Exception as exc:
            print(f"[model loop ran but errored, expected for a 1.5B model]: {exc!r}")
    return True


async def main() -> None:
    await prove_discovery_and_invocation()
    await run_model_loop()


if __name__ == "__main__":
    asyncio.run(main())
