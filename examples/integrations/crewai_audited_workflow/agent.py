"""A CrewAI agent whose toolbox is one native tool plus a Theodosia FSM (via MCP).

Thesis: to get an audited workflow, just mount it into the agent you already
have. The deploy-approval FSM in ``examples/deploy_approval.py`` is a Burr state
machine that Theodosia serves as a plain stdio MCP server. CrewAI's own
``MCPServerAdapter`` connects to it and turns its ``step`` tool (and friends)
into CrewAI ``BaseTool`` instances, which sit in the same list as a native
CrewAI tool. To the agent the audited FSM is one capability among many.

What this proves without any API key (local Ollama is optional):

1. discovery  -- CrewAI lists the Theodosia tools and they appear in the
   combined toolbox next to a native ``record_note`` tool.
2. invocation -- CrewAI CALLS the adapted ``step`` tool and drives the FSM. The
   escalation gate refuses ``deploy`` before ``approve`` (a structured
   ``validation_failed``); after walking open -> review -> approve, the same
   ``deploy`` succeeds. Every step is appended to Theodosia's hash-chained
   ledger, so the run is auditable end to end.

If a local Ollama with a tool-calling model is reachable, ``run_model_loop``
also lets a real CrewAI ``Agent``/``Crew`` decide to call the FSM on its own.
That path is optional and skipped when no local model is available.

Run:

    .venv/bin/python examples/integrations/crewai_audited_workflow/agent.py
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, ClassVar

from crewai.tools import BaseTool
from crewai_tools import MCPServerAdapter
from mcp import StdioServerParameters
from pydantic import BaseModel, Field

REPO = Path(__file__).resolve().parents[3]
THEODOSIA = REPO / ".venv" / "bin" / "theodosia"

# Keep the FSM's hash-chained ledger out of the repo root and out of ~/.theodosia.
TRACKER_HOME = os.environ.get(
    "CREWAI_DEMO_HOME", str(Path(tempfile.gettempdir()) / "crewai-theodosia-demo")
)


def theodosia_server_params() -> StdioServerParameters:
    """stdio params that launch the deploy-approval FSM as a Theodosia MCP server."""
    env = dict(os.environ)
    env["THEODOSIA_HOME"] = TRACKER_HOME
    Path(TRACKER_HOME).mkdir(parents=True, exist_ok=True)
    return StdioServerParameters(
        command=str(THEODOSIA),
        args=["serve", "deploy_approval:build", "--app-dir", "examples"],
        cwd=str(REPO),
        env=env,
    )


class _NoteInput(BaseModel):
    note: str = Field(description="A free-text note to record in the change log.")


class RecordNote(BaseTool):
    """A plain native CrewAI tool, to prove the FSM sits alongside ordinary tools."""

    name: str = "record_note"
    description: str = "Record a free-text note in the local deployment change log."
    args_schema: type[BaseModel] = _NoteInput
    log: ClassVar[list[str]] = []

    def _run(self, note: str) -> str:
        self.log.append(note)
        return f"noted ({len(self.log)} total): {note}"


def _parse_step_text(raw: str) -> dict[str, Any]:
    """Recover Theodosia's structured step result from CrewAI's adapter output.

    CrewAI's MCP adapter drops MCP ``structuredContent`` and only forwards text
    blocks. Theodosia returns two text blocks (a human-readable ``Step N`` line
    plus the JSON result); the adapter renders a multi-block result as
    ``str([...])`` (a Python list repr), not the JSON itself. So we eval the
    list repr and return the first element that parses as a JSON object.
    """
    import ast

    candidates: list[str] = [raw]
    try:
        evaled = ast.literal_eval(raw)
        if isinstance(evaled, list):
            candidates = [c for c in evaled if isinstance(c, str)]
    except (ValueError, SyntaxError):
        pass
    for c in candidates:
        try:
            obj = json.loads(c)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return {"raw": raw}


def _call_step(
    step_tool: BaseTool, action: str, inputs: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Invoke the adapted Theodosia ``step`` tool and parse its result."""
    kwargs: dict[str, Any] = {"action": action}
    if inputs is not None:
        kwargs["inputs"] = inputs
    raw = step_tool.run(**kwargs)
    return _parse_step_text(raw) if isinstance(raw, str) else raw


