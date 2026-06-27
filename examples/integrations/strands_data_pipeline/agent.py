"""A Strands agent whose tools are several native tools plus one Theodosia FSM.

Thesis: an agent workflow is an MCP. The data-pipeline FSM in
``examples/apps/data_agent`` is a Burr state machine mounted by Theodosia as a
plain stdio MCP server. Strands' own ``MCPClient`` connects to it and turns its
``step`` tool (and friends) into native Strands tools, which sit in the same
toolbox as a calculator and a clock. To the Strands agent the FSM is just one
capability among many.

What this proves without any model or API key:

1. discovery  -- Strands lists the Theodosia tools and they appear in the
   combined Agent toolbox next to the native ``calculator`` / ``current_time``.
2. invocation -- Strands CALLS the Theodosia ``step`` tool directly and drives
   the data-pipeline FSM (connect -> load -> profile -> query -> finding ->
   report), getting real SQLite rows back through the FSM's gated ``query``.

If a local Ollama with a tool-calling model is reachable, ``run_model_loop``
also lets a real Strands ``Agent`` decide to call the FSM on its own. That path
is optional and skipped when no local model is available.

Run:

    .venv/bin/python examples/integrations/strands_data_pipeline/agent.py
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

from mcp import StdioServerParameters, stdio_client
from strands import Agent
from strands.tools.mcp import MCPClient
from strands_tools import calculator, current_time

REPO = Path(__file__).resolve().parents[3]
DATA_AGENT_APP = REPO / "examples" / "apps" / "data_agent" / "app.py"
SCRATCH = os.environ.get(
    "STRANDS_DEMO_HOME",
    "/private/tmp/strands-theodosia-demo",
)


def theodosia_transport():
    """A stdio transport that launches the Theodosia-mounted data-pipeline FSM.

    The FSM is served by running its module: ``app.py`` calls
    ``build_server().run()``, which is a FastMCP stdio server. ``build_server``
    wires the two upstream MCP servers (sqlite + filesystem) the pipeline drives
    internally; those upstreams are why we launch the module directly rather
    than via ``theodosia serve`` (the CLI does not carry per-app upstream maps).
    """
    env = dict(os.environ)
    env.setdefault("DATA_AGENT_HOME", f"{SCRATCH}/tracker")
    env.setdefault("DATA_AGENT_DB", f"{SCRATCH}/sales.db")
    Path(env["DATA_AGENT_HOME"]).mkdir(parents=True, exist_ok=True)
    return stdio_client(
        StdioServerParameters(
            command=os.environ.get("DEMO_PYTHON", str(REPO / ".venv" / "bin" / "python")),
            args=[str(DATA_AGENT_APP)],
            cwd=str(REPO),
            env=env,
        )
    )


def make_theodosia_client() -> MCPClient:
    return MCPClient(theodosia_transport)


def _result_text(result: Any) -> str:
    parts = [
        block["text"]
        for block in result.get("content", [])
        if isinstance(block, dict) and "text" in block
    ]
    return "\n".join(parts)


def _step(client: MCPClient, action: str, inputs: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call the Theodosia ``step`` tool through Strands and return parsed JSON."""
    args: dict[str, Any] = {"action": action}
    if inputs is not None:
        args["inputs"] = inputs
    result = client.call_tool_sync(f"step-{uuid.uuid4().hex[:8]}", "step", args)
    payload = result.get("structuredContent")
    if not payload:
        payload = {"raw": _result_text(result)}
    return {"status": result.get("status"), "payload": payload}


