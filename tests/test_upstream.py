"""upstream: theodosia as an MCP client to other servers.

A Burr action calls call_upstream(server, tool, args) from its body; the
call is forwarded to a real MCP server theodosia connected to as a client.
The agent only ever sees theodosia's `step` tool -- the upstream server is
not exposed to it (single surface). Every upstream call happens inside an
action, so it advances state (ledger).
"""

from __future__ import annotations

import pytest
from burr.core import ApplicationBuilder, State, action
from burr.core.action import Condition
from fastmcp import Client, FastMCP

from theodosia import ServingMode, UpstreamManager, call_upstream, mount
from theodosia.upstream import UpstreamError, bind_upstream, reset_upstream

# == a fake upstream MCP server (in-process) =========================


def _make_upstream_server() -> FastMCP:
    up = FastMCP("fake-cluster")

    @up.tool
    def get_pods(namespace: str = "default") -> dict:
        return {"namespace": namespace, "pods": ["api-aa", "api-bb"], "crashing": ["api-aa"]}

    @up.tool
    def get_logs(pod: str) -> dict:
        return {"pod": pod, "logs": "FATAL: redis connection refused"}

    return up


# == UpstreamManager unit ============================================


@pytest.mark.asyncio
async def test_manager_calls_upstream_tool():
    mgr = UpstreamManager({"cluster": _make_upstream_server()})
    out = await mgr.call("cluster", "get_pods", {"namespace": "prod"})
    assert out["namespace"] == "prod"
    assert "api-aa" in out["crashing"]
    await mgr.aclose()


@pytest.mark.asyncio
async def test_manager_unknown_server_raises():
    mgr = UpstreamManager({"cluster": _make_upstream_server()})
    with pytest.raises(UpstreamError, match="unknown upstream server"):
        await mgr.call("nope", "get_pods", {})
    await mgr.aclose()


@pytest.mark.asyncio
async def test_call_upstream_requires_bound_manager():
    with pytest.raises(UpstreamError, match="No upstream manager bound"):
        await call_upstream("cluster", "get_pods", {})


@pytest.mark.asyncio
async def test_call_upstream_uses_bound_manager():
    mgr = UpstreamManager({"cluster": _make_upstream_server()})
    token = bind_upstream(mgr)
    try:
        out = await call_upstream("cluster", "get_logs", {"pod": "api-aa"})
        assert "redis" in out["logs"]
    finally:
        reset_upstream(token)
        await mgr.aclose()


# == end-to-end: action calls upstream, driven via step =============


@action(reads=[], writes=["pods", "done"])
async def survey(state: State) -> State:
    """Survey the cluster by calling the upstream MCP server."""
    result = await call_upstream("cluster", "get_pods", {"namespace": "prod"})
    return state.update(pods=result.get("pods", []), done=False)


@action(reads=["pods"], writes=["report", "done"])
async def report(state: State) -> State:
    """Write a report from what was surveyed."""
    return state.update(report=f"{len(state['pods'])} pods seen", done=True)


def _build_app():
    _open = Condition.expr("done != True")
    return (
        ApplicationBuilder()
        .with_actions(survey=survey, report=report)
        .with_transitions(("survey", "report", _open))
        .with_state(pods=[], report=None, done=False)
        .with_entrypoint("survey")
        .build()
    )


@pytest.mark.asyncio
async def test_action_calls_upstream_through_mounted_server():
    """The agent calls only `step`; the survey action reaches the upstream
    cluster server through theodosia; the result lands in state."""
    server = mount(
        _build_app,
        mode=ServingMode.STEP,
        name="upstream-demo",
        upstream={"cluster": _make_upstream_server()},
    )
    async with Client(server) as client:
        # The agent's tool surface is just the theodosia meta-tools -- the
        # upstream cluster's get_pods/get_logs are NOT exposed here.
        tools = {t.name for t in await client.list_tools()}
        assert "step" in tools
        assert "get_pods" not in tools  # single surface: upstream hidden

        out = (
            await client.call_tool("step", {"action": "survey", "inputs": {}})
        ).structured_content
        assert "error" not in out
        # The upstream call ran inside the action; its result is in state.
        assert out["state"]["pods"] == ["api-aa", "api-bb"]


@pytest.mark.asyncio
async def test_upstream_tool_error_carries_structured_fields(tmp_path):
    """UpstreamManager.call wraps tool errors as UpstreamError with
    server/tool/body populated (ISSUE-015)."""
    from fastmcp import FastMCP

    failing = FastMCP("failing-upstream")

    @failing.tool
    def boom() -> str:
        raise ValueError("the upstream exploded: code=E42")

    mgr = UpstreamManager({"flaky": failing})
    try:
        with pytest.raises(UpstreamError) as excinfo:
            await mgr.call("flaky", "boom", {})
    finally:
        await mgr.aclose()
    err = excinfo.value
    assert err.server == "flaky"
    assert err.tool == "boom"
    assert err.body and "E42" in err.body
    assert "flaky" in str(err) and "boom" in str(err)


