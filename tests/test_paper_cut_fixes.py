"""Regression tests for paper-cut fixes (ergonomics + safety hardening).

Each test pins a specific rough edge that was reported and fixed:

* ``fork_at`` accepts ``seq`` as an alias for ``sequence_id`` and reports a
  clear error when neither is given.
* ``mount`` warns when handed a built Application (shared-app mode), so the
  no-per-session-isolation footgun is visible before it bites on HTTP.
* ``drive_claude``'s client constructor raises a theodosia-level error that
  points at the no-key Agent-SDK path.
* persona interpolation resolves only dict keys, never Python attributes.
* the CLI import resolver accepts a ``path/to/file.py:attr`` target and gives
  a path-aware hint when the ``:attr`` is missing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastmcp import Client

import theodosia.adapter as adapter
from theodosia import ServingMode, mount
from theodosia.cli import build_cli
from theodosia.cli import run as run_cli
from theodosia.cli._resolve import _import_target
from theodosia.drive import _default_anthropic_client
from theodosia.persona import render_with_frame

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"
sys.path.insert(0, str(EXAMPLES))

from adventure import build_application as adventure_factory


@pytest.mark.asyncio
async def test_fork_at_accepts_seq_alias():
    server = mount(adventure_factory, mode=ServingMode.STEP, name="fork-seq-alias")
    async with Client(server) as client:
        await client.call_tool("step", {"action": "enter_foyer", "inputs": {}})
        r = await client.call_tool("fork_at", {"seq": 0})
        out = r.structured_content
        assert out["action"] == "fork_at"
        assert out["result"]["from_action"] == "enter_foyer"
        assert out["result"]["sequence_id"] == 0


@pytest.mark.asyncio
async def test_fork_at_requires_a_sequence():
    server = mount(adventure_factory, mode=ServingMode.STEP, name="fork-missing-seq")
    async with Client(server) as client:
        await client.call_tool("step", {"action": "enter_foyer", "inputs": {}})
        r = await client.call_tool("fork_at", {})
        assert r.structured_content["error"] == "missing_sequence_id"


@pytest.mark.asyncio
async def test_mount_warns_on_shared_app_mode(caplog):
    adapter._shared_app_warned = False  # reset the one-time guard for this test
    with caplog.at_level("WARNING", logger="theodosia"):
        mount(adventure_factory(), mode=ServingMode.STEP, name="shared-warn")
    assert any("shared-app mode" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_mount_does_not_warn_in_factory_mode(caplog):
    adapter._shared_app_warned = False
    with caplog.at_level("WARNING", logger="theodosia"):
        mount(adventure_factory, mode=ServingMode.STEP, name="factory-no-warn")
    assert not any("shared-app mode" in r.message for r in caplog.records)


def test_default_anthropic_client_without_key_is_friendly(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    with pytest.raises((RuntimeError, ModuleNotFoundError)) as exc:
        _default_anthropic_client()
    assert "Agent SDK" in str(exc.value)


def test_persona_interpolation_resolves_dict_keys_only():
    frame = {"state": {"item": "soda"}}
    assert render_with_frame("{state.item}", frame) == "soda"
    # Attribute traversal into Python internals must not resolve.
    assert render_with_frame("{state.item.__class__}", frame) == ""
    assert render_with_frame("{state.item.__class__.__mro__}", frame) == ""


def test_import_target_accepts_file_path():
    target = f"{EXAMPLES / 'coffee_order.py'}:build_application"
    resolved = _import_target(target)
    assert callable(resolved)


def test_import_target_path_without_attr_gives_pathaware_hint():
    with pytest.raises(SystemExit) as exc:
        _import_target("examples/coffee_order.py")
    msg = str(exc.value)
    assert "module:attr" in msg
    assert "coffee_order:build_application" in msg


def test_unknown_cli_option_exits_cleanly(capsys):
    # standalone_mode=False makes Click raise NoSuchOption; the run wrapper must
    # turn it into a clean usage error + exit code 2, not a raw traceback.
    cli = build_cli("theodosia")
    code = run_cli(cli, ["sessions", "ls", "--app-dir", "foo"])
    assert code == 2
    assert "No such option" in capsys.readouterr().err