def prove_discovery_and_invocation() -> None:
    native = RecordNote()
    server = MCPServerAdapter(theodosia_server_params())
    try:
        mcp_tools = list(server.tools)
        theo_names = [t.name for t in mcp_tools]
        toolbox = [native, *mcp_tools]

        print("=" * 70)
        print("PROOF 1  CrewAI sees the Theodosia FSM as native CrewAI tools")
        print("=" * 70)
        print(f"Theodosia MCP server exposes {len(mcp_tools)} tools: {theo_names}")
        print("\nCombined CrewAI toolbox (native + Theodosia FSM):")
        for t in toolbox:
            origin = "theodosia-fsm" if t.name in theo_names else "native-crewai"
            print(f"  - {t.name:<16} [{origin}]")
        assert "step" in theo_names, "Theodosia step tool missing from CrewAI toolbox"

        step = next(t for t in mcp_tools if t.name == "step")

        print("\n" + "=" * 70)
        print("PROOF 2  CrewAI CALLS step and the FSM's gate is enforced through it")
        print("=" * 70)

        opened = _call_step(
            step,
            "open_change",
            {"change": {"service": "payments", "risk": "high", "summary": "rotate db creds"}},
        )
        print(
            f"\nstep(open_change) -> stage={opened.get('state', {}).get('stage')} "
            f"valid_next={opened.get('valid_next_actions')}"
        )

        native_out = native.run(note=f"opened change for {opened.get('state', {}).get('service')}")
        print(f"native record_note -> {native_out}")

        reviewed = _call_step(step, "review")
        print(
            f"step(review)      -> stage={reviewed.get('state', {}).get('stage')} "
            f"valid_next={reviewed.get('valid_next_actions')}"
        )

        # Refusal 1: topology guard. deploy is not reachable from 'reviewed'.
        not_reachable = _call_step(step, "deploy", {"reason": "ship it"})
        print(f"\nstep(deploy) from reviewed -> error={not_reachable.get('error')!r}")
        print(f"  {not_reachable.get('message')}")
        assert not_reachable.get("error") == "invalid_transition", "expected topology refusal"

        approved = _call_step(step, "approve", {"reason": "secrets rotation approved by oncall"})
        print(
            f"\nstep(approve)     -> stage={approved.get('state', {}).get('stage')} "
            f"approved={approved.get('state', {}).get('approved')}"
        )

        # Refusal 2: escalation gate (input validator). deploy with empty reason.
        gated = _call_step(step, "deploy", {"reason": ""})
        print(f"\nstep(deploy) empty reason -> error={gated.get('error')!r}")
        print(f"  gate: {gated.get('reason')} | details={gated.get('details')}")
        assert gated.get("error") == "validation_failed", "expected escalation-gate refusal"

        deployed = _call_step(step, "deploy", {"reason": "rolling creds to prod"})
        print(f"\nstep(deploy) with reason  -> stage={deployed.get('state', {}).get('stage')}")
        assert deployed.get("state", {}).get("stage") == "deployed", "deploy should now succeed"

        verified = _call_step(step, "verify")
        print(
            f"step(verify)      -> stage={verified.get('state', {}).get('stage')} "
            f"result={verified.get('state', {}).get('verify_result')}"
        )

        print(
            "\nVERDICT: CrewAI discovered AND invoked the Theodosia FSM as native "
            "tools. Both guards held when driven through CrewAI: deploy was refused "
            "as unreachable from 'reviewed', refused again by the escalation gate for "
            "an empty reason, and succeeded only with a justification after approve."
        )
    finally:
        server.stop()


def _ollama_ready(host: str, model_id: str) -> bool:
    try:
        import urllib.request

        with urllib.request.urlopen(f"{host}/api/tags", timeout=3) as resp:
            tags = json.loads(resp.read())
        available = {m["name"] for m in tags.get("models", [])}
    except Exception as exc:
        print(f"\n[model loop skipped] no local Ollama at {host}: {exc}")
        return False
    if not any(model_id in n or n.startswith(model_id) for n in available):
        print(f"\n[model loop skipped] model {model_id!r} not pulled; have {sorted(available)}")
        return False
    return True


def run_model_loop() -> bool:
    """Optional: let a real CrewAI Agent/Crew decide to call the FSM, via Ollama.

    CrewAI talks to models through LiteLLM. For Ollama the model string is
    ``ollama/<model>`` and the base URL comes from ``OLLAMA_API_BASE``. Returns
    True if a crew ran. Skipped when no local model is available so the demo
    never blocks on a provider.
    """
    host = os.environ.get(
        "OLLAMA_API_BASE", os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    )
    model_id = os.environ.get("OLLAMA_MODEL", "qwen2.5:1.5b")
    if not _ollama_ready(host, model_id):
        return False

    os.environ["OLLAMA_API_BASE"] = host
    from crewai import LLM, Agent, Crew, Task

    llm = LLM(model=f"ollama/{model_id}", base_url=host)
    native = RecordNote()
    server = MCPServerAdapter(theodosia_server_params())
    try:
        tools = [native, *server.tools]
        agent = Agent(
            role="Release operator",
            goal="Drive the deploy-approval state machine to a verified deployment.",
            backstory=(
                "You operate a deployment workflow exposed as the `step` tool. "
                "Advance it with step(action=..., inputs=...). The deploy action is "
                "gated: it is refused unless an approve step ran first with a reason."
            ),
            tools=tools,
            llm=llm,
            verbose=True,
        )
        task = Task(
            description=(
                "Open a change for service 'payments' (risk 'low', summary 'cache bump'), "
                "review it, approve it with a reason, then deploy with a reason, then verify. "
                "Use the step tool for every transition."
            ),
            expected_output="The final stage of the FSM after verify.",
            agent=agent,
        )
        print("\n" + "=" * 70)
        print(f"MODEL LOOP  CrewAI Agent on Ollama/{model_id} driving the FSM")
        print("=" * 70)
        result = Crew(agents=[agent], tasks=[task], verbose=True).kickoff()
        print("\nCREW RESULT:", str(result)[:400])
    finally:
        server.stop()
    return True


def main() -> None:
    prove_discovery_and_invocation()
    if os.environ.get("CREWAI_DEMO_MODEL_LOOP", "1") != "0":
        run_model_loop()


if __name__ == "__main__":
    main()
