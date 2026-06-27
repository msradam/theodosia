"""Serve-time env toggles for embedding in agent frameworks, plus the
tool-channel recovery hint. All default off / behaviour-preserving.
"""

from __future__ import annotations

import sys

import pytest
from fastmcp import Client

from theodosia import ServingMode, mount

sys.path.insert(0, "examples")
from coffee_order import build_application


async def _refuse(server):
    """Trigger a guidance refusal (an out-of-order action) and return the result."""
    async with Client(server) as c:
        return await c.call_tool("step", {"action": "pay", "inputs": {}}, raise_on_error=False)


@pytest.mark.asyncio
async def test_strict_errors_flips_guidance_refusals(monkeypatch):
    server = mount(build_application, mode=ServingMode.STEP, name="c")
    r = await _refuse(server)
    assert r.is_error is False  # default: guidance refusal is a structured result

    monkeypatch.setenv("THEODOSIA_STRICT_ERRORS", "1")
    r2 = await _refuse(server)
    assert r2.is_error is True
    # payload (valid_next_actions) is preserved even in strict mode
    assert r2.structured_content.get("valid_next_actions") is not None


@pytest.mark.asyncio
async def test_single_block_collapses_to_one_content_block(monkeypatch):
    server = mount(build_application, mode=ServingMode.STEP, name="c")
    async with Client(server) as c:
        default = await c.call_tool("step", {"action": "take_order", "inputs": {"item": "latte"}})
    assert len(default.content) == 2  # headline + JSON

    monkeypatch.setenv("THEODOSIA_SINGLE_BLOCK", "1")
    server2 = mount(build_application, mode=ServingMode.STEP, name="c")
    async with Client(server2) as c:
        one = await c.call_tool("step", {"action": "take_order", "inputs": {"item": "latte"}})
    assert len(one.content) == 1
    assert one.structured_content is not None  # structure still available


@pytest.mark.asyncio
async def test_step_tool_description_carries_recovery_hint():
    server = mount(build_application, mode=ServingMode.STEP, name="c")
    async with Client(server) as c:
        tools = {t.name: t for t in await c.list_tools()}
    desc = tools["step"].description
    assert "valid_next_actions" in desc and "next_action_schemas" in desc
