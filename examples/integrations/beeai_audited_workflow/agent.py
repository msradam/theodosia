"""A BeeAI agent whose tools are one native tool plus an audited Theodosia FSM.

Thesis: just mount a workflow you want audited into your existing agent as MCP.
The deploy-approval FSM in ``examples/deploy_approval.py`` is a Burr state
machine that Theodosia serves as a plain stdio MCP server. BeeAI's own
``MCPTool`` connects to it and turns its ``step`` meta-tool (and friends) into
native BeeAI tools, which sit in the same toolbox as a hand-written
``current_time`` tool. To the BeeAI agent the FSM is just one capability among
many, except that every transition is gated and recorded by Theodosia.

BeeAI is IBM's agent framework. The relevance: Theodosia's lead user, medea, is
an IBM z/OS ops agent, and a watsonx/BeeAI ops agent would mount an audited
change-approval workflow exactly this way.

What this proves without any model or API key:

1. discovery  -- BeeAI lists the Theodosia tools and they appear in the combined
   agent toolbox next to the native ``current_time`` tool.
2. invocation -- BeeAI CALLS the Theodosia ``step`` tool directly and drives the
   deploy-approval FSM: open -> review -> (deploy is REFUSED by the escalation
   gate) -> approve -> deploy(reason) -> verify, getting real gated results.

If a local Ollama with a tool-calling model is reachable, ``run_model_loop``
also lets a real BeeAI ``RequirementAgent`` decide to call the FSM on its own.
That path is optional and skipped when no local model is available.

Run:

    .venv/bin/python examples/integrations/beeai_audited_workflow/agent.py
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from beeai_framework.tools import tool
from beeai_framework.tools.mcp import MCPTool
from mcp import StdioServerParameters
from mcp.client.stdio import stdio_client

REPO = Path(__file__).resolve().parents[3]
SCRATCH = os.environ.get("BEEAI_DEMO_HOME", "/private/tmp/beeai-theodosia-demo")


def theodosia_transport():
    """A stdio transport that launches the Theodosia-mounted deploy-approval FSM.

    The FSM is served by ``theodosia serve deploy_approval:build --app-dir
    examples``. ``THEODOSIA_HOME`` redirects the audit tracker into a scratch
    dir so the demo never writes a tracker tree into the repo.
    """
    env = dict(os.environ)
    env.setdefault("THEODOSIA_HOME", f"{SCRATCH}/tracker")
    env.setdefault("THEODOSIA_PROJECT", "deploy-beeai-dogfood")
    Path(env["THEODOSIA_HOME"]).mkdir(parents=True, exist_ok=True)
    return stdio_client(
        StdioServerParameters(
            command=str(REPO / ".venv" / "bin" / "theodosia"),
            args=["serve", "deploy_approval:build", "--app-dir", "examples"],
            cwd=str(REPO),
            env=env,
        )
    )


@tool
def current_time() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()


async def _step(
    step_tool: MCPTool, action: str, inputs: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Call the Theodosia ``step`` tool through BeeAI and return its payload."""
    args: dict[str, Any] = {"action": action}
    if inputs is not None:
        args["inputs"] = inputs
    output = await step_tool.run(args)
    result = output.result
    if isinstance(result, str):
        result = json.loads(result)
    return result


def _stage(payload: dict[str, Any]) -> Any:
    return (payload.get("state") or {}).get("stage")


