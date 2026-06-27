"""Lazy tracking (persist on first step), cross-project spawn guard,
``session_app_id`` hook, and the enriched ``theodosia://session`` fields.
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest
from burr.core import ApplicationBuilder, State, action

import theodosia
from theodosia import LazyTrackingClient, ServingMode, mount


@action(reads=["n"], writes=["n"])
def tick(state: State) -> State:
    return state.update(n=state.get("n", 0) + 1)


def _factory(storage: str):
    def factory():
        return (
            ApplicationBuilder()
            .with_actions(tick=tick)
            .with_transitions(("tick", "tick"))
            .with_state(n=0)
            .with_entrypoint("tick")
            .with_tracker(theodosia.tracker("lazytest", storage_dir=storage))
        )

    return factory


def _dirs(storage: str) -> list[str]:
    p = Path(storage) / "lazytest"
    return sorted(x.name for x in p.iterdir()) if p.exists() else []


def test_tracker_returns_lazy_by_default():
    t = theodosia.tracker("p", storage_dir="/tmp/x")
    assert isinstance(t, LazyTrackingClient)
    from burr.tracking.client import LocalTrackingClient

    plain = theodosia.tracker("p", storage_dir="/tmp/x", lazy=False)
    assert isinstance(plain, LocalTrackingClient) and not isinstance(plain, LazyTrackingClient)


@pytest.mark.asyncio
async def test_read_only_session_writes_no_dir(tmp_path):
    from fastmcp import Client

    storage = str(tmp_path / "burr")
    server = mount(_factory(storage), mode=ServingMode.STEP, name="lz")
    async with Client(server) as c:
        await c.read_resource("theodosia://session")
        await c.read_resource("theodosia://state")
    assert _dirs(storage) == []  # nothing stepped -> nothing on disk


@pytest.mark.asyncio
async def test_first_step_persists_a_proper_dir(tmp_path):
    from fastmcp import Client

    storage = str(tmp_path / "burr")
    server = mount(_factory(storage), mode=ServingMode.STEP, name="lz")
    async with Client(server) as c:
        await c.call_tool("step", {"action": "tick", "inputs": {}})
    dirs = _dirs(storage)
    assert len(dirs) == 1
    contents = {f.name for f in (Path(storage) / "lazytest" / dirs[0]).iterdir()}
    assert {"graph.json", "metadata.json", "log.jsonl"} <= contents


@pytest.mark.asyncio
async def test_refusal_on_fresh_session_flushes_proper_dir(tmp_path):
    from fastmcp import Client

    storage = str(tmp_path / "burr")
    server = mount(_factory(storage), mode=ServingMode.STEP, name="lz")
    async with Client(server) as c:
        # First interaction is a refusal (unknown action) on a never-stepped session.
        await c.call_tool("step", {"action": "nope", "inputs": {}})
    dirs = _dirs(storage)
    assert len(dirs) == 1
    contents = {f.name for f in (Path(storage) / "lazytest" / dirs[0]).iterdir()}
    # A refusal is a real interaction: proper dir (graph+metadata), plus the sidecar.
    assert {"graph.json", "metadata.json", "refusals.jsonl"} <= contents


def test_cross_project_spawn_guard(tmp_path):
    storage = str(tmp_path / "burr")
    c = LazyTrackingClient(project="childproj", storage_dir=storage)
    # Parent in a different project (its dir is not under the child's storage_dir).
    foreign = types.SimpleNamespace(app_id="parent-elsewhere", sequence_id=0, partition_key=None)
    c._log_child_relationships(None, foreign, app_id="child-1", partition_key=None)
    assert not (Path(c.storage_dir) / "parent-elsewhere").exists()  # not fabricated

    # A parent that already exists under this storage_dir does get the link.
    import os

    real = Path(c.storage_dir) / "real-parent"
    os.makedirs(real, exist_ok=True)
    present = types.SimpleNamespace(app_id="real-parent", sequence_id=1, partition_key=None)
    c._log_child_relationships(None, present, app_id="child-2", partition_key=None)
    assert (real / "children.jsonl").exists()


@pytest.mark.asyncio
async def test_session_app_id_hook(tmp_path):
    from fastmcp import Client

    storage = str(tmp_path / "burr")
    server = mount(
        _factory(storage),
        mode=ServingMode.STEP,
        name="lz",
        session_app_id=lambda sid: f"ts-{sid[:6]}",
    )
    async with Client(server) as c:
        s = json.loads((await c.read_resource("theodosia://session"))[0].text)
    assert s["app_id"].startswith("ts-")
    assert s["app_id"] != s["fastmcp_session_id"]  # custom id, distinct handle


@pytest.mark.asyncio
async def test_session_resource_enriched_fields(tmp_path):
    from fastmcp import Client

    storage = str(tmp_path / "burr")
    server = mount(_factory(storage), mode=ServingMode.STEP, name="lz")
    async with Client(server) as c:
        before = json.loads((await c.read_resource("theodosia://session"))[0].text)
        assert before["fastmcp_session_id"] is not None
        assert before["tracker_project"] == "lazytest"
        assert before["persisted"] is False  # lazy: no dir yet

        await c.call_tool("step", {"action": "tick", "inputs": {}})
        after = json.loads((await c.read_resource("theodosia://session"))[0].text)
        assert after["persisted"] is True  # flushed on first step
