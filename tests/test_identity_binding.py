"""A builder-returning factory binds Burr's app_id to the session id.

Returning an ``ApplicationBuilder`` (rather than a built ``Application``)
from the factory lets Theodosia stamp ``app_id = session_id`` before build,
so the Burr tracking dir matches the session key and stays put across resets.
"""

from __future__ import annotations

import json

import pytest
from burr.core import ApplicationBuilder, State, action

from theodosia import ServingMode, mount
from theodosia.adapter import _build_session_app


@action(reads=[], writes=["n"])
def bump(state: State) -> State:
    return state.update(n=state.get("n", 0) + 1)


def _builder() -> ApplicationBuilder:
    return (
        ApplicationBuilder()
        .with_actions(bump=bump)
        .with_transitions(("bump", "bump"))
        .with_state(n=0)
        .with_entrypoint("bump")
    )


def test_builder_factory_stamps_session_id_as_app_id():
    app = _build_session_app(_builder(), "session-xyz")
    assert app.uid == "session-xyz"


def test_built_application_passes_through_unchanged():
    built = _builder().with_identifiers(app_id="frozen").build()
    assert _build_session_app(built, "session-xyz") is built
    assert built.uid == "frozen"  # legacy path: id is whatever the builder froze


def test_wrong_return_type_is_rejected():
    with pytest.raises(TypeError):
        _build_session_app(object(), "session-xyz")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_app_id_is_stable_across_reset():
    """With a builder factory, the session's app_id survives reset_session."""
    from fastmcp import Client

    server = mount(_builder, mode=ServingMode.STEP, name="bind-test")

    async with Client(server) as client:
        before = json.loads((await client.read_resource("theodosia://session"))[0].text)
        await client.call_tool("step", {"action": "bump", "inputs": {}})
        await client.call_tool("reset_session", {})
        after = json.loads((await client.read_resource("theodosia://session"))[0].text)

    assert before["app_id"] == after["app_id"]


def _tracked_builder(storage: str):
    from burr.tracking.client import LocalTrackingClient

    def factory() -> ApplicationBuilder:
        return _builder().with_tracker(LocalTrackingClient(project="bind", storage_dir=storage))

    return factory


@pytest.mark.asyncio
async def test_mount_writes_no_phantom_template_dir(tmp_path):
    """The introspection template suppresses tracking, so mounting a tracked
    builder factory writes no ``theodosia-template`` session dir."""
    from pathlib import Path

    from fastmcp import Client

    storage = str(tmp_path / "burr")
    server = mount(_tracked_builder(storage), mode=ServingMode.STEP, name="bind")
    async with Client(server):
        pass

    proj = Path(storage) / "bind"
    dirs = [p.name for p in proj.iterdir()] if proj.exists() else []
    assert "theodosia-template" not in dirs


@pytest.mark.asyncio
async def test_fork_from_past_available_for_tracked_builder(tmp_path):
    """Suppressing the template tracker must not hide fork_from_past: the gate
    reads real-session tracked-ness, not the template's."""
    from fastmcp import Client

    storage = str(tmp_path / "burr")
    server = mount(_tracked_builder(storage), mode=ServingMode.STEP, name="bind")
    async with Client(server) as client:
        tools = {t.name for t in await client.list_tools()}
    assert "fork_from_past" in tools