def test_upstream_error_binding_failures_have_no_fields():
    err = UpstreamError("no manager bound")
    assert err.server is None and err.tool is None and err.body is None


# ── per-session upstream isolation ──────────────────────────────────────


def test_config_is_per_session_detects_placeholder():
    from theodosia.upstream import _config_is_per_session

    assert _config_is_per_session({"m": {"env": {"FILE": "/d/{session}.json"}}})
    assert _config_is_per_session({"m": {"args": ["x", "{session}"]}})
    assert not _config_is_per_session({"m": {"command": "npx", "args": ["x"]}})
    assert not _config_is_per_session(None)


def test_substitute_session_replaces_and_sanitizes():
    from theodosia.upstream import substitute_session

    cfg = {"m": {"args": ["{session}/data"], "env": {"P": "/d/{session}.json"}}}
    out = substitute_session(cfg, "ab/cd-12")
    assert out["m"]["args"] == ["ab_cd-12/data"]  # '/' sanitized
    assert out["m"]["env"]["P"] == "/d/ab_cd-12.json"


def test_resolve_upstream_splits_shared_vs_per_session():
    from theodosia.adapter import _resolve_upstream

    shared, cfg = _resolve_upstream({"m": {"command": "npx", "args": ["x"]}})
    assert shared is not None and cfg is None  # plain config -> shared manager

    shared2, cfg2 = _resolve_upstream({"m": {"env": {"F": "{session}.json"}}})
    assert shared2 is None and cfg2 is not None  # placeholder -> per-session config

    assert _resolve_upstream(None) == (None, None)


def test_evicted_session_upstream_is_queued_for_close():
    from theodosia.adapter import _SessionStore

    class _Mgr:
        pass

    store = _SessionStore(ttl_seconds=None, max_sessions=2)
    e = store.get_or_create("a", lambda _sid: None)
    e.upstream = _Mgr()
    # Fill past max so 'a' (LRU) is evicted.
    store.get_or_create("b", lambda _sid: None)
    store.get_or_create("c", lambda _sid: None)
    closables = store.take_closables()
    assert e.upstream in closables
    assert store.take_closables() == []  # drained


# ── upstream polish: rows coercion, timeout, health resource ────────────


def test_classify_rows_coerces_repr_and_json():
    from theodosia.upstream import MALFORMED, OK, classify_payload

    # sqlite-style Python repr string
    repr_rows = "[{'region': 'West', 'revenue': 128622.4}]"
    r = classify_payload("q", repr_rows, expect="rows")
    assert r.status == OK and r.data == [{"region": "West", "revenue": 128622.4}]
    # JSON
    assert classify_payload("q", '[{"a": 1}]', expect="rows").data == [{"a": 1}]
    # single dict -> one row
    assert classify_payload("q", {"a": 1}, expect="rows").data == [{"a": 1}]
    # non-row-shaped
    assert classify_payload("q", "not rows", expect="rows").status == MALFORMED


@pytest.mark.asyncio
async def test_call_upstream_timeout_raises_structured():
    from theodosia.upstream import UpstreamError, bind_upstream, call_upstream

    class SlowMgr:
        async def call(self, server, tool, args):
            import asyncio

            await asyncio.sleep(5)

    bind_upstream(SlowMgr())
    with pytest.raises(UpstreamError) as ei:
        await call_upstream("s", "t", {}, timeout=0.1)
    assert ei.value.server == "s" and ei.value.body == "timeout"


@pytest.mark.asyncio
async def test_upstreams_resource_reports_health_and_mode():
    import json

    class FakeMgr:
        server_names = ("a", "b")

        async def call(self, *a, **k):
            return None

        async def health(self, *, timeout=10.0):
            return [
                {"server": "a", "status": "ok", "tools": ["x"]},
                {"server": "b", "status": "error", "error": "boom"},
            ]

    # Shared upstream (a pre-built manager) -> health.
    server = mount(_build_app(), name="u", upstream=FakeMgr())
    async with Client(server) as c:
        out = json.loads((await c.read_resource("theodosia://upstreams"))[0].text)
        assert out["mode"] == "shared"
        assert {u["server"]: u["status"] for u in out["upstreams"]} == {"a": "ok", "b": "error"}

    # Per-session config -> names + mode, no ping.
    server2 = mount(
        _build_app(),
        name="u",
        upstream={"mem": {"command": "x", "env": {"P": "{session}.json"}}},
    )
    async with Client(server2) as c:
        out = json.loads((await c.read_resource("theodosia://upstreams"))[0].text)
        assert out["mode"] == "per_session" and out["servers"] == ["mem"]

    # No upstream -> none.
    server3 = mount(_build_app(), name="u")
    async with Client(server3) as c:
        out = json.loads((await c.read_resource("theodosia://upstreams"))[0].text)
        assert out["mode"] == "none"
