"""Graph diagrams, action source, and session lineage/progress resources."""

from __future__ import annotations

import json

import pytest
from burr.core import ApplicationBuilder, State, action
from burr.tracking.client import LocalTrackingClient
from fastmcp import Client

from theodosia import ServingMode, mount


@action(reads=["n"], writes=["n"])
def tick(state: State) -> State:
    return state.update(n=state.get("n", 0) + 1)


def _factory():
    return (
        ApplicationBuilder()
        .with_actions(tick=tick)
        .with_transitions(("tick", "tick"))
        .with_state(n=0)
        .with_entrypoint("tick")
    )


@pytest.mark.asyncio
async def test_graph_mermaid_and_dot():
    server = mount(_factory, mode=ServingMode.STEP, name="t")
    async with Client(server) as c:
        mermaid = (await c.read_resource("theodosia://graph/mermaid"))[0].text
        assert mermaid.startswith("stateDiagram-v2")
        assert "[*] --> tick" in mermaid

        dot = (await c.read_resource("theodosia://graph/dot"))[0].text
        assert dot.startswith("digraph G {")
        assert '"tick" -> "tick"' in dot


@pytest.mark.asyncio
async def test_action_source_success_and_unknown():
    server = mount(_factory, mode=ServingMode.STEP, name="t")
    async with Client(server) as c:
        ok = json.loads((await c.read_resource("theodosia://source/tick"))[0].text)
        assert ok["action"] == "tick"
        assert "def tick" in ok["source"]

        bad = json.loads((await c.read_resource("theodosia://source/nope"))[0].text)
        assert bad["error"] == "unknown_action"
        assert "tick" in bad["known_actions"]


@pytest.mark.asyncio
async def test_session_progress_and_lineage(tmp_path):
    storage = str(tmp_path / "burr")

    def factory():
        return _factory().with_tracker(
            LocalTrackingClient(project="introspect", storage_dir=storage)
        )

    server = mount(factory, mode=ServingMode.STEP, name="t")
    async with Client(server) as c:
        before = json.loads((await c.read_resource("theodosia://session"))[0].text)
        assert before["current_action"] == "tick"  # entrypoint, about to run
        assert before["parent"] is None and before["spawning_parent"] is None

        await c.call_tool("step", {"action": "tick", "inputs": {}})
        after = json.loads((await c.read_resource("theodosia://session"))[0].text)
        # sequence_id advances once a step has run.
        assert after["sequence_id"] is not None and after["sequence_id"] >= 0
