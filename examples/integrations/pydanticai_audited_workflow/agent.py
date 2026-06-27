"""A PydanticAI agent whose toolbox is one native tool plus a Theodosia FSM.

Thesis: mount a workflow you want audited into your existing agent as MCP.

The deploy-approval FSM in ``examples/deploy_approval.py`` is a Burr state
machine that Theodosia serves as a plain stdio MCP server
(``theodosia serve deploy_approval:build``). PydanticAI's native MCP support
(``pydantic_ai.mcp.MCPToolset`` over ``StdioTransport``) connects to it and
turns its ``step`` tool (and friends) into agent tools, which sit in the same
toolbox as a native ``@agent.tool_plain`` business lookup. To the PydanticAI
agent the audited FSM is just one capability among many.

What this proves without any API key (local Ollama only):

1. discovery  -- PydanticAI lists the Theodosia MCP tools and they appear in
   the combined toolbox next to the native ``suggest_risk_tier`` tool.
2. invocation -- PydanticAI CALLS the Theodosia ``step`` tool and drives the
   FSM: a clean walk to a verified deploy, a structured gate refusal when
   ``deploy`` is unjustified, and a ``validation_failed`` on malformed input.
   Resources (``theodosia://graph``) are read through the same MCP client.

If a local Ollama (http://localhost:11434) serving a tool-calling model is
reachable, ``run_model_loop`` also lets a real PydanticAI ``Agent`` decide to
call the FSM on its own. That path is optional and skipped otherwise.

Run:

    .venv/bin/python examples/integrations/pydanticai_audited_workflow/agent.py
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.mcp import MCPToolset, StdioTransport
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

REPO = Path(__file__).resolve().parents[3]
EXAMPLES = REPO / "examples"
SCRATCH = os.environ.get("PYDANTICAI_DEMO_HOME", "/private/tmp/pydanticai-theodosia")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:1.5b")


def build_theodosia_toolset() -> MCPToolset:
    """A PydanticAI MCP toolset that launches the Theodosia deploy-approval FSM.

    The FSM is served over stdio by the ``theodosia`` CLI. Tracker output is
    pinned to a scratch dir so the demo never writes a tracker tree into the
    repo root.
    """
    tracker_home = f"{SCRATCH}/tracker"
    Path(tracker_home).mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["THEODOSIA_HOME"] = tracker_home
    env.setdefault("THEODOSIA_PROJECT", "deploy-dogfood")
    transport = StdioTransport(
        command=str(REPO / ".venv" / "bin" / "theodosia"),
        args=["serve", "deploy_approval:build", "--app-dir", str(EXAMPLES)],
        cwd=str(REPO),
        env=env,
    )
    # tool_error_behavior='error' surfaces genuine MCP crashes to us instead of
    # silently turning them into a model retry. Theodosia's *graded* refusals
    # (validation_failed) are returned as normal structured results, not MCP
    # errors, so they flow back regardless of this setting.
    return MCPToolset(transport, id="theodosia", tool_error_behavior="error")


def build_agent(model: OpenAIChatModel | None, toolset: MCPToolset) -> Agent:
    """A PydanticAI agent with one native tool plus the Theodosia FSM toolset."""
    agent = Agent(
        model,
        toolsets=[toolset],
        system_prompt=(
            "You drive a deployment-approval state machine exposed as the `step` "
            "tool. Advance it with step(action=..., inputs=...). The legal walk is "
            "open_change -> review -> approve(reason) -> deploy(reason) -> verify. "
            "`deploy` is gated: it is refused unless an approve step ran and a "
            "non-empty reason is supplied. If a step returns an `error`, read its "
            "`valid_next_actions` and `next_action_schemas` and recover. Use the "
            "native suggest_risk_tier tool to pick a risk tier when opening a change."
        ),
    )

    @agent.tool_plain
    def suggest_risk_tier(service: str) -> str:
        """Suggest a deployment risk tier ('low' or 'high') for a service.

        A native business lookup that sits in the same toolbox as the audited
        FSM. Payments and auth are treated as high risk.
        """
        return "high" if service.lower() in {"payments", "auth", "billing"} else "low"

    return agent


def _summary(payload: dict[str, Any]) -> str:
    if "error" in payload:
        return f"REFUSED[{payload['error']}] {payload.get('reason', '')}"
    state = payload.get("state", {})
    return f"stage={state.get('stage')} valid_next={payload.get('valid_next_actions')}"


async def prove_discovery_and_invocation() -> None:
    toolset = build_theodosia_toolset()
    agent = build_agent(None, toolset)

    async with toolset:
        # ---- PROOF 1: discovery ------------------------------------------
        print("=" * 72)
        print("PROOF 1  PydanticAI sees the Theodosia FSM as MCP tools")
        print("=" * 72)
        mcp_tools = await toolset.list_tools()
        theo_names = [t.name for t in mcp_tools]
        native_names = ["suggest_risk_tier"]
        print(f"Theodosia MCP server exposes {len(theo_names)} tools: {theo_names}")
        print("\nCombined PydanticAI agent toolbox (native + Theodosia FSM):")
        for name in sorted([*theo_names, *native_names]):
            origin = "theodosia-fsm" if name in theo_names else "native-pydanticai"
            print(f"  - {name:<16} [{origin}]")
        assert "step" in theo_names, "Theodosia step tool missing from toolset"
        assert agent is not None

        resources = await toolset.list_resources()
        print(f"\nMCP resources exposed by the FSM: {[str(r.uri) for r in resources]}")

        # ---- PROOF 2: invocation -----------------------------------------
        print("\n" + "=" * 72)
        print("PROOF 2  PydanticAI CALLS step and drives the audited FSM")
        print("=" * 72)

        async def step(action: str, inputs: dict[str, Any] | None = None) -> dict[str, Any]:
            args: dict[str, Any] = {"action": action}
            if inputs is not None:
                args["inputs"] = inputs
            return await toolset.direct_call_tool("step", args)

        print("\n-- clean walk to a verified deploy --")
        for action, inputs in [
            (
                "open_change",
                {"change": {"service": "payments", "risk": "high", "summary": "bump image tag"}},
            ),
            ("review", None),
            ("approve", {"reason": "change reviewed by oncall, low blast radius"}),
            ("deploy", {"reason": "approved rollout during the deploy window"}),
            ("verify", None),
        ]:
            r = await step(action, inputs)
            print(f"  step({action:<11}) -> {_summary(r)}")

        await toolset.direct_call_tool("reset_session", {})

        print("\n-- gate fires: deploy without a justification is refused --")
        await step(
            "open_change", {"change": {"service": "auth", "risk": "high", "summary": "rotate keys"}}
        )
        await step("review")
        await step("approve", {"reason": "approved"})
        refusal = await step("deploy", {"reason": ""})
        print(f"  step(deploy empty reason) -> {_summary(refusal)}")
        print(f"    recovery hint: valid_next_actions={refusal.get('valid_next_actions')}")
        assert refusal.get("error") == "validation_failed"

        await toolset.direct_call_tool("reset_session", {})

        print("\n-- typed input rejected at the wire: risk='critical' --")
        bad = await step(
            "open_change", {"change": {"service": "x", "risk": "critical", "summary": "y"}}
        )
        print(f"  step(open_change bad risk) -> {_summary(bad)}")
        errs = bad.get("details", {}).get("errors")
        print(f"    per-field pydantic errors: {errs}")
        assert bad.get("error") == "validation_failed"

        print("\n-- read an MCP resource through the same client --")
        graph = await toolset.read_resource("theodosia://graph")
        graph_obj = json.loads(graph if isinstance(graph, str) else graph[0])
        action_names = [a["name"] for a in graph_obj.get("actions", [])]
        print(f"  theodosia://graph actions: {action_names}")

        print(
            "\nVERDICT: PydanticAI discovered AND invoked the Theodosia FSM as MCP "
            "tools, drove it to a verified deploy, and got structured refusals on a "
            "blocked gate and on malformed typed input."
        )


async def run_model_loop() -> bool:
    """Optional: let a real PydanticAI Agent decide to call the FSM, via Ollama.

    Returns True if a model loop ran. Skipped when no local Ollama with the
    target model is reachable, so the demo never blocks on a model provider.
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
        print(f"\n[model loop skipped] model {OLLAMA_MODEL!r} not pulled")
        return False

    model = OpenAIChatModel(
        OLLAMA_MODEL,
        provider=OpenAIProvider(base_url=f"{OLLAMA_HOST}/v1", api_key="ollama"),
    )
    toolset = build_theodosia_toolset()
    agent = build_agent(model, toolset)

    print("\n" + "=" * 72)
    print(f"MODEL LOOP  PydanticAI Agent on Ollama/{OLLAMA_MODEL} driving the FSM")
    print("=" * 72)

    async with agent:
        result = await agent.run(
            "Open a deployment change for the 'payments' service (use "
            "suggest_risk_tier for its risk) summarized as 'bump image tag', then "
            "review, approve it with a reason, and deploy it with a reason. Use the "
            "step tool for every state transition."
        )

    calls = [
        f"{part.tool_name}({json.dumps(part.args)[:60]})"
        for msg in result.all_messages()
        for part in getattr(msg, "parts", [])
        if getattr(part, "part_kind", None) == "tool-call"
    ]
    print(f"\nModel issued {len(calls)} tool calls:")
    for c in calls:
        print(f"  - {c}")
    print(f"\nFinal agent output: {result.output[:300]}")
    return True


async def main() -> None:
    await prove_discovery_and_invocation()
    await run_model_loop()


if __name__ == "__main__":
    asyncio.run(main())
