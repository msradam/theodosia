"""theodosia://children: Burr-native sub-apps spawned/forked from a session.

Burr appends a record to ``children.jsonl`` in the parent app's tracker dir
whenever an action spawns a sub-Application via ``with_spawning_parent`` (or
forks one). This resource surfaces those native children, distinct from
``theodosia://subruns`` (Theodosia's own ``spawn_subapp`` index).
"""

from __future__ import annotations

import json

import pytest
from burr.core import ApplicationBuilder, ApplicationContext, State, action
from burr.tracking.client import LocalTrackingClient
from fastmcp import Client

from theodosia import ServingMode, mount
from theodosia.adapter import _children_path


@action(reads=[], writes=["x"])
def probe(state: State) -> State:
    return state.update(x=1)


@action(reads=[], writes=["spawned"])
def spawn_runbook(state: State, storage_dir: str = "", project: str = "") -> State:
    """Spawn a Burr-native child parented at this app via with_spawning_parent."""
    ctx = ApplicationContext.get()
    assert ctx is not None
    child = (
        ApplicationBuilder()
        .with_actions(probe=probe)
        .with_transitions(("probe", "probe"))
        .with_state(x=0)
        .with_entrypoint("probe")
        .with_identifiers(app_id=f"{ctx.app_id}-child")
        .with_spawning_parent(app_id=ctx.app_id, sequence_id=ctx.sequence_id or 0)
        .with_tracker("local", project=project, params={"storage_dir": storage_dir})
        .build()
    )
    child.run(halt_after=["probe"])
    return state.update(spawned=f"{ctx.app_id}-child")


def _tracked_factory(project: str, storage_dir: str):
    def factory():
        return (
            ApplicationBuilder()
            .with_actions(
                spawn_runbook=spawn_runbook.bind(storage_dir=storage_dir, project=project)
            )
            .with_transitions(("spawn_runbook", "spawn_runbook"))
            .with_state(spawned=None)
            .with_entrypoint("spawn_runbook")
            .with_tracker(LocalTrackingClient(project=project, storage_dir=storage_dir))
        )

    return factory


def _untracked_app():
    return (
        ApplicationBuilder()
        .with_actions(probe=probe)
        .with_transitions(("probe", "probe"))
        .with_state(x=0)
        .with_entrypoint("probe")
        .build()
    )


@pytest.mark.asyncio
async def test_children_no_tracker():
    server = mount(_untracked_app, mode=ServingMode.STEP, name="no-tracker")
    async with Client(server) as client:
        out = json.loads((await client.read_resource("theodosia://children"))[0].text)
        assert out["error"] == "no_tracker"
        assert "LocalTrackingClient" in out["message"]


@pytest.mark.asyncio
async def test_children_empty_then_populated_after_spawn(tmp_path):
    storage = str(tmp_path / "burr")
    project = "children-test"
    server = mount(_tracked_factory(project, storage), mode=ServingMode.STEP, name="spawner")
    async with Client(server) as client:
        # Nothing spawned yet.
        before = json.loads((await client.read_resource("theodosia://children"))[0].text)
        assert before == []

        step = (
            await client.call_tool("step", {"action": "spawn_runbook", "inputs": {}})
        ).structured_content
        child_id = step["state"]["spawned"]

        after = json.loads((await client.read_resource("theodosia://children"))[0].text)
        assert isinstance(after, list) and len(after) == 1
        record = after[0]
        assert "spawn" in record["event_type"]
        assert record["child"]["app_id"] == child_id


def test_children_path_none_without_tracker():
    assert _children_path(_untracked_app()) is None
