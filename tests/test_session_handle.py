"""Session continuity via explicit handle.

The 2026-07-28 MCP revision removed protocol sessions; sessionless
transports mint a fresh ctx.session_id per request. The replacement
contract: every step result carries a `session` handle, and echoing it
back as step's `session` argument restores continuity, isolation, and
reset. On session-ful transports the handle is optional and the
transport session id keeps working as before.
"""

from __future__ import annotations

import fastmcp
import pytest
from coffee_order import build_server
from fastmcp import Client

from theodosia import ServingMode

# On FastMCP 3.x the in-memory transport keeps a stable per-client session, so
# calls without a handle continue one session; only 4.x exhibits the
# sessionless per-request churn these two tests document.
sessionless_only = pytest.mark.skipif(
    int(fastmcp.__version__.split(".")[0]) < 4,
    reason="requires the sessionless (MCP 2026-07-28) transport behavior of FastMCP 4",
)


async def _step(client, action, inputs, session=None):
    args = {"action": action, "inputs": inputs}
    if session is not None:
        args["session"] = session
    r = await client.call_tool("step", args)
    return r.structured_content


@pytest.mark.asyncio
async def test_handle_echo_restores_continuity():
    server = build_server(ServingMode.STEP)
    async with Client(server) as client:
        out1 = await _step(client, "take_order", {"item": "latte", "qty": 2})
        assert out1["state"]["stage"] == "ordered"
        handle = out1["session"]

        out2 = await _step(client, "pay", {"amount": 9.0}, session=handle)
        assert out2["state"]["stage"] == "paid"
        assert out2["session"] == handle

        out3 = await _step(client, "fulfill", {}, session=handle)
        assert out3["state"]["stage"] == "fulfilled"
        assert out3["valid_next_actions"] == []


@sessionless_only
@pytest.mark.asyncio
async def test_without_handle_each_call_is_a_fresh_session():
    server = build_server(ServingMode.STEP)
    async with Client(server) as client:
        out1 = await _step(client, "take_order", {"item": "latte", "qty": 1})
        out2 = await _step(client, "pay", {"amount": 9.0})
        assert out2["error"] == "invalid_transition"
        assert out2["valid_next_actions"] == ["take_order"]
        assert out1["session"] != out2["session"]


@sessionless_only
@pytest.mark.asyncio
async def test_interleaved_handles_stay_isolated():
    server = build_server(ServingMode.STEP)
    async with Client(server) as client:
        a = (await _step(client, "take_order", {"item": "latte", "qty": 1}))["session"]
        b = (await _step(client, "take_order", {"item": "mocha", "qty": 3}))["session"]
        assert a != b

        out_a = await _step(client, "pay", {"amount": 4.0}, session=a)
        assert out_a["state"]["item"] == "latte"
        assert out_a["state"]["stage"] == "paid"

        out_b = await _step(client, "add_modifier", {"modifier": "oat_milk"}, session=b)
        assert out_b["state"]["item"] == "mocha"
        assert out_b["state"]["stage"] == "ordered"

        out_a2 = await _step(client, "fulfill", {}, session=a)
        assert out_a2["state"]["stage"] == "fulfilled"
        out_b2 = await _step(client, "pay", {"amount": 12.0}, session=b)
        assert out_b2["state"]["stage"] == "paid"


@pytest.mark.asyncio
async def test_refusal_carries_handle_for_recovery():
    server = build_server(ServingMode.STEP)
    async with Client(server) as client:
        handle = (await _step(client, "take_order", {"item": "flat white", "qty": 1}))["session"]
        refusal = await _step(client, "fulfill", {}, session=handle)
        assert refusal["error"] == "invalid_transition"
        assert "pay" in refusal["valid_next_actions"]
        recovered = await _step(client, "pay", {"amount": 5.0}, session=handle)
        assert recovered["state"]["stage"] == "paid"


@pytest.mark.asyncio
async def test_reset_session_accepts_handle():
    server = build_server(ServingMode.STEP)
    async with Client(server) as client:
        handle = (await _step(client, "take_order", {"item": "latte", "qty": 1}))["session"]
        r = await client.call_tool("reset_session", {"session": handle})
        out = r.structured_content
        assert out["action"] == "reset_session"
        assert out["session"] == handle
        assert out["valid_next_actions"] == ["take_order"]

        after = await _step(client, "pay", {"amount": 4.0}, session=handle)
        assert after["error"] == "invalid_transition"
        assert after["valid_next_actions"] == ["take_order"]