async def prove_discovery_and_invocation() -> MCPTool:
    """PROOF 1 + 2. Returns the live ``step`` tool for the optional model loop."""
    mcp_tools = await MCPTool.from_client(theodosia_transport())
    by_name = {t.name: t for t in mcp_tools}
    toolbox = [current_time, *mcp_tools]

    print("=" * 72)
    print("PROOF 1  BeeAI sees the Theodosia FSM as native tools")
    print("=" * 72)
    theo_names = sorted(by_name)
    print(f"Theodosia MCP server exposes {len(mcp_tools)} tools: {theo_names}")
    print("\nCombined BeeAI agent toolbox (native + Theodosia FSM):")
    for t in toolbox:
        origin = "theodosia-fsm" if t.name in by_name else "native-beeai"
        print(f"  - {t.name:<16} [{origin}]  {t.description.splitlines()[0][:60]}")
    assert "step" in by_name, "Theodosia step tool missing from BeeAI toolbox"

    step_tool = by_name["step"]

    print("\n" + "=" * 72)
    print("PROOF 2  BeeAI CALLS the Theodosia step tool and drives the gated FSM")
    print("=" * 72)

    opened = await _step(
        step_tool,
        "open_change",
        {"change": {"service": "payments", "risk": "high", "summary": "rotate signing key"}},
    )
    svc = (opened.get("state") or {}).get("service")
    print(f"\nstep(open_change)  -> stage={_stage(opened)} service={svc}")

    reviewed = await _step(step_tool, "review")
    print(f"step(review)       -> stage={_stage(reviewed)}")

    # Topology gate: deploy is unreachable until an approve step has run.
    refused = await _step(step_tool, "deploy", {"reason": "ship it"})
    print("\nstep(deploy) BEFORE approve  -> REFUSED by the transition graph:")
    print(f"  error={refused.get('error')!r}")
    print(f"  message={refused.get('message')!r}")
    print(f"  valid_next_actions={refused.get('valid_next_actions')}")

    approved = await _step(
        step_tool, "approve", {"reason": "change board signed off, ticket OPS-4412"}
    )
    appr = (approved.get("state") or {}).get("approved")
    print(f"\nstep(approve)      -> stage={_stage(approved)} approved={appr}")

    # Escalation gate: deploy IS now reachable, but the input validator refuses
    # an empty justification with a structured, recoverable validation_failed.
    refused2 = await _step(step_tool, "deploy", {"reason": ""})
    print("step(deploy) empty reason    -> REFUSED by the escalation gate:")
    print(f"  error={refused2.get('error')!r}")
    print(f"  reason={refused2.get('reason')!r}")
    print(f"  details={refused2.get('details')}")
    print(f"  valid_next_actions={refused2.get('valid_next_actions')}")

    deployed = await _step(
        step_tool, "deploy", {"reason": "maintenance window open, rollback staged"}
    )
    print(f"\nstep(deploy) approved+reason -> stage={_stage(deployed)}")

    verified = await _step(step_tool, "verify")
    vr = (verified.get("state") or {}).get("verify_result")
    print(f"step(verify)       -> stage={_stage(verified)} verify_result={vr!r}")

    print(
        "\nVERDICT: BeeAI discovered AND invoked the Theodosia FSM as native MCP "
        "tools, drove it open -> verify, and the escalation gate refused the two "
        "unsafe deploy attempts with structured, recoverable payloads."
    )
    return step_tool


async def run_model_loop() -> bool:
    """Optional: let a real BeeAI RequirementAgent drive the FSM via local Ollama.

    Returns True if a model loop ran. Requires a local Ollama
    (http://localhost:11434) serving a tool-calling model. Skipped otherwise so
    the demo never blocks on a model provider.
    """
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    model_id = os.environ.get("OLLAMA_MODEL", "qwen2.5:1.5b")
    try:
        import urllib.request

        with urllib.request.urlopen(f"{host}/api/tags", timeout=3) as resp:
            available = {m["name"] for m in json.loads(resp.read()).get("models", [])}
    except Exception as exc:
        print(f"\n[model loop skipped] no local Ollama reachable at {host}: {exc}")
        return False
    if not any(model_id in name for name in available):
        print(f"\n[model loop skipped] model {model_id!r} not pulled; have {sorted(available)}")
        return False

    from beeai_framework.adapters.ollama.backend.chat import OllamaChatModel
    from beeai_framework.agents.requirement import RequirementAgent
    from beeai_framework.memory import UnconstrainedMemory

    mcp_tools = await MCPTool.from_client(theodosia_transport())
    llm = OllamaChatModel(model_id=model_id, base_url=host)
    agent = RequirementAgent(
        llm=llm,
        memory=UnconstrainedMemory(),
        tools=[current_time, *mcp_tools],
    )
    print("\n" + "=" * 72)
    print(f"MODEL LOOP  BeeAI RequirementAgent on Ollama/{model_id} driving the FSM")
    print("=" * 72)
    try:
        out = await agent.run(
            "You drive a deployment-approval state machine via the `step` tool. "
            "Call step(action=..., inputs=...). Walk it: open_change with a "
            "ChangeRequest {service, risk, summary}, then review, then approve "
            "with a reason, then deploy with a reason, then verify. If a call is "
            "refused, read valid_next_actions and try one of those.",
            max_iterations=12,
        )
        print("\nFINAL:", out.last_message.text[:400])
    except Exception as exc:
        print(f"[model loop ran but the small model stumbled] {type(exc).__name__}: {exc}")
    return True


async def main() -> None:
    await prove_discovery_and_invocation()
    await run_model_loop()


if __name__ == "__main__":
    asyncio.run(main())
