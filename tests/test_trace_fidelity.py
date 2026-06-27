"""The tracker records the post-step state, and resume restores the latest.

Burr's async ``astep`` mislogs sync actions: it delegates to ``_step`` with
hooks off, then fires ``post_run_step`` from its own ``finally`` with a stale
pre-step ``new_state``. The tracker (and anything reading it: ``theodosia://
trace``, ``fork_from_past``) would then lag one step, and the last committed
state would never be persisted. Theodosia drives sync bodies via ``app.step``
to avoid this. These tests pin that the on-disk state matches the live state.
"""

from __future__ import annotations

import json

import pytest
from burr.core import ApplicationBuilder, State, action
from burr.tracking.client import LocalTrackingClient
from fastmcp import Client

from theodosia import ServingMode, mount


@action(reads=["counter"], writes=["counter"])
def tick(state: State) -> State:
    return state.update(counter=state.get("counter", 0) + 1)


def _factory(project: str, storage: str):
    def factory():
        return (
            ApplicationBuilder()
            .with_actions(tick=tick)
            .with_transitions(("tick", "tick"))
            .with_state(counter=0)
            .with_entrypoint("tick")
            .with_tracker(LocalTrackingClient(project=project, storage_dir=storage))
        )

    return factory


@pytest.mark.asyncio
async def test_trace_last_entry_matches_live_state(tmp_path):
    """After N steps, the tracker's latest end_entry holds the live state."""
    storage = str(tmp_path / "burr")
    server = mount(_factory("fidelity", storage), mode=ServingMode.STEP, name="f")
    async with Client(server) as client:
        await client.call_tool("step", {"action": "tick", "inputs": {}})
        r = (await client.call_tool("step", {"action": "tick", "inputs": {}})).structured_content
        assert r["state"]["counter"] == 2

        trace = json.loads((await client.read_resource("theodosia://trace"))[0].text)
        end_entries = [e for e in trace if e.get("type") == "end_entry"]
        # The last persisted end_entry must carry the latest committed value,
        # not the pre-step value (which would be 1).
        assert end_entries[-1]["state"]["counter"] == 2


@pytest.mark.asyncio
async def test_resume_restores_latest_committed_state(tmp_path):
    """fork_from_past(-1) restores the latest state, not one step behind."""
    storage = str(tmp_path / "burr")
    factory = _factory("resume", storage)

    # Server A: run two steps, capture the app_id.
    server_a = mount(factory, mode=ServingMode.STEP, name="A")
    async with Client(server_a) as ca:
        await ca.call_tool("step", {"action": "tick", "inputs": {}})
        r = (await ca.call_tool("step", {"action": "tick", "inputs": {}})).structured_content
        app_id = r["app_id"]

    # Server B: fresh store, same storage dir, resume from the persisted run.
    server_b = mount(factory, mode=ServingMode.STEP, name="B")
    async with Client(server_b) as cb:
        resumed = (
            await cb.call_tool("fork_from_past", {"app_id": app_id, "sequence_id": -1})
        ).structured_content
        assert resumed["state"]["counter"] == 2