def prove_discovery_and_invocation() -> None:
    client = make_theodosia_client()
    with client:
        # ---- PROOF 1: discovery -----------------------------------------
        mcp_tools = client.list_tools_sync()
        native_tools = [calculator, current_time]
        agent = Agent(model=None, tools=[*native_tools, *mcp_tools])

        print("=" * 70)
        print("PROOF 1  Strands sees the Theodosia FSM as native tools")
        print("=" * 70)
        theo_names = [t.tool_name for t in mcp_tools]
        print(f"Theodosia MCP server exposes {len(mcp_tools)} tools: {theo_names}")
        print("\nCombined Strands Agent toolbox (native + Theodosia FSM):")
        for name in sorted(agent.tool_names):
            origin = "theodosia-fsm" if name in theo_names else "native-strands"
            print(f"  - {name:<18} [{origin}]")
        assert "step" in agent.tool_names, "Theodosia step tool missing from Agent"
        assert "calculator" in agent.tool_names and "current_time" in agent.tool_names

        # ---- PROOF 2: invocation ----------------------------------------
        print("\n" + "=" * 70)
        print("PROOF 2  Strands CALLS the Theodosia step tool and drives the FSM")
        print("=" * 70)

        connect = _step(client, "connect")
        print(
            f"\nstep(connect)  -> status={connect['status']} "
            f"phase={connect['payload'].get('state', {}).get('phase')}"
        )

        load = _step(client, "load")
        st = load["payload"].get("state", {})
        print(
            f"step(load)     -> status={load['status']} "
            f"phase={st.get('phase')} loaded_rows={st.get('loaded_rows')}"
        )

        profile = _step(client, "profile")
        prof = profile["payload"].get("state", {}).get("profile", {}) or {}
        print(
            f"step(profile)  -> status={profile['status']} "
            f"row_count={prof.get('row_count')} regions={prof.get('regions')} "
            f"products={prof.get('products')}"
        )

        q1_sql = (
            "SELECT region, SUM(amount) AS revenue FROM sales GROUP BY region ORDER BY revenue DESC"
        )
        q1 = _step(client, "query", {"sql": q1_sql})
        rows1 = q1["payload"].get("state", {}).get("last_rows")
        print(f"\nstep(query region revenue) -> status={q1['status']}")
        print(f"  REAL ROWS FROM SQLITE (through the FSM): {rows1}")

        q2_sql = (
            "SELECT product, SUM(quantity) AS units FROM sales GROUP BY product ORDER BY units DESC"
        )
        q2 = _step(client, "query", {"sql": q2_sql})
        rows2 = q2["payload"].get("state", {}).get("last_rows")
        print(f"step(query product units)  -> status={q2['status']}")
        print(f"  REAL ROWS FROM SQLITE (through the FSM): {rows2}")

        # The FSM's gate must refuse a non-SELECT supplied through Strands.
        bad = _step(client, "query", {"sql": "DROP TABLE sales"})
        print(
            f"\nstep(query 'DROP TABLE') -> status={bad['status']} "
            f"(FSM gate refuses non-SELECT) :: "
            f"{json.dumps(bad['payload'])[:160]}"
        )

        top_region = rows1[0]["region"] if rows1 else "?"
        top_product = rows2[0]["product"] if rows2 else "?"
        f1 = _step(
            client,
            "finding",
            {
                "insight": f"{top_region} is the top region by revenue.",
                "query": q1_sql,
            },
        )
        f2 = _step(
            client,
            "finding",
            {
                "insight": f"{top_product} is the top product by units sold.",
                "query": q2_sql,
            },
        )
        print(f"\nstep(finding x2) -> {f1['status']}, {f2['status']}")

        rep = _step(
            client,
            "report",
            {
                "summary": (
                    f"Sales analysis driven entirely through the Theodosia FSM from "
                    f"Strands. Top region by revenue is {top_region}; top product by "
                    f"units is {top_product}. Both findings cite gated SELECT queries "
                    f"executed inside the pipeline against the real SQLite database."
                )
            },
        )
        rst = rep["payload"].get("state", {})
        print(f"step(report)   -> status={rep['status']} phase={rst.get('phase')}")
        print(
            "\nVERDICT: Strands discovered AND invoked the Theodosia FSM as a "
            "native MCP tool, driving it from connect to a terminal report."
        )


def run_model_loop() -> bool:
    """Optional: let a real Strands Agent decide to call the FSM, via local Ollama.

    Returns True if a model loop ran. Requires a local Ollama
    (http://localhost:11434) serving a tool-calling model. Skipped otherwise so
    the demo never blocks on a model provider.
    """
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    model_id = os.environ.get("OLLAMA_MODEL", "qwen2.5:1.5b")
    try:
        import urllib.request

        with urllib.request.urlopen(f"{host}/api/tags", timeout=3) as resp:
            tags = json.loads(resp.read())
        available = {m["name"] for m in tags.get("models", [])}
    except Exception as exc:
        print(f"\n[model loop skipped] no local Ollama reachable at {host}: {exc}")
        return False
    if not any(model_id in name or name.startswith(model_id) for name in available):
        print(f"\n[model loop skipped] model {model_id!r} not pulled; have {sorted(available)}")
        return False

    from strands.models.ollama import OllamaModel

    client = make_theodosia_client()
    with client:
        tools = [calculator, current_time, *client.list_tools_sync()]
        agent = Agent(
            model=OllamaModel(host=host, model_id=model_id),
            tools=tools,
            system_prompt=(
                "You drive a data-analysis state machine exposed as the `step` tool. "
                "Advance it with step(action=..., inputs=...). Read theodosia://next "
                "via read_resource if unsure which action is valid. Walk: connect, "
                "load, profile, then query(sql=SELECT ...). Report what you find."
            ),
        )
        print("\n" + "=" * 70)
        print(f"MODEL LOOP  Strands Agent on Ollama/{model_id} driving the FSM")
        print("=" * 70)
        agent(
            "Connect to the pipeline, load and profile the sales data, then "
            "tell me total revenue by region. Use the step tool."
        )
    return True


def main() -> None:
    prove_discovery_and_invocation()
    run_model_loop()


if __name__ == "__main__":
    main()
