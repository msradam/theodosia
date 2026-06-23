"""In-process smoke tests: Haiku drives theodosia servers over stdio.

No external MCP config or demo bench required. Each test spawns a theodosia
server subprocess over stdio and passes it to the Claude Agent SDK.
Action schemas are in the server instructions, so the model navigates
without reading theodosia://graph first. These tests verify that property
holds on Haiku — the smallest Claude model in the family.

Prerequisite: ``claude`` CLI on PATH with a valid OAuth session.
Run with: ``uv run pytest -m smoke tests/smoke/test_inprocess.py``
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.smoke

_REPO = Path(__file__).resolve().parents[2]
_EXAMPLES = _REPO / "examples"
_PYTHON = str(_REPO / ".venv" / "bin" / "python3")

sys.path.insert(0, str(_EXAMPLES))

from ._helpers import (
    actions_called,
    calls_to,
    calls_with_action,
    check_cli_or_skip,
    drive_inprocess,
    result_for,
)

check_cli_or_skip()

_MODEL = "claude-haiku-4-5-20251001"


def _cmd(module: str, builder: str = "build_server") -> list[str]:
    """Subprocess command that starts a theodosia server on stdio."""
    return [
        _PYTHON,
        "-c",
        f"import sys; sys.path.insert(0, {str(_EXAMPLES)!r}); "
        f"from {module} import {builder}; {builder}().run(transport='stdio')",
    ]


@pytest.mark.asyncio
async def test_coffee_walks_to_fulfilled():
    """Haiku orders a latte and walks the FSM to terminal.

    No resource reads in the prompt — the model must rely entirely on the
    action schema embedded in the server instructions.
    """
    name = "coffee-order"
    trace = await drive_inprocess(
        _cmd("coffee_order"),
        name,
        (
            f"Use the {name} app to place a coffee order. "
            "Order one latte for $5.50, pay, and fulfill it. "
            "Stop when valid_next_actions is empty. Don't ask me anything."
        ),
        model=_MODEL,
        max_budget_usd=2.0,
        max_turns=15,
    )

    step_calls = calls_to(trace["tool_calls"], f"mcp__{name}__step")
    assert step_calls, f"Model never called step. Tools: {[c['name'] for c in trace['tool_calls']]}"

    actions = actions_called(trace["tool_calls"], f"mcp__{name}__step")
    assert "take_order" in actions, f"take_order never called. Actions: {actions}"
    assert "fulfill" in actions, f"fulfill never called. Actions: {actions}"

    last_result = result_for(trace["tool_results"], step_calls[-1]["id"])
    assert last_result is not None
    parsed = last_result["parsed"]
    assert parsed is not None
    assert parsed.get("error") is None, f"Last step returned error: {parsed!r}"
    assert parsed.get("valid_next_actions") == [], f"FSM didn't reach terminal: {parsed!r}"


@pytest.mark.asyncio
async def test_coffee_refusal_triggers_self_correction():
    """Haiku gets an invalid_transition refusal and self-corrects from valid_next_actions."""
    name = "coffee-order"
    trace = await drive_inprocess(
        _cmd("coffee_order"),
        name,
        (
            f"Use the {name} app. First try calling pay (before placing any order) "
            "so we can see the structured refusal. Then place a full order: "
            "take_order -> pay -> fulfill. Don't skip the deliberate first wrong call."
        ),
        model=_MODEL,
        max_budget_usd=2.0,
        max_turns=15,
    )

    step_calls = calls_to(trace["tool_calls"], f"mcp__{name}__step")
    assert step_calls, "Model never called step."

    pay_calls = calls_with_action(trace["tool_calls"], f"mcp__{name}__step", "pay")
    assert pay_calls, "Model skipped the deliberate pay-first call."

    first_pay_result = result_for(trace["tool_results"], pay_calls[0]["id"])
    assert first_pay_result is not None
    parsed = first_pay_result["parsed"]
    assert parsed is not None
    assert parsed.get("error") == "invalid_transition", (
        f"Expected invalid_transition refusal; got {parsed!r}"
    )
    assert "take_order" in parsed.get("valid_next_actions", []), (
        f"Refusal didn't carry take_order in valid_next_actions: {parsed!r}"
    )

    later = actions_called(trace["tool_calls"], f"mcp__{name}__step")[1:]
    assert "take_order" in later, f"Model didn't self-correct. Subsequent actions: {later!r}"


@pytest.mark.asyncio
async def test_chargen_walks_full_pipeline():
    """Haiku builds a D&D character through all six stages to a finalized sheet."""
    name = "chargen"
    trace = await drive_inprocess(
        _cmd("chargen"),
        name,
        (
            f"Use the {name} app to create a D&D character. "
            "Walk all six stages in order. Invent sensible values for every input. "
            "Stop when valid_next_actions is empty."
        ),
        model=_MODEL,
        max_budget_usd=3.0,
        max_turns=25,
    )

    step_calls = calls_to(trace["tool_calls"], f"mcp__{name}__step")
    assert len(step_calls) >= 5, (
        f"Expected >= 5 steps; got {len(step_calls)}. "
        f"Actions: {actions_called(trace['tool_calls'], f'mcp__{name}__step')}"
    )

    actions = actions_called(trace["tool_calls"], f"mcp__{name}__step")
    for required in ("begin", "choose_race", "choose_class", "assign_stats", "finalize"):
        assert required in actions, f"Missing {required!r}. Actions walked: {actions}"

    last_result = result_for(trace["tool_results"], step_calls[-1]["id"])
    assert last_result is not None
    parsed = last_result["parsed"]
    assert parsed is not None
    assert parsed.get("error") is None, f"Last step errored: {parsed!r}"
    assert parsed.get("valid_next_actions") == [], f"FSM didn't reach terminal: {parsed!r}"


@pytest.mark.asyncio
async def test_typed_state_loan_walks_to_decision():
    """Haiku walks a Pydantic-typed state FSM (typed_state_loan) to approval or denial."""
    name = "typed-state-loan"
    trace = await drive_inprocess(
        _cmd("typed_state_loan"),
        name,
        (
            f"Use the {name} app to process a loan application. "
            "Walk the FSM to an approval or denial terminal. "
            "Invent a plausible applicant. Stop when valid_next_actions is empty."
        ),
        model=_MODEL,
        max_budget_usd=2.0,
        max_turns=20,
    )

    step_calls = calls_to(trace["tool_calls"], f"mcp__{name}__step")
    assert step_calls, "Model never called step."

    last_result = result_for(trace["tool_results"], step_calls[-1]["id"])
    assert last_result is not None
    parsed = last_result["parsed"]
    assert parsed is not None
    assert parsed.get("error") is None, f"Last step errored: {parsed!r}"
    assert parsed.get("valid_next_actions") == [], f"FSM didn't reach terminal: {parsed!r}"


@pytest.mark.asyncio
async def test_action_error_is_surfaced_as_mcp_error():
    """When an action raises, the SDK receives is_error=True.

    Uses chargen's assign_stats with an invalid point-buy (all 8s = 0 pts).
    Haiku must receive the error signal and stop rather than hallucinating success.
    """
    name = "chargen"
    trace = await drive_inprocess(
        _cmd("chargen"),
        name,
        (
            f"Use the {name} app. Walk: begin (name=Test), choose_race (human), "
            "choose_class (fighter). Then call assign_stats with ALL stats set to 8. "
            "After you see the error response, stop. Do not retry or fix the inputs."
        ),
        model=_MODEL,
        max_budget_usd=2.0,
        max_turns=12,
    )

    assign_calls = calls_with_action(trace["tool_calls"], f"mcp__{name}__step", "assign_stats")
    assert assign_calls, "Model never called assign_stats."

    bad_result = result_for(trace["tool_results"], assign_calls[0]["id"])
    assert bad_result is not None
    assert bad_result["is_error"] is True, (
        f"Expected is_error=True for action_error; got {bad_result['is_error']!r}. "
        f"Result: {bad_result!r}"
    )
